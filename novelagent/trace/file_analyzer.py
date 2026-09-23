"""LLM extraction of file-backed Insight, Memory and Pattern records."""
import json
import re
from pathlib import Path

from novelagent.core.llm_turn import build_assistant_message
from novelagent.trace.file_lifecycle import FileLifecycleStore, MAX_RECORD_WEIGHT, PATTERN_PROMOTION_WEIGHT
from novelagent.trace.store import TraceStore
from novelagent.trace.agent_run import AgentRunTrace
from novelagent.trace.normalized_trajectory import build_trajectory_prompt, validate_trajectory_payload
from novelagent.tools.base import PermissionResult, ToolContext, ToolResult
from novelagent.tools.get_trace_context import GetTraceContextTool
from novelagent.tools.read import ReadTool
from novelagent.tools.search_rag import SearchRagTool


class TraceAnalysisCallError(RuntimeError):
    pass


class FileTraceAnalyzer:
    def __init__(self, llm_client, workspace_dir: str, trace_store: TraceStore, embedding_gate=None, rag_store=None,
                 bad_case_recorder=None):
        self.llm, self.workspace_dir, self.trace_store, self.embedding_gate = llm_client, workspace_dir, trace_store, embedding_gate
        self.rag_tool = SearchRagTool(rag_store) if rag_store else None
        self.read_tool = ReadTool()
        self.bad_cases = bad_case_recorder

    @staticmethod
    def _execution_event(events: list[dict] | None, event_type: str, payload: dict,
                         actor: str = "memory_analyzer") -> None:
        if events is None:
            return
        if event_type != "llm_request" or not isinstance(payload.get("messages"), list):
            events.append({"event_type": event_type, "actor": actor, "payload": payload})
            return
        messages = payload.get("messages", [])
        tools = payload.get("tools", [])
        events.append({
            "event_type": "llm_request", "actor": actor,
            "payload": {
                "position": payload.get("position", ""), "tag": payload.get("tag", ""),
                "message_count": len(messages), "tool_count": len(tools),
            },
        })
        chunk_size = 12_000
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            content = str(message.get("content", ""))
            chunks = [content[index:index + chunk_size] for index in range(0, len(content), chunk_size)] or [""]
            metadata = {key: value for key, value in message.items() if key != "content"}
            for chunk_index, chunk in enumerate(chunks):
                events.append({
                    "event_type": "agent_request_message", "actor": "system",
                    "payload": {
                        "message_index": message_index, "chunk_index": chunk_index,
                        "chunk_count": len(chunks), "content": chunk, **metadata,
                    },
                })
        for tool_index, tool in enumerate(tools):
            events.append({
                "event_type": "agent_tool_schema", "actor": "system",
                "payload": {"tool_index": tool_index, "schema": tool},
            })

    async def _capture_analysis_bad_case(self, *, trace_id: str, project_id: str, session_id: str,
                                         error: Exception | str, messages: list[dict] | None,
                                         agent_trace: AgentRunTrace | None, failure_kind: str) -> None:
        if not self.bad_cases:
            return
        if agent_trace:
            await agent_trace.flush()
        await self.bad_cases.capture(
            source_trace_id=agent_trace.trace_id if agent_trace else trace_id,
            session_id=session_id, project_id=project_id,
            failure_kind=failure_kind, actor="memory_analyzer", error=str(error),
            messages=messages,
        )

    async def analyze(self, trace_id: str, project_id: str, branch_messages=None, _tools=None, *,
                      events_override: list[dict] | None = None,
                      source_trace_ids: list[str] | None = None,
                      require_summary: bool = False,
                      normalized_trajectory: dict | None = None,
                      window_id: str = "") -> dict:
        execution_events: list[dict] = []
        trace = await self.trace_store.get_trace(trace_id)
        if not trace or (trace.get("analysis_status") == "skipped" and not require_summary):
            return {"window_summary": "", "agent_trace_id": ""}
        session_id = str((trace or {}).get("session_id", ""))
        agent_trace = None
        if self.bad_cases:
            agent_trace = await AgentRunTrace.try_start(
                self.trace_store, session_id=session_id, project_id=project_id,
                actor="memory_analyzer",
                position="main_loop" if branch_messages else "memory_summary_fallback",
                source_trace_id=trace_id,
                title=f"[memory_analyzer] {trace_id}",
            )
        execution_events = agent_trace.events if agent_trace else []
        try:
            window_summary = await self._analyze_impl(
                trace_id, project_id, branch_messages, _tools,
                events_override=events_override, source_trace_ids=source_trace_ids,
                execution_events=execution_events, agent_trace=agent_trace,
                require_summary=require_summary, normalized_trajectory=normalized_trajectory,
                window_id=window_id,
            )
            if agent_trace:
                await agent_trace.finish("completed", window_summary)
            return {
                "window_summary": window_summary,
                "agent_trace_id": agent_trace.trace_id if agent_trace else "",
            }
        except Exception as exc:
            if agent_trace:
                await agent_trace.finish("failed", error=str(exc))
            await self._capture_analysis_bad_case(
                trace_id=trace_id, project_id=project_id, session_id=session_id,
                error=exc, messages=branch_messages, agent_trace=agent_trace,
                failure_kind="memory_analysis_failure",
            )
            raise

    async def _analyze_impl(self, trace_id: str, project_id: str, branch_messages=None, _tools=None, *,
                            events_override: list[dict] | None = None,
                            source_trace_ids: list[str] | None = None,
                            execution_events: list[dict] | None = None,
                            agent_trace: AgentRunTrace | None = None,
                            require_summary: bool = False,
                            normalized_trajectory: dict | None = None,
                            window_id: str = "") -> str:
        trace = await self.trace_store.get_trace(trace_id)
        if not trace or (trace.get("analysis_status") == "skipped" and not require_summary): return ""
        events = events_override if events_override is not None else await self.trace_store.list_events(trace_id, limit=500)
        events = TraceStore.annotate_event_turns(events)
        source_trace_ids = list(dict.fromkeys(source_trace_ids or [trace_id]))
        event_trace_ids = {str(event.get("event_id")): str(event.get("trace_id") or trace_id) for event in events}
        if self.embedding_gate:
            inputs = [self._event_user_content(e) for e in events if e["event_type"] in {"user_message", "user_answer"}]
            if (await self.embedding_gate.evaluate(inputs)).skip and not require_summary:
                for source_trace_id in source_trace_ids:
                    await self.trace_store.set_trace_operation(source_trace_id, "routine", "skipped")
                return ""
        files = FileLifecycleStore(self.workspace_dir, project_id)
        valid = {e["event_id"] for e in events}
        records_by_layer = {layer: files.list(layer) for layer in ("insight", "memory", "pattern")}
        all_records = [
            (layer, record) for layer in ("insight", "memory", "pattern")
            for record in records_by_layer[layer]
        ]
        context_records = [
            *(("insight", record) for record in records_by_layer["insight"][:120]),
            *(("memory", record) for record in records_by_layer["memory"][:80]),
            *(("pattern", record) for record in records_by_layer["pattern"][:40]),
        ]
        context = [{"id": x["id"], "title": x.get("title", x.get("claim", "")), "layer": layer, "category": x.get("category", "project"), "domain": x.get("domain", "overall"), "kind": x.get("kind", ""), "file_path": x.get("file_path", ""), "weight": x.get("weight", 0), "support_count": x.get("support_count", 1), "promotion_status": x.get("promotion_status", "auto"), "downgraded_from": x.get("downgraded_from", "")} for layer, x in context_records]
        feedback_context = [{
            "id": x["id"], "layer": layer, "title": x.get("title", ""),
            "artifact_path": x.get("artifact_path", ""), "anchor_excerpt": str(x.get("anchor_excerpt", ""))[:700],
            "user_requirements": x.get("user_requirements", ""),
            "revision_direction": x.get("revision_direction", x.get("feedback_direction", "")),
            "content": str(x.get("content", ""))[:1600],
            "trace_ids": x.get("trace_ids", []), "source_event_ids": x.get("source_event_ids", []),
            "weight": x.get("weight", 0), "support_count": x.get("support_count", 1),
            "promotion_status": x.get("promotion_status", "auto"),
        } for layer, x in all_records if FileLifecycleStore.is_text_feedback(x)][:80]

        event_view = [self._event_summary(event) for event in events if self._keep_event(event)]
        trajectory_prompt = build_trajectory_prompt(source_trace_ids, event_view)
        trajectory = validate_trajectory_payload(normalized_trajectory or {}, events)
        trajectory_was_reused = bool(trajectory)
        trajectory_uses_fallback = not bool(branch_messages)
        if not trajectory:
            trajectory_messages = [
                *(branch_messages or []),
                {"role": "user", "content": trajectory_prompt},
            ]
            try:
                trajectory_text = await self._chat_text(
                    trajectory_messages,
                    "main_loop" if branch_messages else "memory_summary_fallback",
                    tag=":trace-fork/trajectory" if branch_messages else ":trace-fork/trajectory/fallback",
                    execution_events=execution_events,
                )
                trajectory = validate_trajectory_payload(self._json(trajectory_text), events)
                if not trajectory:
                    raise TraceAnalysisCallError("轨迹整理轮返回了无效 normalized trajectory 或来源绑定")
            except TraceAnalysisCallError as exc:
                if not branch_messages:
                    raise
                await self._capture_analysis_bad_case(
                    trace_id=trace_id, project_id=project_id, session_id=str(trace.get("session_id", "")),
                    error=exc, messages=trajectory_messages, agent_trace=agent_trace,
                    failure_kind="trajectory_normalization_primary_failure",
                )
                print(f"[trace] cached trajectory round failed, retrying locally: {exc}", flush=True)
                trajectory_uses_fallback = True
                trajectory_text = await self._chat_text(
                    [{"role": "user", "content": trajectory_prompt}],
                    "memory_summary_fallback", tag=":trace-fork/trajectory/fallback",
                    execution_events=execution_events,
                )
                trajectory = validate_trajectory_payload(self._json(trajectory_text), events)
                if not trajectory:
                    raise TraceAnalysisCallError("独立轨迹整理轮仍未返回有效 normalized trajectory")
            if window_id:
                await self.trace_store.save_trace_window_trajectory(
                    window_id, trajectory, agent_trace.trace_id if agent_trace else "",
                )
        self._execution_event(execution_events, "normalized_trajectory", {
            "window_id": window_id,
            "reused": trajectory_was_reused,
            **trajectory,
        })
        trajectory_json = json.dumps(trajectory, ensure_ascii=False)
        trajectory_context_prompt = trajectory_prompt if not trajectory_was_reused else (
            "[Memory/Summary Agent：已保存轨迹]\n"
            "下面的 assistant 消息是当前 Trace window 已验证并持久化的 normalized trajectory。"
            "直接复用它，不要重新读取或重建原始 Trace。"
        )
        trajectory_history = [
            *([] if trajectory_uses_fallback else (branch_messages or [])),
            {"role": "user", "content": trajectory_context_prompt},
            {"role": "assistant", "content": trajectory_json},
        ]
        prompt = f'''[后台 Trace 分支命令]\n你收到的不是完整 Trace，而是每条 Trace 中与本次分析锚点临近的少量 ReAct turn。不要回复用户、不要续写，只分析原始 Trace：{json.dumps(source_trace_ids, ensure_ascii=False)}。每个 Trace event 都携带 trace_id 和 turn；记录必须通过 source_event_ids 保留真实来源，程序会据此把记录精确关联到 trace_id + turn。\n只输出 JSON：{{"items":[{{"event_id":"","type":"error|correction|confirmation|feedback","summary":"","confidence":0.5}}],"records":[{{"layer":"insight|memory","category":"user|project|reference","domain":"writing|outline|overall","title":"不超过40字的简要标题","content":"具体事实、约束、适用条件和必要背景","claim":"与 title 一致","source_event_ids":[""],"signal":"weak|strong","relation":"new|support|append","related_id":"","confidence":0.5,"kind":"text_feedback 时填写","artifact_path":"可从工具事件推断时填写","artifact_revision_id":"","anchor_excerpt":"用户引用或评价的原文，最多700字","anchor_sha256":"工具事件提供时填写","user_requirements":"综合历次反馈后，用户希望该段达到什么效果以及明确禁止什么","revision_direction":"后续应如何修改，包括措辞、情节、人物表现和节奏等方向","feedback_direction":"兼容字段，填写当前建议的修改方向","feedback_relation":"same_anchor|cross_text_support|cross_text_conflict","explicit_reconfirmation":false}}]}}。\n不记录：普通闲聊、纯工具调用、一次性命令，例如“继续写作”“读取文件”。\nInsight 是从 Trace 得出的、尚不足以长期生效的低置信观察或假设，例如用户单次说“这章对话节奏有点慢”；它保留来源，等待后续相似反馈支持。\nMemory 是明确、可复用的长期偏好、修正或约束，例如“以后打斗必须突出空间关系”，或“把林深的初始性格改为恐惧回避型”；这类记录 layer=memory、signal=strong。\n凡是由用户 correction 事件得出的记录，source_event_ids 必须包含对应 user_message 或 user_answer 的 event_id，且 layer=memory、signal=strong；不要用后续 assistant_turn 替代该证据。support 必须引用 Existing 的 memory id。\n\n文本修改反馈的特殊规则：当用户粘贴、引用或明确评价某段小说/大纲原文时，写 category=reference、kind=text_feedback，并从工具事件补足 artifact_path、修订和锚点。程序会根据 source_event_ids 保存用户完整原始输入；不要复述或截断用户原话。user_requirements 必须综合历次反馈，提炼用户希望该段达到的效果和明确禁止项；revision_direction 必须总结后续具体修改方向，而不是生成原文摘要。与同一锚点/同一段原文的重复意见使用 relation=append、feedback_relation=same_anchor：只追加历史并更新这两个字段，绝不加分。仅当是不同但相似的文本，且用户修改方向能相互验证时，relation=support、feedback_relation=cross_text_support，才允许为 Existing text_feedback 加分。相同/相似文本但方向相反时用 append、feedback_relation=cross_text_conflict：记录反例与不确定性，不加分。是否同锚点、是否跨文本可迁移由你根据原文、用户引用和 Existing 自行判断。\nTrace events:{json.dumps(event_view, ensure_ascii=False)}\nExisting:{json.dumps(context, ensure_ascii=False)}\nExisting text feedback (full):{json.dumps(feedback_context, ensure_ascii=False)}'''

        prompt = prompt.replace(
            '只输出 JSON：{"items":',
            '只输出 JSON：{"window_summary":"仅概括当前分析窗口的摘要","items":',
            1,
        )
        prompt += "\nwindow_summary 必须始终填写，即使 items 和 records 为空；只概括本次 source_trace_ids 对应的当前分析窗口，不得重复概括更早窗口。保留该窗口内的关键决策、人物与设定变更、文件修订、用户偏好、未完成事项，最多 2000 字，不要写分析过程。"
        prompt = prompt.replace("user|project|reference", "user|project|reference|agent")
        prompt += "\n单次质疑异常工具或 Agent 行为（例如‘为什么调用 AskUserQuestion 工具’）属于 Insight，category=agent；只有明确要求长期遵循的工具流程才属于 Memory。agent 分类只用于后续 Agent 优化，不能作为项目写作或大纲记忆。"
        prompt += "\n自动审阅事件（workflow=auto_review、preset=reviewer）的 result 是审阅报告。报告中明确指出、并且未来写作可复用地检查或避免的写作缺陷，使用 category=project、domain=writing、kind=review_issue。单次审阅发现只能新建 layer=insight、signal=weak；同一种底层错误在不同 Trace 或不同正文修订中再次出现时，必须 relation=support 并引用 Existing 中对应的 review_issue。不要记录一次性错字、仅适用于当前情节的修改项、审阅报告中的肯定项，也不要把 polisher 的修改说明当作新洞察。content 应写清错误表现、适用条件、判断方法和改进方向。程序会在重复洞察达到阈值后自动晋级为 Memory，再继续积累为 Pattern。"
        prompt += "\n决定写入前必须先根据 Existing 选择候选：Insight 只能与 Insight 合并，低幅加分；Memory 可以与 Memory 或 Insight 合并，高幅加分。Memory 候选还必须查看 Existing 中的 Pattern：若语义相同，relation=support、related_layer=pattern，直接为该 Pattern 加分，不重复新建 Pattern。records 可额外返回 related_layer=insight|memory|pattern。被容量挤出的旧 Memory 在 Insight 中保留 origin_memory_id，新的 Memory 可以将其重新提升。"
        prompt += "\npromotion_status=manual_review 表示用户曾手动降级并持有不同意见。后续证据仍可追加，但程序禁止自动晋级。只有用户本轮明确重新确认该记录可作为更高等级规则时，才设置 explicit_reconfirmation=true；不得根据普通支持或相似表述自行解除。"
        prompt += "\n每条 records 还必须提供 title 和 content：title 是不超过 40 字、可用于索引和列表的简要标题；content 是可独立理解的具体事实、约束、适用条件和必要背景。claim 保持与 title 一致，用于兼容旧记录。text_feedback 必须提供 user_requirements 和 revision_direction；程序会保存 source_event_ids 对应的完整用户原始输入并将三部分一起落盘。若局部 turn 不足，可调用一次 GetTraceContext，按 start_turn/end_turn 补查当前 Session 内任意 Trace 的必要区间；若必须核对工件原文或修订号，可调用一次 Read 读取当前项目文件。不要为了保险读取整条 Trace 或无关文件。获得工具结果后必须输出完整 JSON。"

        trace_event_block = f"Trace events:{json.dumps(event_view, ensure_ascii=False)}"
        analysis_prompt = prompt.replace(
            trace_event_block,
            "上一轮 assistant 已输出并保存 normalized trajectory 及 provenance。"
            "必须以该 trajectory 为本轮事实输入，并直接使用 provenance 中的 source_event_ids；"
            "不要重新复述、改写或猜测原始 Trace。",
        ).replace(
            "若局部 turn 不足，可调用一次 GetTraceContext，按 start_turn/end_turn 补查当前 Session 内任意 Trace 的必要区间；若必须核对工件原文或修订号，可调用一次 Read 读取当前项目文件。不要为了保险读取整条 Trace 或无关文件。获得工具结果后必须输出完整 JSON。",
            "只允许调用 GetTraceContext 或 Read：前者按 start_turn/end_turn 补查当前 Session 内未被 normalized trajectory 覆盖的必要区间，后者核对当前项目内的工件原文或修订号；其他工具不得调用。获得工具结果后必须输出完整 JSON。",
        ).replace(
            "你收到的不是完整 Trace，而是每条 Trace 中与本次分析锚点临近的少量 ReAct turn。不要回复用户、不要续写，只分析原始 Trace：",
            "上一轮已把当前分析窗口整理成 normalized trajectory。不要回复用户、不要续写，只分析其中与以下来源 Trace 对应的记录：",
        )

        preferred_sources: dict[str, list[str]] = {}
        for record in feedback_context:
            for feedback_trace_id in record.get("trace_ids", []):
                key = str(feedback_trace_id)
                preferred_sources.setdefault(key, []).extend(
                    str(value) for value in record.get("source_event_ids", [])
                )
        trace_tool = GetTraceContextTool(
            self.trace_store, str(trace.get("session_id", "")), preferred_sources,
        )
        analysis_branch_messages = trajectory_history
        try:
            data = self._json(await self._extract(
                analysis_prompt, analysis_branch_messages, trace_tool=trace_tool, project_id=project_id,
                session_id=str(trace.get("session_id", "")), source_trace_id=trace_id,
                cached_tools=_tools, execution_events=execution_events,
                fallback_mode=trajectory_uses_fallback,
            ))
            if not self._valid_analysis_payload(data, require_summary):
                raise TraceAnalysisCallError("记忆与摘要分析返回空结果、无效 JSON 或缺少 window_summary")
        except TraceAnalysisCallError as exc:
            if trajectory_uses_fallback:
                raise
            await self._capture_analysis_bad_case(
                trace_id=trace_id, project_id=project_id, session_id=str(trace.get("session_id", "")),
                error=exc, messages=[*analysis_branch_messages, {"role": "user", "content": analysis_prompt}],
                agent_trace=agent_trace,
                failure_kind="memory_analysis_primary_failure",
            )
            print(f"[trace] cached branch failed, retrying with local turns only: {exc}", flush=True)
            trajectory_uses_fallback = True
            analysis_branch_messages = [
                {"role": "user", "content": trajectory_context_prompt},
                {"role": "assistant", "content": trajectory_json},
            ]
            data = self._json(await self._extract(
                analysis_prompt, analysis_branch_messages, trace_tool=trace_tool, project_id=project_id,
                session_id=str(trace.get("session_id", "")), source_trace_id=trace_id,
                execution_events=execution_events, fallback_mode=True,
            ))
            if not self._valid_analysis_payload(data, require_summary):
                raise TraceAnalysisCallError("独立记忆与摘要兜底仍未返回有效结果")
        if trace_tool.last_events:
            by_event_id = {str(event.get("event_id")): event for event in [*events, *trace_tool.last_events]}
            events = TraceStore.annotate_event_turns(list(by_event_id.values()))
            valid = {event["event_id"] for event in events}
            event_trace_ids = {
                str(event.get("event_id")): str(event.get("trace_id") or trace_id) for event in events
            }
        checkpoint_reasons = self._checkpoint_reasons(events)
        if checkpoint_reasons and not data.get("items") and not data.get("records"):
            retry_prompt = f'''[Trace 记忆提取重试]\n上一轮返回了空结果，但当前 Trace 明确调用了 CreateTraceCheckpoint。必须重新判断，不能返回空对象。\n只输出与主 schema 相同的 JSON：{{"window_summary":"仅概括当前分析窗口的摘要","items":[],"records":[]}}。\n用户明确指出具体文本问题时，items 至少包含 correction 或 feedback；records 必须生成 category=reference、domain=writing、kind=text_feedback、layer=memory、signal=strong 的记录，并让 source_event_ids 指向 user_message。\n若 checkpoint 并非写作反馈，也必须给出对应分类；只有确认不应长期记录时 records 才可为空。\nCheckpoint reasons:{json.dumps(checkpoint_reasons, ensure_ascii=False)}\nTrace events:{json.dumps(event_view, ensure_ascii=False)}\nExisting:{json.dumps(context, ensure_ascii=False)}'''
            data = self._json(await self._extract(
                retry_prompt, None, trace_tool=trace_tool, project_id=project_id,
                session_id=str(trace.get("session_id", "")), source_trace_id=trace_id,
                execution_events=execution_events,
            ))
        if checkpoint_reasons and not data.get("items") and not data.get("records"):
            raise ValueError("CreateTraceCheckpoint 已触发，但 Trace 判别连续返回空结果")
        window_summary = str(data.get("window_summary") or "").strip()[:8000]
        post_branch_messages = None if trajectory_uses_fallback else analysis_branch_messages
        post_analysis_prompt = analysis_prompt if post_branch_messages else (
            f"{analysis_prompt}\nNormalized trajectory:{trajectory_json}"
        )
        if self._has_reference_feedback(data):
            try:
                data = await self._review_reference_feedback(
                    trace_id, project_id, post_analysis_prompt, post_branch_messages, data,
                    execution_events=execution_events,
                )
            except TraceAnalysisCallError:
                if not post_branch_messages:
                    raise
                data = await self._review_reference_feedback(
                    trace_id, project_id,
                    f"{analysis_prompt}\nNormalized trajectory:{trajectory_json}", None, data,
                    execution_events=execution_events,
                )
            if window_summary and not data.get("window_summary"):
                data["window_summary"] = window_summary
        if data.get("records"):
            try:
                data = await self._reconcile_records(
                    trace_id, project_id, post_analysis_prompt, post_branch_messages,
                    data, context, trace_tool, _tools, str(trace.get("session_id", "")),
                    execution_events=execution_events,
                )
            except TraceAnalysisCallError:
                if not post_branch_messages:
                    raise
                data = await self._reconcile_records(
                    trace_id, project_id,
                    f"{analysis_prompt}\nNormalized trajectory:{trajectory_json}", None,
                    data, context, trace_tool, None, str(trace.get("session_id", "")),
                    execution_events=execution_events,
                )
            if window_summary and not data.get("window_summary"):
                data["window_summary"] = window_summary
        classifications = []
        for raw_item in data.get("items", []):
            if not isinstance(raw_item, dict) or raw_item.get("type") not in {"error", "correction", "confirmation", "feedback"}:
                continue
            item = dict(raw_item)
            event_id = str(item.get("event_id") or "")
            if event_id not in valid:
                event_id = self._match_source_event_id(events, str(item.get("source_excerpt") or ""))
            if not event_id:
                defaults = self._default_source_event_ids(events, str(item.get("type") or ""))
                event_id = defaults[-1] if defaults else ""
            if event_id:
                item["event_id"] = event_id
                classifications.append(item)
        agent_feedback_events = [
            event for event in events
            if event["event_type"] == "user_message" and self._is_agent_feedback(str(event.get("payload", {}).get("content", "")))
        ]
        classified_ids = {item["event_id"] for item in classifications}
        classifications.extend({
            "event_id": event["event_id"], "type": "feedback",
            "summary": "用户质疑 Agent 的工具调用或调度行为。", "confidence": 0.9,
        } for event in agent_feedback_events if event["event_id"] not in classified_ids)
        classifications_by_trace: dict[str, list[dict]] = {}
        for item in classifications:
            source_trace_id = event_trace_ids.get(str(item.get("event_id")), trace_id)
            classifications_by_trace.setdefault(source_trace_id, []).append(item)
        for source_trace_id in source_trace_ids:
            await self.trace_store.save_trace_classification(
                source_trace_id, classifications_by_trace.get(source_trace_id, []),
            )
        correction_ids = {item["event_id"] for item in classifications if item["type"] == "correction"}
        recorded_event_ids: set[str] = set()
        for item in data.get("records", []):
            if not isinstance(item, dict): continue
            ids = [str(x) for x in item.get("source_event_ids", []) if str(x) in valid]
            if not ids:
                matched_id = self._match_source_event_id(events, str(item.get("source_excerpt") or ""))
                ids = [matched_id] if matched_id else self._default_source_event_ids(events, str(item.get("kind") or ""))
            claim = str(item.get("claim", "")).strip()
            layer = item.get("layer")
            if layer not in {"insight", "memory"} or not ids or not claim: continue
            record_trace_ids = list(dict.fromkeys(event_trace_ids.get(event_id, trace_id) for event_id in ids))
            record_trace_id = record_trace_ids[-1]
            trace_refs = self._trace_refs(events, ids, record_trace_id)
            item["trace_refs"] = trace_refs
            recorded_event_ids.update(ids)
            if any(event["event_id"] in ids for event in agent_feedback_events):
                item["category"] = "agent"
            # A concrete user correction is durable by definition.  The model
            # still writes the claim and scope, while the lifecycle boundary
            # prevents it from being stranded as a weak one-off insight.
            if correction_ids.intersection(ids):
                layer = "memory"
                item["signal"] = "strong"
            kind = str(item.get("kind") or "").strip()
            if kind == "text_feedback":
                item["category"] = "reference"
                user_inputs = self._user_inputs(events, ids, record_trace_id)
                if not user_inputs:
                    continue
                feedback_relation = str(item.get("feedback_relation") or "same_anchor")
                related_layer = str(item.get("related_layer") or ("memory" if layer == "memory" else "insight"))
                related = files.get(related_layer, str(item.get("related_id", ""))) if related_layer in {"insight", "memory"} and item.get("related_id") else None
                if related and not files.is_text_feedback(related):
                    related = None
                if related:
                    merged = files.merge_text_feedback(related, item, record_trace_id, ids, user_inputs)
                    merged["trace_ids"] = list(dict.fromkeys([*merged.get("trace_ids", []), *record_trace_ids]))
                    if item.get("explicit_reconfirmation") is True:
                        merged = files.confirm_auto_promotion(merged)
                    if feedback_relation == "cross_text_support":
                        bonus = 35 if item.get("signal") == "strong" else 15
                        merged["weight"] = min(MAX_RECORD_WEIGHT, float(merged.get("weight", 0)) + bonus)
                        merged["support_count"] = int(merged.get("support_count", 1)) + 1
                    can_promote = files.can_auto_promote(merged)
                    saved = files._move(merged, "memory") if layer == "memory" and related_layer == "insight" and can_promote else files.write(related_layer, merged)
                    if feedback_relation == "cross_text_support" and saved["layer"] == "memory" and files.can_auto_promote(saved) and saved["weight"] >= PATTERN_PROMOTION_WEIGHT and saved["support_count"] >= 3:
                        files.promote_memory_to_pattern(saved, record_trace_ids)
                else:

                    user_requirements = str(item.get("user_requirements") or "").strip()
                    revision_direction = str(item.get("revision_direction") or item.get("feedback_direction") or item.get("content") or claim).strip()
                    files.write(layer, {
                        "title": item.get("title", claim), "claim": claim,
                        "content": files.render_text_feedback_content(user_requirements, revision_direction, user_inputs),
                        "user_requirements": user_requirements, "revision_direction": revision_direction,
                        "category": "reference", "domain": item.get("domain", "overall"), "kind": "text_feedback",

                        "artifact_path": str(item.get("artifact_path") or "").replace("\\", "/").lstrip("./"),
                        "artifact_revision_id": str(item.get("artifact_revision_id") or ""),
                        "anchor_excerpt": str(item.get("anchor_excerpt") or "")[:700],
                        "anchor_sha256": str(item.get("anchor_sha256") or ""),
                        "anchor_examples": [{"artifact_path": str(item.get("artifact_path") or "").replace("\\", "/").lstrip("./"), "artifact_revision_id": str(item.get("artifact_revision_id") or ""), "anchor_excerpt": str(item.get("anchor_excerpt") or "")[:700], "anchor_sha256": str(item.get("anchor_sha256") or "")}],
                        "feedback_direction": str(item.get("feedback_direction") or claim),
                        "feedback_count": 1, "feedback_history": [{"trace_id": record_trace_id, "trace_refs": trace_refs, "source_event_ids": ids, "feedback_direction": str(item.get("feedback_direction") or ""), "relation": feedback_relation}], "user_inputs": user_inputs,
                        "trace_id": record_trace_id, "trace_ids": record_trace_ids, "trace_refs": trace_refs, "source_event_ids": ids,
                        "confidence": item.get("confidence", .5), "weight": 65 if item.get("signal") == "strong" else 15, "support_count": 1,
                    })
                continue
            related_layer = str(item.get("related_layer") or ("memory" if layer == "memory" else "insight"))
            relation = str(item.get("relation") or "new")
            related = files.get(related_layer, str(item.get("related_id", ""))) if relation in {"support", "append"} and related_layer in {"insight", "memory", "pattern"} and (related_layer != "pattern" or layer == "memory") else None
            if related:
                related["trace_id"] = record_trace_id
                related["source_event_ids"] = list(dict.fromkeys([*related.get("source_event_ids", []), *ids]))
                related["trace_ids"] = list(dict.fromkeys([*related.get("trace_ids", []), *record_trace_ids]))
                related["trace_refs"] = self._merge_trace_refs(related.get("trace_refs", []), trace_refs)
                if relation == "append":
                    history = list(related.get("relation_history") or [])
                    history.append({
                        "relation": "append",
                        "title": item.get("title", claim),
                        "claim": claim,
                        "content": item.get("content", claim),
                        "trace_ids": record_trace_ids,
                        "trace_refs": trace_refs,
                        "source_event_ids": ids,
                    })
                    related["relation_history"] = history
                    files.write(related_layer, related)
                    continue
                bonus = 35 if item.get("signal") == "strong" else 15
                related["weight"] = min(MAX_RECORD_WEIGHT, float(related.get("weight", 0)) + bonus)
                related["support_count"] = int(related.get("support_count", 1)) + 1
                if item.get("explicit_reconfirmation") is True:
                    related = files.confirm_auto_promotion(related)
                can_promote = files.can_auto_promote(related)
                saved = files._move(related, "memory") if layer == "memory" and related_layer == "insight" and can_promote else files.write(related_layer, related)
                if layer == "insight" and files.can_auto_promote(saved) and saved["weight"] >= 60 and saved["support_count"] >= 3:
                    saved = files._move(saved, "memory")
                if saved["layer"] == "memory" and files.can_auto_promote(saved) and saved["weight"] >= PATTERN_PROMOTION_WEIGHT and saved["support_count"] >= 3:
                    files.promote_memory_to_pattern(saved, record_trace_ids)
            else:

                files.write(layer, {"title": item.get("title", claim), "claim": claim, "content": item.get("content", claim), "category": item.get("category", "project"), "domain": item.get("domain", "overall"), "kind": item.get("kind", ""), "trace_id": record_trace_id, "trace_ids": record_trace_ids, "trace_refs": trace_refs, "source_event_ids": ids, "confidence": item.get("confidence", .5), "weight": 65 if item.get("signal") == "strong" else 15, "support_count": 1})

        for event in agent_feedback_events:
            already_recorded = any(
                item.get("trace_id") == event_trace_ids.get(event["event_id"], trace_id) and event["event_id"] in item.get("source_event_ids", [])
                for item in files.list("insight")
            )
            if event["event_id"] not in recorded_event_ids and not already_recorded:
                trace_refs = self._trace_refs(events, [event["event_id"]], event_trace_ids.get(event["event_id"], trace_id))
                files.write("insight", {
                    "title": "核查 AskUserQuestion 调用时机",
                    "claim": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "content": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "category": "agent", "domain": "overall", "trace_id": event_trace_ids.get(event["event_id"], trace_id),
                    "trace_ids": [event_trace_ids.get(event["event_id"], trace_id)],
                    "trace_refs": trace_refs,
                    "source_event_ids": [event["event_id"]], "confidence": 0.9,
                    "weight": 15, "support_count": 1,
                })
        files.rebuild_indexes()
        for source_trace_id in source_trace_ids:
            await self.trace_store.set_trace_analysis_status(source_trace_id, "complete")
        window_summary = str(data.get("window_summary") or window_summary).strip()[:8000]
        if require_summary and not window_summary:
            raise ValueError("记忆分析未返回 window_summary")
        return window_summary

    async def analyze_window(self, window_id: str, project_id: str, branch_messages=None, _tools=None) -> None:
        """Analyze bounded local ReAct turns without inventing an aggregate Trace.

        A window is only an ordered set of raw request traces.  Each raw trace
        remains the provenance anchor for classifications and file-backed
        records.  The normal path inherits the main Agent's complete current
        context for KV-cache reuse.  If that branch fails, an independent fallback
        starts from nearby turns and can request another bounded turn range.
        """
        window = await self.trace_store.get_trace_window(window_id)
        if not window:
            return
        await self.trace_store.set_trace_window_status(window_id, "running")
        try:
            source_trace_ids = window.get("source_trace_ids", [])
            if not source_trace_ids:
                await self.trace_store.set_trace_window_status(window_id, "complete")
                return
            events = self._select_analysis_events(await self.trace_store.list_trace_window_events(window_id))
            messages = branch_messages if branch_messages is not None else window.get("messages", [])
            result = await self.analyze(
                source_trace_ids[-1], project_id, messages, _tools,
                events_override=events, source_trace_ids=source_trace_ids,
                require_summary=True,
                normalized_trajectory={
                    "trajectory": window.get("normalized_trajectory", []),
                    "provenance": window.get("trajectory_provenance", []),
                },
                window_id=window_id,
            )
            await self.trace_store.complete_trace_window_summary(
                window_id, result.get("window_summary", ""), result.get("agent_trace_id", ""),
            )
        except Exception:
            await self.trace_store.set_trace_window_status(window_id, "failed")
            raise

    async def _extract(self, prompt: str, branch_messages, *, trace_tool=None,
                       project_id: str = "", session_id: str = "", source_trace_id: str = "",
                       cached_tools: list[dict] | None = None,
                       execution_events: list[dict] | None = None,
                       fallback_mode: bool = False) -> str:
        messages = [*branch_messages, {"role": "user", "content": prompt}] if branch_messages else [{"role": "user", "content": prompt}]
        position = "memory_summary_fallback" if fallback_mode or not branch_messages else "main_loop"
        if branch_messages and not fallback_mode:
            available_tools = list(cached_tools or [])
            if trace_tool and not any(tool.get("name") == trace_tool.name for tool in available_tools):
                available_tools.append(trace_tool.get_schema())
            if not any(tool.get("name") == self.read_tool.name for tool in available_tools):
                available_tools.append(self.read_tool.get_schema())
            if not available_tools:
                return await self._chat_text(
                    messages, position, tag=":trace-fork/cached", execution_events=execution_events,
                )
            text, calls = await self._chat_with_schemas(
                messages, position, available_tools, tag=":trace-fork/cached",
                execution_events=execution_events,
            )
            if calls:
                call = calls[0]
                allowed_tool = (
                    trace_tool if trace_tool and call.tool_name == trace_tool.name
                    else self.read_tool if call.tool_name == self.read_tool.name
                    else None
                )
                if not allowed_tool:
                    raise TraceAnalysisCallError(
                        f"缓存分支调用了不允许执行的主 Agent 工具: {call.tool_name}；改用独立 turn 分析"
                    )
                return await self._complete_analysis_tool_call(
                    messages, position, allowed_tool, call, text,
                    project_id=project_id, session_id=session_id,
                    source_trace_id=source_trace_id, execution_events=execution_events,
                    tag=":trace-fork/cached/final",
                )
            return text
        if not trace_tool:
            return await self._chat_text(
                messages, position, tag=":trace-fork", execution_events=execution_events,
            )

        text, calls = await self._chat_with_schemas(
            messages, position, [trace_tool.get_schema(), self.read_tool.get_schema()],
            tag=":trace-fork", execution_events=execution_events,
        )
        if not calls:
            return text
        call = calls[0]
        allowed_tool = (
            trace_tool if call.tool_name == trace_tool.name
            else self.read_tool if call.tool_name == self.read_tool.name
            else None
        )
        if not allowed_tool:
            return text
        return await self._complete_analysis_tool_call(
            messages, position, allowed_tool, call, text,
            project_id=project_id, session_id=session_id,
            source_trace_id=source_trace_id, execution_events=execution_events,
            tag=":trace-fork/final",
        )

    async def _complete_analysis_tool_call(
        self, messages: list[dict], position: str, tool, call, text: str, *,
        project_id: str, session_id: str, source_trace_id: str,
        execution_events: list[dict] | None, tag: str,
        final_instruction: str = "",
    ) -> str:
        """Execute one read-only analyzer tool and finish the merge decision."""
        working_dir = str(Path(self.workspace_dir) / project_id)
        context = ToolContext(
            session_id=session_id, project_id=project_id,
            working_dir=working_dir, actor="trace_analyzer", source_trace_id=source_trace_id,
        )
        params = call.tool_input or {}
        if tool is self.read_tool and not tool.validate_params(params):
            result = ToolResult(success=False, error="Read 缺少必填参数 path")
        elif tool is self.read_tool and not (
            Path(context.working_dir) / str(params["path"])
        ).resolve().is_relative_to(Path(context.working_dir).resolve()):
            result = ToolResult(success=False, error="Memory/Summary Agent 只能读取当前项目目录")
        elif tool is not self.read_tool and tool.checkPermissions(params, context) != PermissionResult.ALLOW:
            result = ToolResult(success=False, error="Memory/Summary Agent 无权执行该工具调用")
        else:
            result = await tool.execute(params, context)
        self._execution_event(execution_events, "tool_result", {
            "tool": call.tool_name, "success": result.success,
            "data": result.data if result.success else "", "error": result.error,
        })
        tool_call_id = getattr(call, "tool_call_id", "") or "trace-context-call"
        messages.append(build_assistant_message(text, "", [call], 0))
        tool_content = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)
        messages.append({
            "role": "tool_result", "tool_call_id": tool_call_id,
            "content": tool_content if result.success else f"Error: {result.error}",
        })
        messages.append({"role": "user", "content": final_instruction or (
            "根据工具返回的 Trace 局部上下文或项目文件完成 Insight/Memory 的冲突、支持与合并判断。"
            "现在不得再调用工具；只输出完整最终 JSON。若证据仍不足，在 content 中明确待确认点。"
        )})
        return await self._chat_text(
            messages, position, tag=tag, execution_events=execution_events,
        )

    async def _reconcile_records(
        self, trace_id: str, project_id: str, analysis_prompt: str,
        branch_messages, candidate: dict, existing: list[dict], trace_tool,
        cached_tools: list[dict] | None, session_id: str, *,
        execution_events: list[dict] | None = None,
    ) -> dict:
        """Run a dedicated second-stage Insight/Memory reconciliation pass."""
        records = [record for record in candidate.get("records", []) if isinstance(record, dict)]
        if not records:
            return candidate
        reconcile_prompt = f'''[Insight/Memory 第二阶段合并判定]
第一阶段已经完成事实提取。现在只判断每条候选与 Existing Insight、Memory、Pattern 的关系；不要重新提取事实，不要删除候选，也不要修改 source_event_ids、layer、title、content、claim、category、domain、kind 或 signal。

对每条候选按 candidate_index 返回一个 decision：
- new：没有语义相同或同一主题下需要保留的既有记录。
- support：与既有记录表达同一规则、事实或修改方向；必须填写 related_id 和 related_layer。Insight 可支持 Insight；Memory 可支持 Insight、Memory 或 Pattern。
- append：与既有记录属于同一主题，但构成补充、修订、例外或冲突；必须填写 related_id 和 related_layer。程序会合并来源和冲突历史，但不会增加支持权重。
- text_feedback 还要返回 feedback_relation=same_anchor|cross_text_support|cross_text_conflict。
- promotion_status=manual_review 的记录只有用户本轮明确重新确认时，explicit_reconfirmation 才能为 true。

若标题不足以判断，可调用一次 Read 读取 Existing 的 file_path；若必须回看来源，可调用一次 GetTraceContext。只能使用这两个只读工具。
只输出 JSON：{{"decisions":[{{"candidate_index":0,"relation":"new|support|append","related_id":"","related_layer":"insight|memory|pattern","feedback_relation":"same_anchor|cross_text_support|cross_text_conflict","explicit_reconfirmation":false,"reason":"简要说明"}}]}}。
每条候选必须恰好有一个 decision。

Candidates:{json.dumps(records, ensure_ascii=False)}
Existing:{json.dumps(existing, ensure_ascii=False)}'''
        messages = [
            *(branch_messages or []),
            {"role": "user", "content": analysis_prompt},
            {"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False)},
            {"role": "user", "content": reconcile_prompt},
        ]
        position = "main_loop" if branch_messages else "memory_summary_fallback"
        available_tools = list(cached_tools or []) if branch_messages else []
        if trace_tool and not any(tool.get("name") == trace_tool.name for tool in available_tools):
            available_tools.append(trace_tool.get_schema())
        if not any(tool.get("name") == self.read_tool.name for tool in available_tools):
            available_tools.append(self.read_tool.get_schema())
        text, calls = await self._chat_with_schemas(
            messages, position, available_tools, tag=":trace-fork/reconcile",
            execution_events=execution_events,
        )
        if calls:
            call = calls[0]
            allowed_tool = (
                trace_tool if trace_tool and call.tool_name == trace_tool.name
                else self.read_tool if call.tool_name == self.read_tool.name
                else None
            )
            if not allowed_tool:
                raise TraceAnalysisCallError(
                    f"合并判定调用了不允许执行的工具: {call.tool_name}"
                )
            text = await self._complete_analysis_tool_call(
                messages, position, allowed_tool, call, text,
                project_id=project_id,
                session_id=session_id,
                source_trace_id=trace_id, execution_events=execution_events,
                tag=":trace-fork/reconcile/final",
                final_instruction=(
                    "根据工具结果完成 Insight/Memory 合并判定。现在不得再调用工具；"
                    "只输出包含 decisions 的 JSON，每条候选恰好一个 decision。"
                ),
            )
        reviewed = self._json(text)
        decisions = reviewed.get("decisions", []) if isinstance(reviewed, dict) else []
        by_index = {
            int(decision["candidate_index"]): decision
            for decision in decisions
            if isinstance(decision, dict) and str(decision.get("candidate_index", "")).isdigit()
        }
        reconciled = []
        for index, record in enumerate(records):
            item = dict(record)
            decision = by_index.get(index, {})
            relation = str(decision.get("relation") or "new")
            related_layer = str(decision.get("related_layer") or "")
            related_id = str(decision.get("related_id") or "")
            if relation not in {"new", "support", "append"}:
                relation = "new"
            if relation != "new" and (related_layer not in {"insight", "memory", "pattern"} or not related_id):
                relation, related_layer, related_id = "new", "", ""
            if relation != "new" and item.get("layer") == "insight" and related_layer != "insight":
                relation, related_layer, related_id = "new", "", ""
            item["relation"] = relation
            item["related_layer"] = related_layer
            item["related_id"] = related_id
            if decision.get("feedback_relation") in {
                "same_anchor", "cross_text_support", "cross_text_conflict",
            }:
                item["feedback_relation"] = decision["feedback_relation"]
            item["explicit_reconfirmation"] = decision.get("explicit_reconfirmation") is True
            reconciled.append(item)
        return {**candidate, "records": reconciled}

    async def _review_reference_feedback(self, trace_id: str, project_id: str, prompt: str,
                                         branch_messages, candidate: dict, *,
                                         execution_events: list[dict] | None = None) -> dict:
        """Let only reference-feedback extraction make one bounded RAG check."""
        if not self.rag_tool:
            return candidate

        messages = [
            *(branch_messages or []),
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False)},
            {"role": "user", "content": (
                "[文本反馈核查阶段]\\n"
                "上一轮已经给出候选 Trace 编排结果，其中包含 reference/text_feedback。现在只核查这些文本修改反馈。\\n"
                "如果候选的修改方向仍有多种合理解释，或需要借鉴相似场景的处理方式，可以调用 SearchRag 一次；否则直接输出最终 JSON。\\n"
                "SearchRag 最多调用一次，最多检索 3 个片段。资料库片段只用于理解可借鉴的写法，不能覆盖用户意见，不能照抄，"
                "不能改变权重、support_count、relation 或 feedback_relation。\\n"
                "无论是否调用，都必须只输出完整 JSON，沿用上一轮的 schema。最终 JSON 中不得保存、引用或复述资料库的片段、标题、ID 或分数；"
                "只保留用户反馈所归纳出的修改方向、观察、推测与待确认点。"
            )},
        ]
        position = "main_loop" if branch_messages else "memory_summary_fallback"
        first_text, calls = await self._chat_with_tools(messages, position, execution_events=execution_events)
        if not calls:
            return self._json(first_text) or candidate

        call = calls[0]
        if call.tool_name != self.rag_tool.name:
            return candidate
        query = str((call.tool_input or {}).get("query") or "").strip()
        if not query:
            return candidate
        result = await self.rag_tool.execute({"query": query[:500], "limit": 3}, ToolContext(
            session_id=f"trace:{trace_id}", project_id=project_id,
            working_dir=self.workspace_dir, actor="trace_analyzer", source_trace_id=trace_id,
        ))
        self._execution_event(execution_events, "tool_result", {
            "tool": call.tool_name, "success": result.success,
            "data": result.data if result.success else "", "error": result.error,
        })
        tool_call_id = getattr(call, "tool_call_id", "") or "call_0_0"
        messages.append(build_assistant_message(first_text, "", [call], 0))
        messages.append({
            "role": "tool_result", "tool_call_id": tool_call_id,
            "content": result.data if result.success else f"Error: {result.error}",
        })
        messages.append({"role": "user", "content": (
            "根据本次临时检索结果完成核查。现在不得再调用工具；只输出完整的最终 JSON。\\n"
            "资料库结果不得进入任何 JSON 字段，也不得单独增加权重。若检索不足以消除不确定性，"
            "在 user_requirements 或 revision_direction 中明确保留待确认点。"
        )})
        return self._json(await self._chat_text(
            messages, position, execution_events=execution_events,
        )) or candidate


    async def _chat_with_tools(self, messages: list[dict], position: str, *,
                               execution_events: list[dict] | None = None) -> tuple[str, list]:
        text, calls = "", []
        self._execution_event(execution_events, "llm_request", {
            "position": position, "tag": ":trace-fork/rag-review",
            "messages": messages, "tools": [self.rag_tool.get_schema()],
        })
        async for chunk in self.llm.chat(
            position=position,
            messages=messages, tools=[self.rag_tool.get_schema()], stream=False,
            tag=":trace-fork/rag-review",
        ):
            if chunk.type == "text_delta":
                text += chunk.content
                self._execution_event(execution_events, "text_delta", {"content": chunk.content})
            elif chunk.type == "thinking":
                self._execution_event(execution_events, "thinking", {"content": chunk.content})
            elif chunk.type == "tool_use" and not calls:
                calls.append(chunk)
                self._execution_event(execution_events, "tool_call", {
                    "tool": chunk.tool_name, "params": chunk.tool_input,
                    "tool_call_id": chunk.tool_call_id,
                })
            elif chunk.type == "error":
                self._execution_event(execution_events, "error", {"message": chunk.error})
                raise TraceAnalysisCallError(chunk.error or "记忆分析调用失败")
        return text, calls

    async def _chat_with_tool(self, messages: list[dict], position: str, tool, *, tag: str,
                              execution_events: list[dict] | None = None) -> tuple[str, list]:
        return await self._chat_with_schemas(
            messages, position, [tool.get_schema()], tag=tag, execution_events=execution_events,
        )

    async def _chat_with_schemas(self, messages: list[dict], position: str, tools: list[dict], *, tag: str,
                                 execution_events: list[dict] | None = None) -> tuple[str, list]:
        text, calls = "", []
        self._execution_event(execution_events, "llm_request", {
            "position": position, "tag": tag, "messages": messages, "tools": tools,
        })
        async for chunk in self.llm.chat(
            position=position, messages=messages, tools=tools, stream=False,
            tag=tag,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
                self._execution_event(execution_events, "text_delta", {"content": chunk.content})
            elif chunk.type == "thinking":
                self._execution_event(execution_events, "thinking", {"content": chunk.content})
            elif chunk.type == "tool_use" and not calls:
                calls.append(chunk)
                self._execution_event(execution_events, "tool_call", {
                    "tool": chunk.tool_name, "params": chunk.tool_input,
                    "tool_call_id": chunk.tool_call_id,
                })
            elif chunk.type == "error":
                self._execution_event(execution_events, "error", {"message": chunk.error})
                raise TraceAnalysisCallError(chunk.error or "记忆分析调用失败")
        return text, calls

    async def _chat_text(self, messages: list[dict], position: str, *, tag: str = ":trace-fork/rag-review",
                         execution_events: list[dict] | None = None) -> str:
        text = ""
        self._execution_event(execution_events, "llm_request", {
            "position": position, "tag": tag, "messages": messages, "tools": [],
        })
        async for chunk in self.llm.chat(
            position=position,
            messages=messages, tools=None, stream=False,
            tag=tag,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
                self._execution_event(execution_events, "text_delta", {"content": chunk.content})
            elif chunk.type == "thinking":
                self._execution_event(execution_events, "thinking", {"content": chunk.content})
            elif chunk.type == "error":
                self._execution_event(execution_events, "error", {"message": chunk.error})
                raise TraceAnalysisCallError(chunk.error or "记忆分析调用失败")
        return text

    @staticmethod
    def _select_analysis_events(events: list[dict], radius: int = 2) -> list[dict]:
        """Keep at most five neighboring ReAct turns around one useful anchor per Trace."""
        annotated = TraceStore.annotate_event_turns(events)
        grouped: dict[str, list[dict]] = {}
        for event in annotated:
            grouped.setdefault(str(event.get("trace_id") or ""), []).append(event)

        selected: list[dict] = []
        for trace_events in grouped.values():
            checkpoint_events = [
                event for event in trace_events
                if event.get("event_type") == "tool_call"
                and (event.get("payload") or {}).get("tool") == "CreateTraceCheckpoint"
            ]
            review_events = [
                event for event in trace_events
                if (
                    str((event.get("payload") or {}).get("preset") or "") == "reviewer"
                    or str((event.get("payload") or {}).get("workflow") or "") == "auto_review"
                    or FileTraceAnalyzer._is_legacy_review_summary(
                        str((event.get("payload") or {}).get("content") or "")
                    )
                )
            ]
            answer_events = [event for event in trace_events if event.get("event_type") == "user_answer"]
            user_events = [event for event in trace_events if event.get("event_type") == "user_message"]
            anchors = checkpoint_events or review_events or answer_events or user_events or trace_events[-1:]
            anchor = anchors[-1] if checkpoint_events or review_events or answer_events else anchors[0]
            anchor_turn = int(anchor.get("trace_turn") or 1)
            low, high = max(1, anchor_turn - max(0, radius)), anchor_turn + max(0, radius)
            chosen = [
                event for event in trace_events
                if low <= int(event.get("trace_turn") or 1) <= high
            ]
            selected.extend(chosen or trace_events[:40])
        return selected

    @staticmethod
    def _event_user_content(event: dict) -> str:
        payload = event.get("payload") or {}
        if event.get("event_type") == "user_answer":
            answers = payload.get("answers")
            if isinstance(answers, dict):
                return "\n".join(f"{key}: {value}" for key, value in answers.items()).strip()
            return str(answers or "").strip()
        return str(payload.get("content") or "").strip()

    @staticmethod
    def _event_source_content(event: dict) -> str:
        if event.get("event_type") in {"user_message", "user_answer"}:
            return FileTraceAnalyzer._event_user_content(event)
        payload = event.get("payload") or {}
        for key in ("content", "message", "result", "data", "error"):
            if payload.get(key):
                return str(payload[key]).strip()
        reason = (payload.get("params") or {}).get("reason") if isinstance(payload.get("params"), dict) else ""
        return str(reason or "").strip()

    @staticmethod
    def _match_source_event_id(events: list[dict], excerpt: str) -> str:
        needle = " ".join(excerpt.split()).casefold()
        if not needle:
            return ""
        for event in reversed(events):
            content = " ".join(FileTraceAnalyzer._event_source_content(event).split()).casefold()
            if content and (needle in content or content in needle):
                return str(event.get("event_id") or "")
        return ""

    @staticmethod
    def _default_source_event_ids(events: list[dict], source_kind: str = "") -> list[str]:
        if not events:
            return []
        if source_kind == "error":
            candidates = [event for event in events if event.get("event_type") == "error"]
        elif source_kind == "review_issue":
            candidates = [
                event for event in events
                if str((event.get("payload") or {}).get("preset") or "") == "reviewer"
                or str((event.get("payload") or {}).get("workflow") or "") == "auto_review"
                or FileTraceAnalyzer._is_legacy_review_summary(
                    str((event.get("payload") or {}).get("content") or "")
                )
            ]
        else:
            candidates = [
                event for event in events if event.get("event_type") in {"user_message", "user_answer"}
            ]
        if not candidates:
            candidates = [
                event for event in events
                if event.get("event_type") == "tool_call"
                and (event.get("payload") or {}).get("tool") == "CreateTraceCheckpoint"
            ]
        candidate = (candidates or events)[-1]
        event_id = str(candidate.get("event_id") or "")
        return [event_id] if event_id else []

    @staticmethod
    def _trace_refs(events: list[dict], event_ids: list[str], default_trace_id: str) -> list[dict]:
        requested = set(event_ids)
        grouped: dict[tuple[str, int], list[str]] = {}
        for event in events:
            event_id = str(event.get("event_id") or "")
            if event_id not in requested:
                continue
            key = (str(event.get("trace_id") or default_trace_id), int(event.get("trace_turn") or 1))
            grouped.setdefault(key, []).append(event_id)
        return [
            {"trace_id": trace_id, "turn": turn, "source_event_ids": list(dict.fromkeys(ids))}
            for (trace_id, turn), ids in grouped.items()
        ]

    @staticmethod
    def _merge_trace_refs(existing: list[dict], incoming: list[dict]) -> list[dict]:
        grouped: dict[tuple[str, int], list[str]] = {}
        for ref in [*(existing or []), *(incoming or [])]:
            if not isinstance(ref, dict) or not ref.get("trace_id"):
                continue
            try:
                key = (str(ref["trace_id"]), int(ref.get("turn") or 1))
            except (TypeError, ValueError):
                continue
            grouped.setdefault(key, []).extend(str(value) for value in ref.get("source_event_ids", []) if value)
        return [
            {"trace_id": trace_id, "turn": turn, "source_event_ids": list(dict.fromkeys(ids))}
            for (trace_id, turn), ids in grouped.items()
        ]

    @staticmethod
    def _user_inputs(events: list[dict], event_ids: list[str], trace_id: str) -> list[dict]:
        selected = []
        requested = set(event_ids)
        for event in events:
            if event.get("event_id") not in requested or event.get("event_type") not in {"user_message", "user_answer"}:
                continue
            content = FileTraceAnalyzer._event_user_content(event)
            if content:
                selected.append({
                    "trace_id": str(event.get("trace_id") or trace_id),
                    "turn": int(event.get("trace_turn") or 1),
                    "event_id": event["event_id"],
                    "content": content,
                })
        return selected

    @staticmethod
    def _keep_event(event: dict) -> bool:
        event_type = str(event.get("event_type", ""))
        return event_type in {"user_message", "user_answer", "question_ask", "assistant_turn", "error", "tool_call", "tool_result"} or "tool_" in event_type or event_type.endswith("subagent_done")

    @staticmethod
    def _event_summary(event: dict) -> dict:
        payload = event.get("payload", {}) or {}
        result = {
            "event_id": event["event_id"], "trace_id": event.get("trace_id", ""),
            "turn": event.get("trace_turn", 1), "type": event.get("event_type", ""),
            "actor": event.get("actor", ""),
            "timestamp": event.get("created_at") or "1970-01-01T00:00:00Z",
        }
        if event.get("parent_event_id"):
            result["parent_event_id"] = event["parent_event_id"]
        content = payload.get("content") or payload.get("message")
        if content:
            result["content"] = str(content)[:1200]
            # Historical imports may only retain the main agent's explicit
            # reviewer summary instead of the original reviewer sub-agent
            # event.  Preserve the same extraction semantics for that shape.
            if event.get("event_type") == "assistant_turn" and FileTraceAnalyzer._is_legacy_review_summary(str(content)):
                result["preset"] = "reviewer"
                result["workflow"] = "auto_review"
                result["legacy_summary"] = True
        if payload.get("tool"):
            result["tool"] = payload["tool"]
        if payload.get("reasoning_content"):
            result["reasoning_content"] = str(payload["reasoning_content"])[:2400]
        if "success" in payload:
            result["success"] = bool(payload["success"])
        if payload.get("error"):
            result["error"] = str(payload["error"])[:1400]
        if isinstance(payload.get("params"), dict):
            params = payload["params"]
            result["params"] = {key: (str(value)[:1200] if key in {"path", "old_string", "new_string", "expected_revision_id"} else value)
                                for key, value in params.items() if key in {"path", "old_string", "new_string", "expected_revision_id", "reason"}}
        if payload.get("data"):
            result["data"] = str(payload["data"])[:1400]
        if payload.get("result"):
            result["result"] = str(payload["result"])[:6000]
        if payload.get("answers"):
            result["answers"] = payload["answers"]
        if payload.get("questions"):
            result["questions"] = payload["questions"]
        for key in ("preset", "workflow", "run_id", "parent_run_id"):
            if payload.get(key):
                result[key] = payload[key]
        if isinstance(payload.get("revision_events"), list):
            result["revision_events"] = payload["revision_events"]
        return result

    @staticmethod
    def _is_legacy_review_summary(content: str) -> bool:
        normalized = content.lower()
        return "审阅结论" in content and "reviewer" in normalized

    @staticmethod
    def _checkpoint_reasons(events: list[dict]) -> list[dict]:
        reasons = []
        for event in events:
            payload = event.get("payload") or {}
            if event.get("event_type") != "tool_call" or payload.get("tool") != "CreateTraceCheckpoint":
                continue
            reason = str((payload.get("params") or {}).get("reason") or "").strip()
            if reason:
                reasons.append({"event_id": event.get("event_id", ""), "reason": reason})
        return reasons

    @staticmethod
    def _json(text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try: return json.loads(clean[clean.find("{"):clean.rfind("}") + 1])
        except (ValueError, json.JSONDecodeError): return {}

    @staticmethod
    def _valid_analysis_payload(data: dict, require_summary: bool) -> bool:
        if not isinstance(data, dict):
            return False
        if not isinstance(data.get("items"), list) or not isinstance(data.get("records"), list):
            return False
        return not require_summary or bool(str(data.get("window_summary") or "").strip())

    @staticmethod
    def _has_reference_feedback(data: dict) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("category") == "reference"
            and item.get("kind") == "text_feedback"
            for item in data.get("records", [])
        )

    @staticmethod
    def _is_agent_feedback(content: str) -> bool:
        text = content.lower()
        is_questioning = any(marker in content for marker in ("为什么", "为何", "怎么会", "不应该", "有问题", "不对"))
        mentions_agent_operation = "askuserquestion" in text or any(word in text for word in ("工具", "tool", "agent", "subagent"))
        return "askuserquestion" in text or (is_questioning and mentions_agent_operation)


class FilePatternContextProvider:
    def __init__(self, workspace_dir: str): self.workspace_dir = workspace_dir
    async def render(self, project_id: str, limit: int = 16) -> str:
        records = [item for item in FileLifecycleStore(self.workspace_dir, project_id).list("pattern") if item.get("category") != "agent"]
        return "\n\n".join(
            f"### {x.get('title', x.get('claim', ''))}\n{x.get('content', x.get('claim', ''))}"
            for x in records[:limit]
        )
