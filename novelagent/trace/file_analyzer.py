"""LLM extraction of file-backed Evidence, Memory and Pattern records."""
import json
import re

from novelagent.trace.file_lifecycle import FileLifecycleStore, MAX_RECORD_WEIGHT, PATTERN_PROMOTION_WEIGHT
from novelagent.trace.store import TraceStore


class FileTraceAnalyzer:
    def __init__(self, llm_client, workspace_dir: str, trace_store: TraceStore, embedding_gate=None):
        self.llm, self.workspace_dir, self.trace_store, self.embedding_gate = llm_client, workspace_dir, trace_store, embedding_gate

    async def analyze(self, trace_id: str, project_id: str, branch_messages=None, _tools=None) -> None:
        trace = await self.trace_store.get_trace(trace_id)
        if not trace or trace.get("analysis_status") == "skipped": return
        events = await self.trace_store.list_events(trace_id, limit=500)
        if self.embedding_gate:
            inputs = [str(e.get("payload", {}).get("content", "")) for e in events if e["event_type"] == "user_message"]
            if (await self.embedding_gate.evaluate(inputs)).skip:
                await self.trace_store.set_trace_operation(trace_id, "routine", "skipped"); return
        files = FileLifecycleStore(self.workspace_dir, project_id)
        valid = {e["event_id"] for e in events}
        context = [{"id": x["id"], "title": x.get("title", x.get("claim", "")), "layer": layer, "domain": x.get("domain", "overall"), "weight": x.get("weight", 0), "support_count": x.get("support_count", 1), "downgraded_from": x.get("downgraded_from", "")} for layer in ("evidence", "memory", "pattern") for x in files.list(layer)[:80]]
        event_view = [{"event_id": e["event_id"], "type": e["event_type"], "content": str(e.get("payload", {}).get("content") or e.get("payload", {}).get("message") or "")[:1200]} for e in events if e["event_type"] in {"user_message", "assistant_turn", "error", "tool_result"}]
        prompt = f'''[后台 Trace 分支命令]\n你从当前主对话的末尾分叉。不要回复用户、不要续写、不要调用工具，只分析 Trace `{trace_id}`。\n只输出 JSON：{{"items":[{{"event_id":"","type":"error|correction|confirmation|feedback","summary":"","confidence":0.5}}],"records":[{{"layer":"evidence|memory","category":"user|project|reference","domain":"writing|outline|overall","title":"不超过40字的简要标题","content":"具体事实、约束、适用条件和必要背景","claim":"与 title 一致","source_event_ids":[""],"signal":"weak|strong","relation":"new|support","related_id":"","confidence":0.5}}]}}。\n不记录：普通闲聊、纯工具调用、一次性命令，例如“继续写作”“读取文件”。\nEvidence 是尚不足以长期生效的弱证据，例如用户单次说“这章对话节奏有点慢”；它保留来源，等待后续相似反馈支持。\nMemory 是明确、可复用的长期偏好、修正或约束，例如“以后打斗必须突出空间关系”，或“把林深的初始性格改为恐惧回避型”；这类记录 layer=memory、signal=strong。\n凡是由用户 correction 事件得出的记录，source_event_ids 必须包含该 user_message 的 event_id，且 layer=memory、signal=strong；不要用后续 assistant_turn 替代该证据。support 必须引用 Existing 的 memory id。\nTrace events:{json.dumps(event_view, ensure_ascii=False)}\nExisting:{json.dumps(context, ensure_ascii=False)}'''
        prompt = prompt.replace("user|project|reference", "user|project|reference|agent")
        prompt += "\n单次质疑异常工具或 Agent 行为（例如‘为什么调用 AskUserQuestion 工具’）属于 Evidence，category=agent；只有明确要求长期遵循的工具流程才属于 Memory。agent 分类只用于后续 Agent 优化，不能作为项目写作或大纲记忆。"
        prompt += "\n决定写入前必须先根据 Existing 选择候选：Evidence 只能与 Evidence 合并，低幅加分；Memory 可以与 Memory 或 Evidence 合并，高幅加分。Memory 候选还必须查看 Existing 中的 Pattern：若语义相同，relation=support、related_layer=pattern，直接为该 Pattern 加分，不重复新建 Pattern。records 可额外返回 related_layer=evidence|memory|pattern。被容量挤出的旧 Memory 在 Evidence 中保留 origin_memory_id，新的 Memory 可以将其重新提升。"
        prompt += "\nExisting 的 downgraded_from 表示用户曾手动降级：该条可能不符合当前偏好或需要重新验证。除非用户本轮明确重新确认，只能用 signal=weak 的低幅证据支持它，不能直接恢复高优先级。"
        prompt += "\n每条 records 还必须提供 title 和 content：title 是不超过 40 字、可用于索引和列表的简要标题；content 是可独立理解的具体事实、约束、适用条件和必要背景。claim 保持与 title 一致，用于兼容旧记录。"
        text = ""
        messages = [*branch_messages, {"role": "user", "content": prompt}] if branch_messages else [{"role": "user", "content": prompt}]
        async for chunk in self.llm.chat(position="main_loop" if branch_messages else "auto_memory", messages=messages, tools=None, stream=False, tag=":trace-fork", max_tokens=4096):
            if chunk.type == "text_delta": text += chunk.content
        data = self._json(text)
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
        await self.trace_store.save_trace_classification(trace_id, classifications)
        correction_ids = {item["event_id"] for item in classifications if item["type"] == "correction"}
        recorded_event_ids: set[str] = set()
        for item in data.get("records", []):
            if not isinstance(item, dict): continue
            ids = [str(x) for x in item.get("source_event_ids", []) if str(x) in valid]
            claim = str(item.get("claim", "")).strip()
            layer = item.get("layer")
            if layer not in {"evidence", "memory"} or not ids or not claim: continue
            recorded_event_ids.update(ids)
            if any(event["event_id"] in ids for event in agent_feedback_events):
                item["category"] = "agent"
            # A concrete user correction is durable by definition.  The model
            # still writes the claim and scope, while the lifecycle boundary
            # prevents it from being stranded as weak one-off evidence.
            if correction_ids.intersection(ids):
                layer = "memory"
                item["signal"] = "strong"
            related_layer = str(item.get("related_layer") or ("memory" if layer == "memory" else "evidence"))
            related = files.get(related_layer, str(item.get("related_id", ""))) if item.get("relation") == "support" and related_layer in {"evidence", "memory", "pattern"} and (related_layer != "pattern" or layer == "memory") else None
            if related:
                bonus = 35 if item.get("signal") == "strong" else 15
                related["weight"] = min(MAX_RECORD_WEIGHT, float(related.get("weight", 0)) + bonus)
                related["support_count"] = int(related.get("support_count", 1)) + 1
                related["trace_id"] = trace_id
                related["source_event_ids"] = list(dict.fromkeys([*related.get("source_event_ids", []), *ids]))
                related["trace_ids"] = list(dict.fromkeys([*related.get("trace_ids", []), trace_id]))
                saved = files._move(related, "memory") if layer == "memory" and related_layer == "evidence" else files.write(related_layer, related)
                if layer == "evidence" and saved["weight"] >= 60 and saved["support_count"] >= 3:
                    saved = files._move(saved, "memory")
                if saved["layer"] == "memory" and saved["weight"] >= PATTERN_PROMOTION_WEIGHT and saved["support_count"] >= 3:
                    files.write("pattern", {"title": saved.get("title", saved["claim"]), "claim": saved["claim"], "content": saved.get("content", saved["claim"]), "category": saved["category"], "domain": saved["domain"], "memory_ids": [saved["id"]], "trace_ids": [trace_id], "weight": saved["weight"], "support_count": saved["support_count"]})
            else:
                files.write(layer, {"title": item.get("title", claim), "claim": claim, "content": item.get("content", claim), "category": item.get("category", "project"), "domain": item.get("domain", "overall"), "trace_id": trace_id, "source_event_ids": ids, "confidence": item.get("confidence", .5), "weight": 65 if item.get("signal") == "strong" else 15, "support_count": 1})
        for event in agent_feedback_events:
            already_recorded = any(
                item.get("trace_id") == trace_id and event["event_id"] in item.get("source_event_ids", [])
                for item in files.list("evidence")
            )
            if event["event_id"] not in recorded_event_ids and not already_recorded:
                files.write("evidence", {
                    "title": "核查 AskUserQuestion 调用时机",
                    "claim": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "content": "用户质疑 Agent 对 AskUserQuestion 工具的调用时机，需要在后续调度优化中核查。",
                    "category": "agent", "domain": "overall", "trace_id": trace_id,
                    "source_event_ids": [event["event_id"]], "confidence": 0.9,
                    "weight": 15, "support_count": 1,
                })
        files.rebuild_indexes()
        await self.trace_store.set_trace_analysis_status(trace_id, "complete")

    @staticmethod
    def _json(text: str) -> dict:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try: return json.loads(clean[clean.find("{"):clean.rfind("}") + 1])
        except (ValueError, json.JSONDecodeError): return {}

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
