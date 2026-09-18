"""LLM extraction of file-backed Evidence, Memory and Pattern records."""
import json
import re

from novelagent.core.llm_turn import build_assistant_message
from novelagent.trace.file_lifecycle import FileLifecycleStore, MAX_RECORD_WEIGHT, PATTERN_PROMOTION_WEIGHT
from novelagent.trace.store import TraceStore
from novelagent.tools.base import ToolContext
from novelagent.tools.get_trace_context import GetTraceContextTool
from novelagent.tools.search_rag import SearchRagTool


class FileTraceAnalyzer:
    def __init__(self, llm_client, workspace_dir: str, trace_store: TraceStore, embedding_gate=None, rag_store=None):
        self.llm, self.workspace_dir, self.trace_store, self.embedding_gate = llm_client, workspace_dir, trace_store, embedding_gate
        self.rag_tool = SearchRagTool(rag_store) if rag_store else None

    async def analyze(self, trace_id: str, project_id: str, branch_messages=None, _tools=None, *,
                      events_override: list[dict] | None = None,
                      source_trace_ids: list[str] | None = None) -> None:
        trace = await self.trace_store.get_trace(trace_id)
        if not trace or trace.get("analysis_status") == "skipped": return
        events = events_override if events_override is not None else await self.trace_store.list_events(trace_id, limit=500)
        source_trace_ids = list(dict.fromkeys(source_trace_ids or [trace_id]))
        event_trace_ids = {str(event.get("event_id")): str(event.get("trace_id") or trace_id) for event in events}
        if self.embedding_gate:
            inputs = [str(e.get("payload", {}).get("content", "")) for e in events if e["event_type"] == "user_message"]
            if (await self.embedding_gate.evaluate(inputs)).skip:
                for source_trace_id in source_trace_ids:
                    await self.trace_store.set_trace_operation(source_trace_id, "routine", "skipped")
                return
        files = FileLifecycleStore(self.workspace_dir, project_id)
        valid = {e["event_id"] for e in events}
        all_records = [(layer, record) for layer in ("evidence", "memory", "pattern") for record in files.list(layer)]
        context = [{"id": x["id"], "title": x.get("title", x.get("claim", "")), "layer": layer, "category": x.get("category", "project"), "domain": x.get("domain", "overall"), "kind": x.get("kind", ""), "weight": x.get("weight", 0), "support_count": x.get("support_count", 1), "promotion_status": x.get("promotion_status", "auto"), "downgraded_from": x.get("downgraded_from", "")} for layer, x in all_records[:240]]
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
        prompt = f'''[后台 Trace 分支命令]\n你从当前主对话的末尾分叉。不要回复用户、不要续写，只分析原始 Trace：{json.dumps(source_trace_ids, ensure_ascii=False)}。每个 Trace event 都携带 trace_id；记录必须通过 source_event_ids 保留真实来源。\n只输出 JSON：{{"items":[{{"event_id":"","type":"error|correction|confirmation|feedback","summary":"","confidence":0.5}}],"records":[{{"layer":"evidence|memory","category":"user|project|reference","domain":"writing|outline|overall","title":"不超过40字的简要标题","content":"具体事实、约束、适用条件和必要背景","claim":"与 title 一致","source_event_ids":[""],"signal":"weak|strong","relation":"new|support|append","related_id":"","confidence":0.5,"kind":"text_feedback 时填写","artifact_path":"可从工具事件推断时填写","artifact_revision_id":"","anchor_excerpt":"用户引用或评价的原文，最多700字","anchor_sha256":"工具事件提供时填写","user_requirements":"综合历次反馈后，用户希望该段达到什么效果以及明确禁止什么","revision_direction":"后续应如何修改，包括措辞、情节、人物表现和节奏等方向","feedback_direction":"兼容字段，填写当前建议的修改方向","feedback_relation":"same_anchor|cross_text_support|cross_text_conflict","explicit_reconfirmation":false}}]}}。\n不记录：普通闲聊、纯工具调用、一次性命令，例如“继续写作”“读取文件”。\nEvidence 是尚不足以长期生效的弱证据，例如用户单次说“这章对话节奏有点慢”；它保留来源，等待后续相似反馈支持。\nMemory 是明确、可复用的长期偏好、修正或约束，例如“以后打斗必须突出空间关系”，或“把林深的初始性格改为恐惧回避型”；这类记录 layer=memory、signal=strong。\n凡是由用户 correction 事件得出的记录，source_event_ids 必须包含该 user_message 的 event_id，且 layer=memory、signal=strong；不要用后续 assistant_turn 替代该证据。support 必须引用 Existing 的 memory id。\n\n文本修改反馈的特殊规则：当用户粘贴、引用或明确评价某段小说/大纲原文时，写 category=reference、kind=text_feedback，并从工具事件补足 artifact_path、修订和锚点。程序会根据 source_event_ids 保存用户完整原始输入；不要复述或截断用户原话。user_requirements 必须综合历次反馈，提炼用户希望该段达到的效果和明确禁止项；revision_direction 必须总结后续具体修改方向，而不是生成原文摘要。与同一锚点/同一段原文的重复意见使用 relation=append、feedback_relation=same_anchor：只追加历史并更新这两个字段，绝不加分。仅当是不同但相似的文本，且用户修改方向能相互验证时，relation=support、feedback_relation=cross_text_support，才允许为 Existing text_feedback 加分。相同/相似文本但方向相反时用 append、feedback_relation=cross_text_conflict：记录反例与不确定性，不加分。是否同锚点、是否跨文本可迁移由你根据原文、用户引用和 Existing 自行判断。\nTrace events:{json.dumps(event_view, ensure_ascii=False)}\nExisting:{json.dumps(context, ensure_ascii=False)}\nExisting text feedback (full):{json.dumps(feedback_context, ensure_ascii=False)}'''

        prompt = prompt.replace("user|project|reference", "user|project|reference|agent")
        prompt += "\n单次质疑异常工具或 Agent 行为（例如‘为什么调用 AskUserQuestion 工具’）属于 Evidence，category=agent；只有明确要求长期遵循的工具流程才属于 Memory。agent 分类只用于后续 Agent 优化，不能作为项目写作或大纲记忆。"
        prompt += "\n自动审阅事件（workflow=auto_review、preset=reviewer）的 result 是审阅报告。报告中明确指出、并且未来写作可复用地检查或避免的写作缺陷，使用 category=project、domain=writing、kind=review_issue。单次审阅发现只能新建 layer=evidence、signal=weak；同一种底层错误在不同 Trace 或不同正文修订中再次出现时，必须 relation=support 并引用 Existing 中对应的 review_issue。不要记录一次性错字、仅适用于当前情节的修改项、审阅报告中的肯定项，也不要把 polisher 的修改说明当作新证据。content 应写清错误表现、适用条件、判断方法和改进方向。程序会在重复证据达到阈值后自动晋级为 Memory，再继续积累为 Pattern。"
        prompt += "\n决定写入前必须先根据 Existing 选择候选：Evidence 只能与 Evidence 合并，低幅加分；Memory 可以与 Memory 或 Evidence 合并，高幅加分。Memory 候选还必须查看 Existing 中的 Pattern：若语义相同，relation=support、related_layer=pattern，直接为该 Pattern 加分，不重复新建 Pattern。records 可额外返回 related_layer=evidence|memory|pattern。被容量挤出的旧 Memory 在 Evidence 中保留 origin_memory_id，新的 Memory 可以将其重新提升。"
        prompt += "\npromotion_status=manual_review 表示用户曾手动降级并持有不同意见。后续证据仍可追加，但程序禁止自动晋级。只有用户本轮明确重新确认该记录可作为更高等级规则时，才设置 explicit_reconfirmation=true；不得根据普通支持或相似表述自行解除。"
        prompt += "\n每条 records 还必须提供 title 和 content：title 是不超过 40 字、可用于索引和列表的简要标题；content 是可独立理解的具体事实、约束、适用条件和必要背景。claim 保持与 title 一致，用于兼容旧记录。text_feedback 必须提供 user_requirements 和 revision_direction；程序会保存 source_event_ids 对应的完整用户原始输入并将三部分一起落盘。若仅凭这些原始输入、当前 Trace 和已有记录无法判断，可调用一次 GetTraceContext；可查询当前 Session 内的任意 Trace。获得工具结果后必须输出完整 JSON。"

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
        data = self._json(await self._extract(
            prompt, branch_messages, trace_tool=trace_tool, project_id=project_id,
            session_id=str(trace.get("session_id", "")), source_trace_id=trace_id,
        ))
        checkpoint_reasons = self._checkpoint_reasons(events)
        if checkpoint_reasons and not data.get("items") and not data.get("records"):
            retry_prompt = f'''[Trace 记忆提取重试]\n上一轮返回了空结果，但当前 Trace 明确调用了 CreateTraceCheckpoint。必须重新判断，不能返回空对象。\n只输出与主 schema 相同的 JSON：{{"items":[],"records":[]}}。\n用户明确指出具体文本问题时，items 至少包含 correction 或 feedback；records 必须生成 category=reference、domain=writing、kind=text_feedback、layer=memory、signal=strong 的记录，并让 source_event_ids 指向 user_message。\n若 checkpoint 并非写作反馈，也必须给出对应分类；只有确认不应长期记录时 records 才可为空。\nCheckpoint reasons:{json.dumps(checkpoint_reasons, ensure_ascii=False)}\nTrace events:{json.dumps(event_view, ensure_ascii=False)}\nExisting:{json.dumps(context, ensure_ascii=False)}'''
            data = self._json(await self._extract(
                retry_prompt, None, trace_tool=trace_tool, project_id=project_id,
                session_id=str(trace.get("session_id", "")), source_trace_id=trace_id,
            ))
        if checkpoint_reasons and not data.get("items") and not data.get("records"):
            raise ValueError("CreateTraceCheckpoint 已触发，但 Trace 判别连续返回空结果")
        if self._has_reference_feedback(data):
            data = await self._review_reference_feedback(trace_id, project_id, prompt, branch_messages, data)
        classifications = [x for x in data.get("items", []) if isinstance(x, dict) and x.get("event_id") in valid and x.get("type") in {"error","correction","confirmation","feedback"}]
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
            claim = str(item.get("claim", "")).strip()
            layer = item.get("layer")
            if layer not in {"evidence", "memory"} or not ids or not claim: continue
            record_trace_ids = list(dict.fromkeys(event_trace_ids.get(event_id, trace_id) for event_id in ids))
            record_trace_id = record_trace_ids[-1]
            recorded_event_ids.update(ids)
            if any(event["event_id"] in ids for event in agent_feedback_events):
                item["category"] = "agent"
            # A concrete user correction is durable by definition.  The model
            # still writes the claim and scope, while the lifecycle boundary
            # prevents it from being stranded as weak one-off evidence.
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
                related_layer = str(item.get("related_layer") or ("memory" if layer == "memory" else "evidence"))
                related = files.get(related_layer, str(item.get("related_id", ""))) if related_layer in {"evidence", "memory"} and item.get("related_id") else None
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
                    saved = files._move(merged, "memory") if layer == "memory" and related_layer == "evidence" and can_promote else files.write(related_layer, merged)
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
                        "feedback_count": 1, "feedback_history": [{"trace_id": record_trace_id, "source_event_ids": ids, "feedback_direction": str(item.get("feedback_direction") or ""), "relation": feedback_relation}], "user_inputs": user_inputs,
                        "trace_id": record_trace_id, "trace_ids": record_trace_ids, "source_event_ids": ids,
                        "confidence": item.get("confidence", .5), "weight": 65 if item.get("signal") == "strong" else 15, "support_count": 1,
                    })
                continue
            related_layer = str(item.get("related_layer") or ("memory" if layer == "memory" else "evidence"))
            related = files.get(related_layer, str(item.get("related_id", ""))) if item.get("relation") == "support" and related_layer in {"evidence", "memory", "pattern"} and (related_layer != "pattern" or layer == "memory") else None
            if related:
                bonus = 35 if item.get("signal") == "strong" else 15
                related["weight"] = min(MAX_RECORD_WEIGHT, float(related.get("weight", 0)) + bonus)
                related["support_count"] = int(related.get("support_count", 1)) + 1
                related["trace_id"] = record_trace_id
                related["source_event_ids"] = list(dict.fromkeys([*related.get("source_event_ids", []), *ids]))
                related["trace_ids"] = list(dict.fromkeys([*related.get("trace_ids", []), *record_trace_ids]))
                if item.get("explicit_reconfirmation") is True:
                    related = files.confirm_auto_promotion(related)
                can_promote = files.can_auto_promote(related)
                saved = files._move(related, "memory") if layer == "memory" and related_layer == "evidence" and can_promote else files.write(related_layer, related)
                if layer == "evidence" and files.can_auto_promote(saved) and saved["weight"] >= 60 and saved["support_count"] >= 3:
                    saved = files._move(saved, "memory")
                if saved["layer"] == "memory" and files.can_auto_promote(saved) and saved["weight"] >= PATTERN_PROMOTION_WEIGHT and saved["support_count"] >= 3:
                    files.promote_memory_to_pattern(saved, record_trace_ids)
            else:

                files.write(layer, {"title": item.get("title", claim), "claim": claim, "content": item.get("content", claim), "category": item.get("category", "project"), "domain": item.get("domain", "overall"), "kind": item.get("kind", ""), "trace_id": record_trace_id, "trace_ids": record_trace_ids, "source_event_ids": ids, "confidence": item.get("confidence", .5), "weight": 65 if item.get("signal") == "strong" else 15, "support_count": 1})

        for event in agent_feedback_events:
            already_recorded = any(
                item.get("trace_id") == event_trace_ids.get(event["event_id"], trace_id) and event["event_id"] in item.get("source_event_ids", [])
                for item in files.list("evidence")
            )
            if event["event_id"] not in recorded_event_ids and not already_recorded:
                files.write("evidence", {
                    "title": "核查 AskUserQuestion 调用时机",
                    "claim": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "content": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "category": "agent", "domain": "overall", "trace_id": event_trace_ids.get(event["event_id"], trace_id),
                    "trace_ids": [event_trace_ids.get(event["event_id"], trace_id)],
                    "source_event_ids": [event["event_id"]], "confidence": 0.9,
                    "weight": 15, "support_count": 1,
                })
        files.rebuild_indexes()
        for source_trace_id in source_trace_ids:
            await self.trace_store.set_trace_analysis_status(source_trace_id, "complete")

    async def analyze_window(self, window_id: str, project_id: str, branch_messages=None, _tools=None) -> None:
        """Analyze a five-turn window without inventing a second aggregate Trace.

        A window is only an ordered set of raw request traces.  Each raw trace
        remains the provenance anchor for classifications and file-backed
        records; the shared snapshot gives every extraction call the same
        surrounding conversation context.
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
            events = await self.trace_store.list_trace_window_events(window_id)
            messages = branch_messages if branch_messages is not None else window.get("messages", [])
            await self.analyze(
                source_trace_ids[-1], project_id, messages, _tools,
                events_override=events, source_trace_ids=source_trace_ids,
            )
            await self.trace_store.set_trace_window_status(window_id, "complete")
        except Exception:
            await self.trace_store.set_trace_window_status(window_id, "failed")
            raise

    async def _extract(self, prompt: str, branch_messages, *, trace_tool=None,
                       project_id: str = "", session_id: str = "", source_trace_id: str = "") -> str:
        messages = [*branch_messages, {"role": "user", "content": prompt}] if branch_messages else [{"role": "user", "content": prompt}]
        position = "main_loop" if branch_messages else "auto_memory"
        if not trace_tool:
            return await self._chat_text(messages, position, tag=":trace-fork")

        text, calls = await self._chat_with_tool(messages, position, trace_tool, tag=":trace-fork")
        if not calls:
            return text
        call = calls[0]
        if call.tool_name != trace_tool.name:
            return text
        result = await trace_tool.execute(call.tool_input or {}, ToolContext(
            session_id=session_id, project_id=project_id,
            working_dir=self.workspace_dir, actor="trace_analyzer", source_trace_id=source_trace_id,
        ))
        tool_call_id = getattr(call, "tool_call_id", "") or "trace-context-call"
        messages.append(build_assistant_message(text, "", [call], 0))
        tool_content = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)
        messages.append({
            "role": "tool_result", "tool_call_id": tool_call_id,
            "content": tool_content if result.success else f"Error: {result.error}",
        })
        messages.append({"role": "user", "content": (
            "根据工具返回的历史 Trace 局部上下文完成判断。现在不得再调用工具；"
            "只输出完整最终 JSON。若证据仍不足，在 content 中明确待确认点。"
        )})
        return await self._chat_text(messages, position, tag=":trace-fork/final")

    async def _review_reference_feedback(self, trace_id: str, project_id: str, prompt: str,
                                         branch_messages, candidate: dict) -> dict:
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
        position = "main_loop" if branch_messages else "auto_memory"
        first_text, calls = await self._chat_with_tools(messages, position)
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
        return self._json(await self._chat_text(messages, position)) or candidate


    async def _chat_with_tools(self, messages: list[dict], position: str) -> tuple[str, list]:
        text, calls = "", []
        async for chunk in self.llm.chat(
            position=position,
            messages=messages, tools=[self.rag_tool.get_schema()], stream=False,
            tag=":trace-fork/rag-review", max_tokens=4096,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
            elif chunk.type == "tool_use" and not calls:
                calls.append(chunk)
        return text, calls

    async def _chat_with_tool(self, messages: list[dict], position: str, tool, *, tag: str) -> tuple[str, list]:
        text, calls = "", []
        async for chunk in self.llm.chat(
            position=position, messages=messages, tools=[tool.get_schema()], stream=False,
            tag=tag, max_tokens=4096,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
            elif chunk.type == "tool_use" and not calls:
                calls.append(chunk)
        return text, calls

    async def _chat_text(self, messages: list[dict], position: str, *, tag: str = ":trace-fork/rag-review") -> str:
        text = ""
        async for chunk in self.llm.chat(
            position=position,
            messages=messages, tools=None, stream=False,
            tag=tag, max_tokens=4096,
        ):
            if chunk.type == "text_delta":
                text += chunk.content
        return text

    @staticmethod
    def _user_inputs(events: list[dict], event_ids: list[str], trace_id: str) -> list[dict]:
        selected = []
        requested = set(event_ids)
        for event in events:
            if event.get("event_id") not in requested or event.get("event_type") != "user_message":
                continue
            content = str((event.get("payload") or {}).get("content") or "").strip()
            if content:
                selected.append({
                    "trace_id": str(event.get("trace_id") or trace_id),
                    "event_id": event["event_id"],
                    "content": content,
                })
        return selected

    @staticmethod
    def _keep_event(event: dict) -> bool:
        event_type = str(event.get("event_type", ""))
        return event_type in {"user_message", "assistant_turn", "error", "tool_call", "tool_result"} or "tool_" in event_type or event_type.endswith("subagent_done")

    @staticmethod
    def _event_summary(event: dict) -> dict:
        payload = event.get("payload", {}) or {}
        result = {"event_id": event["event_id"], "trace_id": event.get("trace_id", ""), "type": event.get("event_type", "")}
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
        if isinstance(payload.get("params"), dict):
            params = payload["params"]
            result["params"] = {key: (str(value)[:1200] if key in {"path", "old_string", "new_string", "expected_revision_id"} else value)
                                for key, value in params.items() if key in {"path", "old_string", "new_string", "expected_revision_id", "reason"}}
        if payload.get("data"):
            result["data"] = str(payload["data"])[:1400]
        if payload.get("result"):
            result["result"] = str(payload["result"])[:6000]
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
