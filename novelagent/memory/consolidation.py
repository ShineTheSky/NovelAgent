"""History -> fact package -> independent learning, with resumable source versions."""

import asyncio
import difflib
import json
import re
import logging
from copy import deepcopy
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from novelagent.context.token_counter import TokenCounter
from novelagent.core.llm_turn import build_assistant_message
from novelagent.history import DocumentHistoryStore
from novelagent.memory.knowledge_store import KnowledgeStore, fingerprint
from novelagent.memory.history_input import HistoryBodyReferences
from novelagent.memory.evidence_text import excerpt_matches, user_excerpt_matches, event_quote_matches
from novelagent.memory.models import MemoryPackage, MemoryExtractionPackage, Insight, GraphRelation, retrieval_text, TimeAnchor, require_classifications, exclude_dispatch_entities
from novelagent.memory.retrieval import KnowledgeRetrieval
from novelagent.trace.agent_run import AgentRunTrace
from novelagent.llm.json_output import json_object
from novelagent.llm.failures import recorded_bad_case_id
from novelagent.tools.base import ToolContext
from novelagent.tools.search_memory import ReadHistoryTool
from novelagent.memory.extraction_prompts import (
    RETAIN_CONTRACT_VERSION, MEMORY_RETAIN_PROMPT, MEMORY_MATCH_PROMPT, INSIGHT_CONSOLIDATE_PROMPT,
    ENTITY_IDENTITY_RULES, MEMORY_GRAPH_PROMPT,
)


class DirectionDeferred(Exception):
    """A valid uncertainty, not a broken model/tool execution."""


class MemoryValidationError(ValueError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class KnowledgePipeline:
    def __init__(self, llm, workspace_dir, embedding, model_id, *, bad_cases=None, config=None, trace_store=None):
        self.llm, self.workspace_dir, self.embedding, self.model_id = llm, Path(workspace_dir), embedding, model_id
        self.bad_cases, self.config = bad_cases, config or {}
        self.trace_store = trace_store
        self._locks = {}
        self._retrievers = {}

    def store(self, project_id):
        target = (self.workspace_dir / project_id).resolve()
        if not target.is_relative_to(self.workspace_dir.resolve()):
            raise ValueError("Knowledge project escapes workspace")
        return KnowledgeStore(target)

    def retrieval(self, project_id):
        if project_id not in self._retrievers:
            self._retrievers[project_id] = KnowledgeRetrieval(self.store(project_id), self.embedding, self.model_id,
                candidate_limit=self.config.get("candidate_limit", 40), graph_depth=self.config.get("graph_depth", 3),
                graph_nodes=self.config.get("graph_nodes", 128), timeout=self.config.get("recall_timeout", 30))
        return self._retrievers[project_id]

    async def _call(self, project_id, prompt, stage, context=None, *, validate=None, history_id=""):
        messages = [*(context or []), {"role": "user", "content": prompt}]
        trace = None
        max_turns = self.config.get("analysis_max_turns", 12)
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
            raise ValueError("Knowledge analysis_max_turns must be a positive integer")
        def feedback(error, remaining_turns=max_turns):
            details = {"message": str(error), "remaining_turns": remaining_turns, **getattr(error, "details", {})}
            if isinstance(error, ValidationError):
                details["errors"] = [{"location": list(e["loc"]), "message": e["msg"], "type": e["type"]}
                                     for e in error.errors(include_input=False)]
            return {"role": "user", "content": "上一轮输出未通过程序校验，请根据以下错误修正，并重新输出完整 JSON。"
                "版本号和路径由程序按来源事件补填，fragments 只输出 source_event_id/before/after；"
                "source_event_quotes 必须为每个引用事件提供相关连续原文，不能用摘要、字段名或猜测 ID 代替；"
                "选择真正对应片段的事件，不能用无关 Edit 代替。缺少原文时保留模型提取的 fragment，"
                "不要改写真实原文或编造版本。错误信息是诊断数据，不是创作指令。\n校验详情："
                + json.dumps(details, ensure_ascii=False)}
        route = {}
        anthropic = False
        if hasattr(self.llm, "loader"):
            config = self.llm.loader.get_config("memory_summary_fallback")
            route = {"provider": config.provider, "model": config.model, "base_url": config.base_url,
                     "temperature": config.temperature, "reasoning_effort": config.reasoning_effort}
            anthropic = config.is_anthropic
            if stage == "retain" and getattr(config, "wire_api", "") == "responses" and not anthropic:
                route["reasoning_summary"] = "auto"
        reader = ReadHistoryTool(self) if stage == "retain" else None
        tools = None
        if reader:
            schema = reader.get_schema()
            schema["parameters"] = {"type": "object", "properties": {
                k: v for k, v in reader.parameters["properties"].items() if k != "history_id"},
                "required": ["revision_id"]}
            schema["description"] = "按 revision_id 分页读取存档正文；缺失返回 missing，不读取当前文件，不修改数据。"
            tools = [schema]
        request_id = fingerprint({"messages": messages, "stage": stage, "route": route,
                                  **({"tools": tools} if reader else {}),
                                  "max_tokens": self.config.get("analysis_output_tokens", 16384)})
        async def record_error(error, kind, **details):
            await self.capture_failure(project_id, {"history_id": history_id,
                "session_id": f"knowledge:{project_id}", "trace_ids": [trace.trace_id if trace else request_id],
                "bad_case_details": {"request_id": request_id, "error_kind": kind,
                    **getattr(error, "details", {}), **details}, "messages": messages}, error, stage)
        async def record_repairs(repairs, turn):
            for repair in repairs or []:
                if trace:
                    trace.add("event_reference_correction", {"stage": stage, "turn": turn, **repair})
                self.store(project_id).audit({"stage": stage, "request_id": request_id,
                    "trace_id": trace.trace_id if trace else "", "event_reference_correction": repair})
                await record_error(MemoryValidationError("Event reference corrected by unique excerpt", **repair),
                    "reference_correction", turn=turn)
        cached = self.store(project_id).model_result(request_id)
        if cached:
            try:
                result = json_object(cached["response"])
                repairs = validate(result) if validate else None
                if repairs and self.trace_store:
                    trace = await AgentRunTrace.try_start(self.trace_store, session_id=f"knowledge:{project_id}",
                        project_id=project_id, actor="knowledge:"+stage, position="memory_summary_fallback",
                        title="[knowledge] cached "+stage)
                    if trace:
                        trace.add("cached_response", {"request_id": request_id, "response": cached["response"]})
                await record_repairs(repairs, 0)
                if trace:
                    await trace.finish("completed", cached["response"])
                return result
            except (ValueError, TypeError, KeyError) as exc:
                await record_error(exc, "cached_validation")
                messages.extend([{"role": "assistant", "content": cached["response"]}, feedback(exc)])
        counter = TokenCounter()
        def check_budget(active_tools):
            if counter.count_messages(messages) + counter.count(json.dumps(active_tools or [], ensure_ascii=False)) > self.config.get("analysis_input_tokens", 480000):
                raise ValueError("Knowledge analysis exceeds input budget; source evidence retained, not silently truncated")
        started = asyncio.get_running_loop().time()
        chunks = []
        usage = {}
        if self.trace_store:
            trace = await AgentRunTrace.try_start(self.trace_store, session_id=f"knowledge:{project_id}",
                project_id=project_id, actor="knowledge:"+stage, position="memory_summary_fallback",
                title="[knowledge] "+stage)
            if trace:
                trace.add("model_route", {**route, "request_id": request_id})
        async def request():
            nonlocal usage
            last_error = "未返回有效的最终 JSON"
            for turn in range(max_turns):
                active_tools = tools
                check_budget(active_tools)
                if trace:
                    trace.add_request(position="memory_summary_fallback", tag=":knowledge/"+stage,
                                      messages=messages, tools=active_tools)
                text, reasoning, calls = [], [], []
                async for chunk in self.llm.chat(position="memory_summary_fallback", messages=messages, tools=active_tools,
                                               stream=True, tag=":knowledge/"+stage,
                                               json_mode=True,
                                               bad_case_context={"project_id": project_id, "history_id": history_id,
                                                                 "session_id": f"knowledge:{project_id}",
                                                                 "source_trace_id": trace.trace_id if trace else ""},
                                               **({"reasoning_summary": "auto"} if stage == "retain" else {}),
                                               max_tokens=self.config.get("analysis_output_tokens", 16384)):
                    if chunk.type == "error":
                        raise RuntimeError(chunk.error or "Knowledge analysis failed")
                    if trace and chunk.type in {"thinking", "text_delta"}:
                        for offset in range(0, len(chunk.content or ""), 12000):
                            trace.add(chunk.type, {"content": chunk.content[offset:offset+12000]})
                    if chunk.type == "text_delta":
                        text.append(chunk.content)
                    elif chunk.type == "thinking":
                        reasoning.append(chunk.content)
                    elif chunk.type == "tool_use":
                        calls.append(chunk)
                    elif chunk.type == "done":
                        turn_usage = getattr(chunk, "usage", {}) or {}
                        for key, value in turn_usage.items():
                            if isinstance(value, (int, float)):
                                usage[key] = usage.get(key, 0) + value
                            else:
                                usage[key] = value
                        if trace:
                            trace.add("llm_usage", {**route, **turn_usage, "finish_reason": getattr(chunk, "finish_reason", "")})
                if not calls:
                    try:
                        result = json_object("".join(text))
                        repairs = validate(result) if validate else None
                    except (ValueError, TypeError, KeyError) as exc:
                        last_error = str(exc)
                        correction = feedback(exc, max_turns - turn - 1)
                        messages.extend([build_assistant_message("".join(text), "".join(reasoning), [], turn), correction])
                        if trace:
                            trace.add("validation_error", {"stage": stage, "turn": turn + 1,
                                "remaining_turns": max_turns - turn - 1, "error": last_error, "feedback": correction["content"]})
                        await record_error(exc, "validation", turn=turn + 1,
                            remaining_turns=max_turns - turn - 1, feedback=correction["content"])
                        continue
                    await record_repairs(repairs, turn + 1)
                    chunks.extend(text)
                    return
                assistant = build_assistant_message("".join(text), "".join(reasoning), calls, turn)
                if anthropic:
                    blocks = ([{"type": "text", "text": "".join(text)}] if text else [])
                    blocks.extend({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                                   "input": calls[i].tool_input} for i, c in enumerate(assistant["tool_calls"]))
                    messages.append({"role": "assistant", "content": blocks})
                else:
                    messages.append(assistant)
                replies = []
                for index, call in enumerate(calls):
                    tid = assistant["tool_calls"][index]["id"]
                    params = call.tool_input
                    if trace:
                        trace.add("tool_call", {"tool": call.tool_name, "params": params, "tool_call_id": tid})
                    if not reader or call.tool_name != reader.name or not isinstance(params, dict) or not params.get("revision_id") or params.get("history_id"):
                        result = {"success": False, "data": None, "error": "本轮只允许按 revision_id 调用 ReadHistory"}
                    else:
                        result = (await reader.execute(params, ToolContext("knowledge", project_id,
                            str(self.store(project_id).project_dir)))).to_dict()
                    if trace:
                        trace.add("tool_result", {"tool": call.tool_name, "tool_call_id": tid, **result})
                    if not result["success"]:
                        await record_error(RuntimeError(result.get("error") or "Knowledge tool failed"), "tool",
                            turn=turn + 1, tool=call.tool_name, tool_call_id=tid, tool_params=params, tool_result=result)
                    content = json.dumps(result, ensure_ascii=False)
                    if anthropic:
                        replies.append({"type": "tool_result", "tool_use_id": tid, "content": content,
                                        "is_error": not result["success"]})
                    else:
                        messages.append({"role": "tool_result", "tool_call_id": tid, "content": content})
                if anthropic:
                    messages.append({"role": "user", "content": replies})
            raise RuntimeError(f"Knowledge analysis reached max_turns={max_turns}; last validation error: {last_error}")
        try:
            await asyncio.wait_for(request(), self.config.get("analysis_timeout", 120))
            self.store(project_id).save_model_result(request_id, stage, "".join(chunks), usage, replace_invalid=bool(cached))
            result = json_object("".join(chunks))
            if trace:
                try:
                    await trace.finish("completed", "".join(chunks))
                except Exception:
                    logging.exception("Knowledge model trace persistence failed")
            return result
        except Exception as exc:
            await record_error(exc, "execution")
            if trace:
                try:
                    await trace.finish("failed", error=str(exc))
                except Exception:
                    logging.exception("Knowledge failure trace persistence failed")
            raise
        finally:
            self.store(project_id).audit({"stage": stage, "trace_id": trace.trace_id if trace else "",
                                         "elapsed_ms": (asyncio.get_running_loop().time()-started)*1000})

    async def capture_failure(self, project_id, unit, error, stage):
        if recorded_bad_case_id(error):
            return
        if self.bad_cases and not getattr(error, "_knowledge_bad_case_recorded", False):
            recorded = await self.bad_cases.capture(source_trace_id=(unit.get("trace_ids") or [unit.get("history_id", "knowledge")])[0],
                session_id=unit.get("session_id", ""), project_id=project_id,
                failure_kind="knowledge_"+stage+"_failure", actor="memory:"+stage, error=str(error),
                params={"history_id": unit.get("history_id", ""), "stage": stage, **unit.get("bad_case_details", {})},
                messages=unit.get("messages"), schedule_analysis=False)
            if isinstance(error, Exception) and recorded != "":
                error._knowledge_bad_case_recorded = True

    async def resume_history(self, project_id, unit):
        """Only abnormal demand continuations need a semantic identity decision."""
        history = DocumentHistoryStore(self.workspace_dir / project_id)
        hid = unit["history_id"]
        if unit.get("attempts"):
            self.store(project_id).merge_task_uses(hid, [identity for a in unit["attempts"]
                for identity in (a["history_id"], a["request_id"])], self.config.get("pattern_after_uses", 0),
                score_threshold=self.config.get("pattern_promotion_weight", 200))
        if history.canonical_root(hid) != hid or unit.get("attempts"):
            return history.get_unit(hid)
        candidates = history.resume_candidates(hid)
        if not candidates:
            return unit
        def summary(item):
            return {"history_id": item["history_id"], "status": item["status"],
                    "events": [{"event_id": e["event_id"], "kind": e["kind"], "content": e["content"]}
                               for e in item["events"] if e["kind"] in {"user_input", "user_answer", "main_task"}],
                    "affected_files": item.get("affected_files", []),
                    "selected_turns": item.get("selected_turns", []),
                    "assistant_response": item["assistant_response"]}
        signatures = {item["history_id"]: history.root_signature(item["history_id"]) for item in [unit, *candidates]}
        digest = fingerprint(signatures)
        try:
            decision = history.resume_decision(hid, digest)
            if decision is None:
                decision = await self._call(project_id,
                    "判断本轮是否明确延续某个异常中断的同一需求。只有需求目标、范围和未完成工作一致才合并；"
                    "同文件、相似话题或新增修改意见不代表同一需求，已结束的工作不候选。"
                    "不确定则 same_requirement=false，不编造续接关系。只选给定候选 ID。"
                    '输出 JSON {"same_requirement":true或false,"target_history_id":"候选ID或空","reason":"证据依据"}。'
                    + "\n本轮：" + json.dumps(summary(unit), ensure_ascii=False)
                    + "\n异常候选：" + json.dumps([summary(c) for c in candidates], ensure_ascii=False), "resume-history", history_id=hid)
                if type(decision.get("same_requirement")) is not bool or not isinstance(decision.get("reason"), str):
                    raise ValueError("Invalid demand resume decision")
                if decision["same_requirement"] and decision.get("target_history_id") not in {c["history_id"] for c in candidates}:
                    raise ValueError("Resume target is not an abnormal candidate")
                history.resume_decision(hid, digest, decision)
            if decision["same_requirement"]:
                target = decision["target_history_id"]
                history.merge_roots(target, hid, decision["reason"], signatures)
                merged = history.get_unit(target)
                self.store(project_id).merge_task_uses(target, [identity for a in merged["attempts"]
                    for identity in (a["history_id"], a["request_id"])], self.config.get("pattern_after_uses", 0),
                    score_threshold=self.config.get("pattern_promotion_weight", 200))
                return merged
            return unit
        except Exception as exc:
            await self.capture_failure(project_id, unit, exc, "resume_history")
            # Do not extract a separate package after failed identity judgment; retry is safe.
            raise

    def _evidence(self, project_id, unit):
        history = DocumentHistoryStore(self.workspace_dir / project_id)
        events = [e for e in unit["events"] if e["kind"] not in {"revision", "revision_reference", "initial_context"}]
        revisions = [r for r in unit.get("revisions", []) if r]
        if unit["kind"] == "requirement":
            for child in unit.get("affected_files", []):
                document = history.get_unit(child["history_id"])
                events.extend(e for e in document["events"] if not e.get("inherited_requirement") and not e.get("inherited_review")
                              and e["kind"] not in {"revision", "revision_reference", "initial_context", "reviewer_thinking", "reviewer_output"})
                revisions.extend(r for r in document["revisions"] if r)
        events = list({e["event_id"]: e for e in events}.values())
        events = history.order_evidence(events, [a["history_id"] for a in unit.get("attempts", [])] or None)
        evidence = [{"event_id": e["event_id"], "history_id": e.get("evidence_history_id", e["history_id"]),
                     "kind": e["kind"], "content": e["content"], "revision_id": e.get("revision_id", ""),
                     "scope": e.get("scope", {}), "event_order": e.get("event_order"),
                     "occurred_at": e.get("occurred_at", ""), "order_status": e.get("order_status", "unverified")} for e in events]
        artifacts = []
        for revision in {r["revision_id"]: r for r in revisions}.values():
            previous = history.get_revision(revision["parent_revision_id"]) if revision.get("parent_revision_id") else None
            artifacts.append({"path": revision["path"], "revision_id": revision["revision_id"],
                              "body": revision["body_md"], "previous_revision_id": revision.get("parent_revision_id"),
                              "diff": "\n".join(difflib.unified_diff((previous["body_md"] if previous else "").splitlines(),
                                                                   revision["body_md"].splitlines(), n=3))})
        return evidence, artifacts

    @staticmethod
    def _allowed_paths(unit, evidence, artifacts):
        paths = {r["path"] for r in artifacts} | {unit.get("path", "")}
        paths.update(f["path"] for f in unit.get("affected_files", []))
        for event in evidence:
            paths.add(event.get("scope", {}).get("path", ""))
            paths.update(re.findall(r"(?:chapters|outlines|characters|world)/[\w.\-\u4e00-\u9fff]+\.md|(?<![\w/])outline\.md",
                                    event["content"].replace("\\", "/")))
        return paths - {""}

    @staticmethod
    def _quote_matches(text, event):
        if event["kind"] == "user_answer":
            return user_excerpt_matches(text, event["content"])
        return excerpt_matches(text, event["content"])

    @staticmethod
    def _validate_package(payload, evidence):
        package = exclude_dispatch_entities(MemoryPackage.model_validate(payload).model_dump())
        by_id = {e["event_id"]: e for e in evidence}
        for index, fact in enumerate(package["statements"], 1):
            # Keep the extraction's local identities when an old package is prepended during matching.
            if not fact['statement_id']:
                fact['statement_id'] = f'new:{index}'
            if set(fact["source_event_ids"]) - by_id.keys():
                raise MemoryValidationError("Memory cites unavailable History evidence",
                    location=f"statements[{index-1}].source_event_ids",
                    unavailable_ids=sorted(set(fact["source_event_ids"]) - by_id.keys()),
                    correction="引用给定 History 的真实事件 ID，不复制 Trace ID 或编造新事件")
        for index, relation in enumerate(package["relations"]):
            if set(relation["source_event_ids"]) - by_id.keys():
                raise MemoryValidationError("Relation cites unavailable History evidence",
                    location=f"relations[{index}].source_event_ids",
                    unavailable_ids=sorted(set(relation["source_event_ids"]) - by_id.keys()))
        for index, change in enumerate(package["statement_changes"]):
            if set(change["source_event_ids"]) - by_id.keys():
                raise MemoryValidationError("MemoryStatement change cites unavailable History evidence",
                    location=f"statement_changes[{index}].source_event_ids",
                    unavailable_ids=sorted(set(change["source_event_ids"]) - by_id.keys()))
        if package["source_text"] and not any(KnowledgePipeline._quote_matches(package["source_text"], e)
                                              for e in evidence if e["kind"] in {"user_input", "user_answer"}):
            raise MemoryValidationError("User source text must be an exact excerpt", location="source_text",
                supplied_text=package["source_text"],
                correction="使用单条用户输入或回答中的连续原文；允许中英文引号及引号转义形式差异，不拼接、不改写、不增加角色名")
        for path in package["artifact_paths"]:
            if ".." in Path(path).parts or Path(path).is_absolute():
                raise ValueError("Memory artifact path escapes project")
        for fact in package["statements"]:
            if any(t["dimension"] == "chapter" for t in fact["times"]):
                continue
            explicit = fact["attributes"].get("artifact_paths", [])
            if not isinstance(explicit, list) or any(not isinstance(p, str) or p not in package["artifact_paths"] for p in explicit):
                raise ValueError("MemoryStatement scope references an unrelated artifact")
            paths = set(explicit) | {by_id[eid].get("scope", {}).get("path", "") for eid in fact["source_event_ids"]}
            paths &= set(package["artifact_paths"])
            if not paths and len(package["artifact_paths"]) == 1:
                paths = set(package["artifact_paths"])
            # Multi-file statements without a precise scope must not inherit the first file's position.
            for path in sorted(paths):
                position = re.search(r"(?:content_|outline_)(\d+\.\d+\.\d+)\.md$", path)
                if position:
                    fact["times"].append(TimeAnchor(dimension="chapter", start=position[1], raw=path, precision="section").model_dump())
        return package

    def _bind_fragment(self, project_id, selection, evidence):
        event = next((e for e in evidence if e["event_id"] == selection["source_event_id"]), None)
        if not event or event["kind"] not in {"file_change_attempt", "document_reference"}:
            raise MemoryValidationError("Memory fragment cites an unavailable document event",
                field="source_event_id", source_event_id=selection["source_event_id"],
                actual_kind=event["kind"] if event else "missing",
                correction="引用给定 History 中对应片段的成功修改或版本读取事件，不能引用用户输入或不存在的事件")
        payload = json.loads(event["content"])
        if not isinstance(payload, dict):
            raise ValueError("Memory fragment source is not a document event")
        params = payload.get("params") or {}
        if event["kind"] == "file_change_attempt" and (payload.get("success") is not True
                or payload.get("tool") not in {"Write", "Edit"}):
            raise MemoryValidationError("Memory fragment must cite a successful document modification",
                field="source_event_id", source_event_id=selection["source_event_id"],
                tool=payload.get("tool"), success=payload.get("success"),
                correction="失败的修改不能作为正文片段来源；引用对应成功事件，否则移除此 fragment")
        path = payload.get("path") or params.get("path") or event.get("scope", {}).get("path", "")
        result_id = HistoryBodyReferences.revision_from_result(payload.get("result") or payload.get("content"))
        revision_id = payload.get("revision_id") or result_id or event.get("revision_id", "")
        if not path or not revision_id:
            raise ValueError("Memory fragment source has no document version")
        if result_id and result_id != revision_id:
            raise ValueError("Memory fragment source has conflicting document versions")
        revision = DocumentHistoryStore(self.workspace_dir / project_id).get_revision(revision_id)
        previous_id = params.get("expected_revision_id") or (revision or {}).get("parent_revision_id") or ""
        tool = payload.get("tool")
        event_verified = tool == "Edit" or (tool == "Write" and not selection["before"])
        if tool in {"Edit", "Write"}:
            fields = (("before", "old_string"), ("after", "new_string")) if tool == "Edit" else (("after", "content"),)
            for field, parameter in fields:
                if selection[field] and not isinstance(params.get(parameter), str):
                    event_verified = False
                elif selection[field] and not excerpt_matches(selection[field], params[parameter]):
                    raise MemoryValidationError(f"Memory fragment does not match its cited {tool}",
                        field=field, source_event_id=selection["source_event_id"], path=path,
                        supplied_text=selection[field], expected_source_field="params." + parameter,
                        event_text=params.get(parameter, ""),
                        correction="该片段不是此修改事件的内容；引用真正修改该片段的事件，或使用此事件的真实前后片段")
        return {**selection, "path": path.replace("\\", "/"), "revision_id": revision_id,
                "previous_revision_id": previous_id, "verification": "event_verified" if event_verified else "model_trusted"}

    @staticmethod
    def _resolve_event_quotes(package, evidence, corrections):
        referenced = {eid for field in ("statements", "relations", "statement_changes")
                      for item in package[field] for eid in item["source_event_ids"]}
        fragment_ids = {f["source_event_id"] for f in package["fragments"]}
        referenced |= fragment_ids
        quotes = package.pop("source_event_quotes")
        quoted = {q["event_id"] for q in quotes}
        if referenced != quoted:
            raise MemoryValidationError("Event quote table must cover exactly the cited events",
                location="source_event_quotes", missing_ids=sorted(referenced - quoted), unused_ids=sorted(quoted - referenced))
        by_id = {e["event_id"]: e for e in evidence}
        resolved = {}
        for index, quote in enumerate(quotes):
            event_id, excerpt = quote["event_id"], quote["excerpt"]
            def eligible(event):
                if event_id not in fragment_ids:
                    return True
                if event["kind"] == "document_reference":
                    return True
                if event["kind"] != "file_change_attempt":
                    return False
                try:
                    value = json.loads(event["content"])
                    return isinstance(value, dict) and value.get("success") is True and value.get("tool") in {"Edit", "Write"}
                except (TypeError, ValueError):
                    return False
            def matches(event):
                return event_quote_matches(excerpt, event["content"])
            original = by_id.get(event_id)
            if original and eligible(original) and matches(original):
                selected = original
            else:
                candidates = [e for e in evidence if eligible(e) and matches(e)]
                if len(candidates) != 1:
                    raise MemoryValidationError("Event excerpt has no unique allowed source",
                        location=f"source_event_quotes[{index}]", supplied_id=event_id, supplied_text=excerpt,
                        minimum_similarity=.90,
                        status="ambiguous" if candidates else "missing",
                        candidates=[{"event_id": e["event_id"], "kind": e["kind"], "content_preview": e["content"][:240]}
                                    for e in candidates])
                selected = candidates[0]
            if event_id in resolved and resolved[event_id] != selected["event_id"]:
                raise MemoryValidationError("Quotes for the same event ID resolve to different events",
                    location=f"source_event_quotes[{index}]", supplied_id=event_id,
                    resolved_ids=[resolved[event_id], selected["event_id"]])
            resolved[event_id] = selected["event_id"]
            if event_id != selected["event_id"]:
                corrections.append({"location": f"source_event_quotes[{index}]", "supplied_id": event_id,
                    "corrected_id": selected["event_id"], "excerpt": excerpt, "kind": selected["kind"]})
        for field in ("statements", "relations", "statement_changes"):
            for item in package[field]:
                item["source_event_ids"] = list(dict.fromkeys(resolved[eid] for eid in item["source_event_ids"]))
        for fragment in package["fragments"]:
            fragment["source_event_id"] = resolved[fragment["source_event_id"]]
        return package

    def _model_package(self, project_id, payload, evidence, *, require_event_quotes=False, corrections=None):
        raw = deepcopy(payload)
        has_quotes = isinstance(raw, dict) and "source_event_quotes" in raw
        if isinstance(raw, dict) and not require_event_quotes and not has_quotes:
            raw["source_event_quotes"] = []  # Compatibility for already saved/offline packages only.
        package = MemoryExtractionPackage.model_validate(raw).model_dump()
        if has_quotes or require_event_quotes:
            package = self._resolve_event_quotes(package, evidence, corrections if corrections is not None else [])
        else:
            package.pop("source_event_quotes")
        for index, fragment in enumerate(package["fragments"]):
            try:
                package["fragments"][index] = self._bind_fragment(project_id, fragment, evidence)
            except MemoryValidationError as exc:
                exc.details["location"] = f"fragments[{index}].{exc.details.get('field', '')}"
                raise
        return self._validate_package(package, evidence)

    def _checked_model_package(self, project_id, payload, evidence, allowed_paths, location, *, require_event_quotes=True, corrections=None):
        repair_start = len(corrections) if corrections is not None else 0
        try:
            package = self._model_package(project_id, payload, evidence, require_event_quotes=require_event_quotes, corrections=corrections)
            if set(package["artifact_paths"]) - allowed_paths:
                raise MemoryValidationError("Memory references an unrelated artifact", field="artifact_paths",
                    supplied_paths=package["artifact_paths"], allowed_paths=sorted(allowed_paths))
            self._validate_fragments(project_id, package, set(package["artifact_paths"]), evidence)
        except (ValueError, TypeError, KeyError) as exc:
            details = dict(getattr(exc, "details", {}))
            if corrections:
                details["reference_repairs"] = list(corrections)
            details["location"] = location + ("." + details["location"] if details.get("location") else "")
            if isinstance(exc, ValidationError):
                details["errors"] = [{"location": list(e["loc"]), "message": e["msg"], "type": e["type"]}
                                     for e in exc.errors(include_input=False)]
            raise MemoryValidationError(str(exc), **details) from exc
        finally:
            for repair in (corrections or [])[repair_start:]:
                repair["location"] = location + "." + repair["location"]
        return package

    def _validate_fragments(self, project_id, package, allowed_paths, evidence=()):
        history = DocumentHistoryStore(self.workspace_dir / project_id)
        for index, fragment in enumerate(package["fragments"]):
            bound = {}
            if fragment.get("source_event_id"):
                bound = self._bind_fragment(project_id, {k: fragment[k] for k in ("source_event_id", "before", "after")}, evidence)
                if any(fragment[k] != bound[k] for k in ("path", "revision_id", "previous_revision_id")):
                    raise ValueError("Memory fragment versions do not match its source event")
            revision = history.get_revision(fragment["revision_id"])
            previous = history.get_revision(fragment["previous_revision_id"]) if fragment["previous_revision_id"] else None
            details = {"location": f"fragments[{index}]", "source_event_id": fragment.get("source_event_id", ""),
                "path": fragment["path"], "revision_id": fragment["revision_id"],
                "previous_revision_id": fragment["previous_revision_id"]}
            if fragment["path"] not in allowed_paths or (revision and revision["path"] != fragment["path"]):
                raise MemoryValidationError("Memory fragment cannot be verified against the saved document version", **details,
                    status="missing" if not revision else "path_mismatch", allowed_paths=sorted(allowed_paths),
                    correction="片段引用的文件与允许范围或存档版本不匹配；引用对应事件，不能用当前文件代替")
            if revision and fragment["after"] and not excerpt_matches(fragment["after"], revision["body_md"]):
                raise MemoryValidationError("Memory fragment cannot be verified against the saved document version", **details,
                    field="after", supplied_text=fragment["after"],
                    correction="after 在该存档版本中不存在；用 ReadHistory 核对原文，保持逐字一致，不复制其他版本的片段")
            if fragment["previous_revision_id"] and ((previous and previous["path"] != fragment["path"])
                or (revision and revision["parent_revision_id"] and revision["parent_revision_id"] != fragment["previous_revision_id"])):
                raise MemoryValidationError("Memory fragment previous version is not this revision's parent", **details,
                    expected_parent_revision_id=revision["parent_revision_id"] if revision else "",
                    previous_version_status="available" if previous else "missing",
                    correction="事件的修改前版本与存档父版本无法核验；不要猜版本号，移除此不可核验的 fragment")
            if previous and fragment["before"] and not excerpt_matches(fragment["before"], previous["body_md"]):
                raise MemoryValidationError("Memory before fragment cannot be verified", **details,
                    field="before", supplied_text=fragment["before"],
                    correction="before 在对应旧版本中不存在；核对旧版本真实原文或移除该 fragment")
            fragment["verification"] = ("version_verified" if revision and (not fragment["before"] or previous)
                else bound.get("verification", "model_trusted"))

    async def _resolve_entities(self, project_id, package, evidence, resolved_aliases=None):
        pending = []
        targets = []
        resolved_aliases = {} if resolved_aliases is None else resolved_aliases
        for fact in package["statements"]:
            for entity in fact["entities"]:
                alias = entity['entity_id'] if re.fullmatch(r'new:entity:\d+', entity['entity_id']) else ''
                if alias in resolved_aliases:
                    if resolved_aliases[alias]['kind'] != entity['kind']:
                        raise ValueError('Batch entity identity conflicts with kind')
                    entity.update(resolved_aliases[alias])
                    continue
                if (entity["entity_id"] and not alias) or entity["ambiguous"] or entity["new_identity"]:
                    if alias:
                        resolved_aliases[alias] = {k: entity[k] for k in ('entity_id', 'new_identity', 'ambiguous', 'kind')}
                    continue
                candidates = self.store(project_id).entity_candidates([entity["name"], *entity["aliases"]], entity["kind"])
                if candidates:
                    pending.append({"index": len(targets), "entity": entity, "fact": fact, "batch_alias": alias,
                                    "candidates": candidates})
                    targets.append(entity)
                elif alias:
                    resolved_aliases[alias] = {k: entity[k] for k in ('entity_id', 'new_identity', 'ambiguous', 'kind')}
        if not pending:
            return
        decision = await self._call(project_id,
            "消歧事实中的实体身份。姓名或别名相同不等于同一人物/对象；依据当前事实、范围和候选背景判断。"
            "同一身份填给定 entity_id；确为新身份设 new_identity=true；证据不足 ambiguous=true，不建立实体关联。"
            "每个 index 恰好一次，不创造现有ID。输出 JSON {entities:[{index:0,entity_id:'',new_identity:false,ambiguous:false,reason:''}]}。"
            + ENTITY_IDENTITY_RULES
            + "\n背景：" + package["background"] + "\n待解析：" + json.dumps(pending, ensure_ascii=False)
            + "\n当前证据：" + json.dumps(evidence, ensure_ascii=False), "entity-resolution")
        choices = decision.get("entities")
        if not isinstance(choices, list) or len(choices) != len(targets):
            raise ValueError("Entity resolution did not cover all mentions")
        seen = set()
        for choice in choices:
            index = choice.get("index")
            if type(index) is not int or index in seen or not 0 <= index < len(targets):
                raise ValueError("Invalid entity mention index")
            seen.add(index)
            eid = choice.get("entity_id", "")
            new, ambiguous = choice.get("new_identity"), choice.get("ambiguous")
            if type(new) is not bool or type(ambiguous) is not bool or int(bool(eid))+int(new)+int(ambiguous) != 1:
                raise ValueError("Entity resolution must choose existing, new or unresolved")
            if eid and eid not in {c["id"] for c in pending[index]["candidates"]}:
                raise ValueError("Entity resolution invented a candidate identity")
            alias = pending[index]['batch_alias']
            targets[index].update(entity_id=eid or (alias if new else ''), new_identity=new, ambiguous=ambiguous)
            if alias:
                resolved = {k: targets[index][k] for k in ('entity_id', 'new_identity', 'ambiguous', 'kind')}
                if alias in resolved_aliases and resolved_aliases[alias] != resolved:
                    raise ValueError('Inconsistent resolution of the same batch entity')
                resolved_aliases[alias] = resolved

    async def process(self, project_id, unit, *, retry_failed=False):
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            store = self.store(project_id)
            evidence, artifacts = self._evidence(project_id, unit)
            digest = fingerprint({"events": evidence, "artifacts": artifacts, "selected_turns": unit.get("selected_turns", []),
                                  "extraction_contract": RETAIN_CONTRACT_VERSION})
            state = store.input_state(unit["history_id"], digest)
            try:
                prepared = state["prepared"]
                if not prepared:
                    allowed_paths = self._allowed_paths(unit, evidence, artifacts)
                    def validate_retain(result):
                        corrections = []
                        if not isinstance(result.get("memories"), list):
                            raise MemoryValidationError("Retain response missing memories list", location="memories",
                                correction='输出 {"memories": [...]}，不要返回其他顶层结构')
                        for index, payload in enumerate(result["memories"]):
                            package = self._checked_model_package(project_id, payload, evidence, allowed_paths, f"memories[{index}]",
                                require_event_quotes=True, corrections=corrections)
                            for position, statement in enumerate(package["statements"]):
                                try:
                                    require_classifications(statement)
                                except ValueError as exc:
                                    raise MemoryValidationError(str(exc), location=f"memories[{index}].statements[{position}].classifications") from exc
                        return corrections
                    bodies = HistoryBodyReferences(DocumentHistoryStore(self.workspace_dir / project_id))
                    context = bodies.context(unit.get("review_workflow", {}).get("context", []))
                    input_events, input_artifacts = bodies.events(evidence), bodies.artifacts(artifacts)
                    registry = [{k: e[k] for k in ("event_id", "history_id", "kind", "revision_id", "scope")} for e in evidence] if context else input_events
                    prompt = (MEMORY_RETAIN_PROMPT + "\nSchema:"
                        + json.dumps(MemoryExtractionPackage.model_json_schema(), ensure_ascii=False)
                        + "\nHistory:" + json.dumps({"id": unit["history_id"], "kind": unit["kind"], "status": unit["status"],
                            "events": registry, "artifacts": input_artifacts, "body_versions": list(bodies.catalog.values()),
                            "selected_turns": unit.get("selected_turns", []),
                            "allowed_paths": sorted(self._allowed_paths(unit, evidence, artifacts))}, ensure_ascii=False))
                    prepared = await self._call(project_id, prompt, "retain", context, validate=validate_retain, history_id=unit["history_id"])
                    if not isinstance(prepared.get("memories"), list):
                        raise ValueError("Retain response missing memories list")
                    store.input_state(unit["history_id"], digest, prepared=prepared)
                entries, resolved_aliases = [], {}
                for index, raw in enumerate(prepared["memories"]):
                    operation_id = f"{unit['history_id']}:{digest}:{index}"
                    saved = store.operation(operation_id)
                    if saved:
                        entries.append({"payload": saved["payload"], "evidence": saved["evidence"],
                                        "vector": np.frombuffer(saved["vector"], dtype=np.float32), "model_id": saved["model_id"],
                                        "operation_id": operation_id, "batch_id": f"new:{index+1}"})
                        continue
                    package = self._model_package(project_id, raw, evidence)
                    allowed_paths = self._allowed_paths(unit, evidence, artifacts)
                    if set(package["artifact_paths"]) - allowed_paths:
                        raise ValueError("Memory references an unrelated artifact")
                    indexed_text = retrieval_text("memory", package)
                    vector = (await self.embedding.encode([indexed_text]))[0]
                    candidates_by_vector = await self.retrieval(project_id).vector_candidates(vector,
                        limit=self.config.get("memory_vector_top_k", 20),
                        exclude=[e["id"] for e in entries if e.get("id")])
                    high_similarity = [i for i in candidates_by_vector
                                       if i["similarity"] > self.config.get("memory_merge_similarity", .97)]
                    recall = {"items": high_similarity}
                    record_id, expected = "", 0
                    combined_evidence = evidence
                    if recall["items"]:
                        candidates = [{"id": r["record"]["id"], "version": r["record"]["version"], "payload": r["record"]["payload"],
                                       "evidence": r["record"]["evidence"], "similarity": r["similarity"]}
                                      for r in recall["items"]]
                        def validate_match(result):
                            corrections = []
                            action = result.get("action")
                            if action == "defer":
                                return
                            match_evidence, match_paths = evidence, allowed_paths
                            if action == "update":
                                prior = next((r["record"] for r in recall["items"] if r["record"]["id"] == result.get("memory_id")), None)
                                if not prior or result.get("expected_version") != prior["version"]:
                                    raise MemoryValidationError("Memory matching returned an invalid candidate version",
                                        location="memory_id/expected_version", supplied_id=result.get("memory_id"),
                                        supplied_version=result.get("expected_version"),
                                        candidates=[{"id": c["id"], "version": c["version"]} for c in candidates],
                                        correction="更新只能使用给定候选的真实 Memory ID 和当前版本")
                                match_evidence = list({e["event_id"]: e for e in [*prior["evidence"], *evidence]}.values())
                                match_paths = allowed_paths | set(prior["payload"]["artifact_paths"])
                            elif action != "new":
                                raise MemoryValidationError("Unknown Memory match action", location="action",
                                    supplied_action=action, expected_actions=["new", "update", "defer"])
                            if action == "update" or result.get("memory") is not None:
                                self._checked_model_package(project_id, result.get("memory"), match_evidence, match_paths, "memory",
                                    require_event_quotes=True, corrections=corrections)
                            return corrections
                        decision = await self._call(project_id,
                            MEMORY_MATCH_PROMPT + "\nSchema:" + json.dumps(MemoryExtractionPackage.model_json_schema(), ensure_ascii=False)
                            + "\nNew:" + json.dumps(package, ensure_ascii=False)
                            + "\nNewEvidence:" + json.dumps(evidence, ensure_ascii=False)
                            + "\nCandidates:" + json.dumps(candidates, ensure_ascii=False), "match", validate=validate_match, history_id=unit["history_id"])
                        if decision.get("action") == "defer":
                            raise DirectionDeferred("Memory direction remains ambiguous: "+str(decision.get("reason", "")))
                        if decision.get("action") == "update":
                            prior = next((r["record"] for r in recall["items"] if r["record"]["id"] == decision.get("memory_id")), None)
                            if not prior or decision.get("expected_version") != prior["version"]:
                                raise ValueError("Memory matching returned an invalid candidate version")
                            record_id, expected = prior["id"], prior["version"]
                            combined_evidence = list({e["event_id"]: e for e in [*prior["evidence"], *evidence]}.values())
                            package = self._model_package(project_id, decision["memory"], combined_evidence)
                            if set(package["artifact_paths"]) - (allowed_paths | set(prior["payload"]["artifact_paths"])):
                                raise ValueError("Merged Memory references an unrelated artifact")
                        elif decision.get("action") != "new":
                            raise ValueError("Unknown Memory match action")
                        elif decision.get("memory") is not None:
                            package = self._model_package(project_id, decision["memory"], evidence)
                            if set(package["artifact_paths"]) - allowed_paths:
                                raise ValueError("New Memory references an unrelated artifact")
                    await self._resolve_entities(project_id, package, combined_evidence, resolved_aliases)
                    # Matching/entity resolution may have changed the indexed text.
                    if retrieval_text("memory", package) != indexed_text:
                        vector = (await self.embedding.encode([retrieval_text("memory", package)]))[0]
                    self._validate_fragments(project_id, package, set(package["artifact_paths"]), combined_evidence)
                    referenced = {eid for f in package["statements"] for eid in f["source_event_ids"]}
                    referenced.update(f["source_event_id"] for f in package["fragments"] if f.get("source_event_id"))
                    referenced.update(eid for r in package["relations"] for eid in r["source_event_ids"])
                    referenced.update(eid for c in package["statement_changes"] for eid in c["source_event_ids"])
                    combined_evidence = [e for e in combined_evidence if e["event_id"] in referenced
                                         or (package["source_text"] and e["kind"] in {"user_input", "user_answer"}
                                             and self._quote_matches(package["source_text"], e))]
                    entries.append({"payload": package, "evidence": combined_evidence, "vector": vector,
                                    "model_id": self.model_id, "id": record_id, "expected_version": expected,
                                    "operation_id": operation_id, "batch_id": f"new:{index+1}"})
                store.save_memories(entries, build_graph=True)
                store.input_state(unit["history_id"], digest, status="complete", error="")
            except DirectionDeferred as exc:
                store.input_state(unit["history_id"], digest, status="deferred", error=str(exc))
                return False
            except Exception as exc:
                if prepared and not store.has_operations(f"{unit['history_id']}:{digest}:"):
                    store.audit({"stage": "retain_failed_proposal", "history_id": unit["history_id"],
                                 "prepared": prepared, "error": str(exc)})
                store.input_state(unit["history_id"], digest, status="failed", error=str(exc))
                await self.capture_failure(project_id, unit, exc, "retain")
                raise
        await self.consolidate_pending(project_id, retry_failed=retry_failed)
        return True

    async def consolidate_pending(self, project_id, *, retry_failed=False):
        async with self._locks.setdefault(project_id, asyncio.Lock()):
            store = self.store(project_id)
            for job in store.jobs():
                graph_job = store.graph_job(job["id"], job["version"])
                if graph_job and graph_job["status"] != "complete":
                    if graph_job["status"] == "superseded" or (graph_job["status"] == "failed" and not retry_failed):
                        continue
                    try:
                        await self._build_memory_graph(project_id, graph_job)
                    except Exception as exc:
                        for rid, version in graph_job["peers"]:
                            store.fail_graph_job(rid, version, exc)
                        await self.capture_failure(project_id, {"history_id": job["id"]}, exc, "memory_graph")
                        continue
            for job in store.jobs():
                current = next((j for j in store.jobs() if j["id"] == job["id"] and j["version"] == job["version"]), None)
                if not current:
                    continue  # A preceding batch consolidation may already have completed this job.
                job = current
                graph_job = store.graph_job(job["id"], job["version"])
                if graph_job and graph_job["status"] != "complete":
                    continue
                if job["status"] == "failed" and not retry_failed:
                    continue
                try:
                    await self._consolidate(project_id, job)
                except Exception as exc:
                    for rid, version in (graph_job["peers"] if graph_job else [(job["id"], job["version"])]):
                        store.fail_job(rid, version, str(exc))
                    await self.capture_failure(project_id, {"history_id": job["id"]}, exc, "consolidation")

    async def _build_memory_graph(self, project_id, job):
        store = self.store(project_id)
        source = store.get(job["id"], job["version"])
        memories = {source["id"]: source}
        new_ids = {source["id"]}
        for rid, version in job["peers"]:
            peer = store.get(rid)
            if peer and peer["version"] == version:
                memories[rid] = peer
                new_ids.add(rid)
        new_versions = {rid: memories[rid]["version"] for rid in sorted(new_ids)}
        candidates_by_id, time_edges = {}, []
        for rid in sorted(new_ids):
            memory = memories[rid]
            candidates = await self.retrieval(project_id).vector_candidates(
                np.frombuffer(memory["vector"], dtype=np.float32),
                limit=self.config.get("memory_vector_top_k", 20), exclude=new_ids)
            for item in candidates:
                key = item["record"]["id"]
                if key not in candidates_by_id or item["similarity"] > candidates_by_id[key]["similarity"]:
                    candidates_by_id[key] = item
                memories[key] = item["record"]
            time_edges.extend(store.time_relations(rid, memory["version"],
                window_hours=self.config.get("memory_time_window_hours", 24),
                neighbors=self.config.get("memory_time_neighbors", 8)))
        candidates = list(candidates_by_id.values())
        response = {"relations": []}
        if len(memories) > 1:
            def validate_graph(result):
                if not isinstance(result.get("relations"), list):
                    raise MemoryValidationError("Graph response missing relations list", location="relations")
                for index, raw in enumerate(result["relations"]):
                    edge = GraphRelation.model_validate(raw)
                    owner, target = memories.get(edge.owner_id), memories.get(edge.target_id)
                    location = f"relations[{index}]"
                    if not owner or not target or edge.owner_version != owner["version"] or edge.target_version != target["version"]:
                        raise MemoryValidationError("Graph edge must use given Memory IDs and versions", location=location,
                            candidates=[{"id": r["id"], "version": r["version"]} for r in memories.values()])
                    if not ({owner["id"], target["id"]} & new_ids):
                        raise MemoryValidationError("Graph edge must touch a New Memory", location=location)
                    if edge.kind not in {"semantic", "causal"} or (edge.kind == "semantic" and edge.predicate != "related"):
                        raise MemoryValidationError("Graph model only adds semantic/related or causal edges", location=location)
                    statements = [f for r in (owner, target) for f in r["payload"]["statements"]
                             if f["status"] == "active"]
                    for memory, fid in ((owner, edge.source_statement_id), (target, edge.target_statement_id)):
                        if not fid or not any(f["statement_id"] == fid and f["status"] == "active" for f in memory["payload"]["statements"]):
                            raise MemoryValidationError("Graph edge needs active endpoint statements", location=location,
                                supplied_fact_id=fid, memory_id=memory["id"])
                    if owner["id"] == target["id"] and edge.source_statement_id == edge.target_statement_id:
                        raise MemoryValidationError("Self relation is not evidence", location=location)
                    events = {eid for f in statements if f["statement_id"] in {edge.source_statement_id, edge.target_statement_id}
                              for eid in f["source_event_ids"]}
                    if set(edge.source_event_ids) - events:
                        raise MemoryValidationError("Graph edge cites unrelated events", location=location,
                            allowed_event_ids=sorted(events))
            response = await self._call(project_id, MEMORY_GRAPH_PROMPT + "\nSchema:"
                + json.dumps(GraphRelation.model_json_schema(), ensure_ascii=False)
                + "\nNew:" + json.dumps(sorted(new_ids))
                + "\nCandidates:" + json.dumps([{"id": i["record"]["id"], "similarity": i["similarity"]} for i in candidates])
                + "\nMemories:" + json.dumps([{k: r[k] for k in ("id", "version", "payload", "evidence")}
                                             for r in memories.values()], ensure_ascii=False),
                "memory_graph", validate=validate_graph, history_id=source["id"])
        for edge in time_edges:
            target = store.get(edge["target_id"])
            if target:
                memories[target["id"]] = target
        relations = [GraphRelation.model_validate(r).model_dump() for r in response["relations"]] + time_edges
        store.finish_graph_job(source["id"], source["version"], relations,
            {rid: r["version"] for rid, r in memories.items()}, completed_jobs=new_versions)

    async def _consolidate(self, project_id, job, extra_existing=None, coordination_round=0):
        store = self.store(project_id)
        source = store.get(job["id"], job["version"])
        new_sources = {source["id"]: source}
        graph_job = store.graph_job(source["id"], source["version"])
        if graph_job and graph_job["status"] == "complete":
            pending = {(j["id"], j["version"]) for j in store.jobs()
                       if j["status"] == "pending" or job["status"] == "failed"}
            for rid, version in graph_job["peers"]:
                if (rid, version) in pending:
                    new_sources[rid] = store.get(rid, version)
        direct, existing = {}, dict(extra_existing or {})
        for memory in new_sources.values():
            recall = await self.retrieval(project_id).recall(retrieval_text("memory", memory["payload"]), purpose="knowledge_reconcile")
            if recall["status"] in {"failed", "timeout"}:
                raise RuntimeError(recall.get("error", "Knowledge reconciliation failed"))
            direct.update({r["id"]: r for r in store.derived(memory["id"])})
            existing.update({i["record"]["id"]: i["record"] for i in recall["items"]})
        existing.update(direct)
        sources = {}
        for memory in new_sources.values():
            sources.update({r["id"]: r for r in store.related_memories(memory["id"], memory["version"],
                                                                      limit=self.config.get("memory_vector_top_k", 20))})
        sources.update(new_sources)
        for record in existing.values():
            for ref in record["payload"]["sources"]:
                sources[ref["memory_id"]] = store.get(ref["memory_id"])
        def validate_classifications(result):
            if not isinstance(result.get("insights"), list) or not isinstance(result.get("withdrawals"), list):
                raise ValueError("Consolidation response missing operations")
            for index, proposal in enumerate(result["insights"]):
                try:
                    require_classifications(Insight.model_validate(proposal["payload"]).model_dump())
                except ValueError as exc:
                    raise MemoryValidationError(str(exc), location=f"insights[{index}].payload.classifications") from exc

        response = await self._call(project_id,
            INSIGHT_CONSOLIDATE_PROMPT + "\nSchema:"+json.dumps(Insight.model_json_schema(), ensure_ascii=False)+"\nMemories:"
            +json.dumps([{ "id":r["id"],"version":r["version"],"payload":r["payload"],"evidence":r["evidence"]} for r in sources.values()], ensure_ascii=False)
            +"\nExisting:"+json.dumps([{ "id":r["id"],"version":r["version"],"layer":r["layer"],"payload":r["payload"]} for r in existing.values()], ensure_ascii=False), "consolidate", validate=validate_classifications)
        if not isinstance(response.get("insights"), list) or not isinstance(response.get("withdrawals"), list):
            raise ValueError("Consolidation response missing operations")
        # Newly learned wording can find conflicts not visible from the source Memory.
        # Never publish a proposal before this bounded coordination has converged.
        additional = {}
        for proposal in response["insights"]:
            payload = Insight.model_validate(proposal["payload"]).model_dump()
            related = await self.retrieval(project_id).recall(retrieval_text("insight", payload), purpose="knowledge_reconcile")
            if related["status"] in {"failed", "timeout"}:
                raise RuntimeError(related.get("error", "Insight coordination failed"))
            for item in related["items"]:
                record = item["record"]
                if record["id"] not in existing:
                    additional[record["id"]] = record
        if additional:
            if coordination_round >= self.config.get("coordination_rounds", 2):
                raise ValueError("Insight coordination did not converge; old published knowledge remains active")
            return await self._consolidate(project_id, job, {**existing, **additional}, coordination_round+1)
        touched = {e.get("id") for e in response["insights"]} | set(response["withdrawals"])
        if direct.keys() - touched:
            raise ValueError("Consolidation did not update all directly derived knowledge")
        if set(response["withdrawals"]) - existing.keys():
            raise ValueError("Cannot withdraw knowledge outside the reconciliation candidates")
        entries = []
        for raw in response["insights"]:
            payload = Insight.model_validate(raw["payload"]).model_dump()
            previous = existing.get(raw.get("id"))
            if raw.get("id") and (not previous or previous["version"] != raw.get("expected_version")):
                raise ValueError("Invalid Insight update target")
            selected_events, evidence = [], []
            for ref in payload["sources"]:
                memory = sources.get(ref["memory_id"])
                if not memory or memory["version"] != ref["version"]:
                    raise ValueError("Insight source version is not the analyzed Memory")
                statement_ids = set(ref["statement_ids"]) or {f["statement_id"] for f in memory["payload"]["statements"]}
                if statement_ids - {f["statement_id"] for f in memory["payload"]["statements"]}:
                    raise ValueError("Insight cites unknown statements")
                ev_ids = {eid for f in memory["payload"]["statements"] if f["statement_id"] in statement_ids for eid in f["source_event_ids"]}
                evidence.extend(e for e in memory["evidence"] if e["event_id"] in ev_ids)
                if ref["role"] == "support":
                    selected_events.extend(e for e in memory["evidence"] if e["event_id"] in ev_ids)
            human = any(e["kind"] in {"user_input", "user_answer"} for e in selected_events)
            reviewer = any(e["kind"].startswith("reviewer_") for e in selected_events)
            if (payload["authority"] == "human" or payload["human_confirmed"] or any(s["human_selected"] for s in payload["solutions"])) and not human:
                raise ValueError("Insight claims unsupported human authority")
            if reviewer and not human:
                payload["authority"], payload["human_confirmed"], payload["roles"] = "reviewer", False, ["reviewer"]
            if not selected_events:
                raise ValueError("Insight has no supporting statements")
            evidence = list({e["event_id"]: e for e in evidence}.values())
            vector = (await self.embedding.encode([retrieval_text("insight", payload)]))[0]
            entries.append({"id": raw.get("id", ""), "expected_version": raw.get("expected_version", 0),
                            "payload": payload, "evidence": evidence, "vector": vector, "model_id": self.model_id,
                            "replaces": raw.get("replaces", []), "replacement_reason": raw.get("replacement_reason", "")})
        store.publish_insights(entries, {rid:r["version"] for rid,r in sources.items()}, response["withdrawals"],
                               completed_jobs={rid: r["version"] for rid, r in new_sources.items()},
                               withdrawal_versions={rid: existing[rid]["version"] for rid in response["withdrawals"]})

    async def recover(self):
        if self.workspace_dir.exists():
            for project in self.workspace_dir.iterdir():
                if project.is_dir() and (project / ".knowledge" / "knowledge.sqlite3").exists():
                    await self.consolidate_pending(project.name)
