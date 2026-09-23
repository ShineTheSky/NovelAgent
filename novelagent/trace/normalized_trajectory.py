"""Normalized trajectory prompt and validation for Memory/Summary analysis."""

from __future__ import annotations

import json
import re


_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)

_ROLE_FIELDS = {
    "meta": {"role", "source", "cwd", "git_branch", "model"},
    "user": {"role", "content", "timestamp"},
    "system": {"role", "content", "timestamp"},
    "observation": {"role", "content", "timestamp"},
    "reasoning": {"role", "content", "timestamp"},
    "assistant": {"role", "content", "timestamp", "tool_calls"},
    "tool": {"role", "tool_call_id", "content", "ok", "timestamp"},
}


def build_trajectory_prompt(source_trace_ids: list[str], event_view: list[dict]) -> str:
    """Ask the same Memory/Summary Agent to establish a grounded first-pass trajectory."""
    return f'''[Memory/Summary Agent：轨迹整理轮]
这是同一个 Memory/Summary Agent 的第一轮。此轮只整理来源轨迹，不生成摘要、Insight、Memory 或 Pattern，也不进行冲突合并。

只输出一个 JSON 对象：
{{
  "trajectory": [<Normalized trajectory v1 records>],
  "provenance": [
    {{"record_index": 0, "trace_id": "tr_...", "turn_id": "tr_...:1", "turn_no": 1, "source_event_ids": ["evt_..."]}}
  ]
}}

trajectory 必须符合 https://letta.ai/schemas/trajectory/v1.json 的记录约束：
- 整体是至少含一项的数组；每项 role 只能是 meta、system、observation、user、reasoning、assistant、tool。
- meta 必须含 role、source，可选 cwd、git_branch、model，且不含 timestamp。
- system、observation、user、reasoning 必须只含 role、content、timestamp。
- assistant 必须含 role、content、timestamp。存在 tool_calls 时 content 必须为 null；否则 content 必须是非空字符串。
- tool_calls 每项只含 id、name、args，三者均为非空字符串，其中 args 是 JSON 参数的字符串形式。
- tool 必须含 role、tool_call_id、content、timestamp，可选 ok。
- timestamp 必须是带时区的 ISO-8601 字符串。
- 所有 record 都禁止 schema 之外的额外字段。

来源绑定规则：
- 除 meta 外，每条 trajectory record 必须在 provenance 中恰好有一项，通过 record_index 绑定。
- provenance 的 source_event_ids 只能使用下方原始事件中真实存在的 event_id。
- trace_id、turn_no 必须与这些事件一致；turn_id 固定写成 "{{trace_id}}:{{turn_no}}"。
- 一个 record 不得合并不同 trace_id 或不同 turn_no 的事件；需要时拆成多条。
- content 保留来源含义，不得补充、推断或改写事实。reasoning 只能来自原始 reasoning_content，不得生成新的思考过程。
- tool_call.id 使用对应 tool_call 事件的 event_id；tool.tool_call_id 使用其 parent_event_id，缺失时使用最近的同 turn tool_call event_id。
- assistant 同时包含文本和工具调用时，拆成一条文本 assistant 和一条 content=null 的 tool_calls assistant。

Source traces:{json.dumps(source_trace_ids, ensure_ascii=False)}
Raw trace events:{json.dumps(event_view, ensure_ascii=False)}'''


def validate_trajectory_payload(payload: dict, events: list[dict]) -> dict:
    """Strictly validate the model envelope and derive trustworthy provenance fields."""
    if not isinstance(payload, dict):
        return {}
    trajectory = payload.get("trajectory")
    provenance = payload.get("provenance")
    if not isinstance(trajectory, list) or not trajectory or not isinstance(provenance, list):
        return {}

    for record in trajectory:
        if not isinstance(record, dict):
            return {}
        role = record.get("role")
        allowed = _ROLE_FIELDS.get(role)
        if allowed is None or set(record) - allowed:
            return {}
        if role == "meta":
            if not isinstance(record.get("source"), str) or not record["source"].strip():
                return {}
            continue
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
            return {}
        if role in {"user", "system", "observation", "reasoning"}:
            if not isinstance(record.get("content"), str):
                return {}
        elif role == "assistant":
            calls = record.get("tool_calls")
            if calls is None:
                if not isinstance(record.get("content"), str) or not record["content"]:
                    return {}
            else:
                if record.get("content") is not None or not isinstance(calls, list) or not calls:
                    return {}
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"id", "name", "args"}:
                        return {}
                    if not all(isinstance(call.get(key), str) and call[key] for key in ("id", "name", "args")):
                        return {}
        elif role == "tool":
            if not all(isinstance(record.get(key), str) for key in ("tool_call_id", "content")):
                return {}
            if not record["tool_call_id"]:
                return {}
            if "ok" in record and not isinstance(record["ok"], bool):
                return {}

    by_event_id = {str(event.get("event_id")): event for event in events if event.get("event_id")}
    normalized_refs: list[dict] = []
    covered: set[int] = set()
    for ref in provenance:
        if not isinstance(ref, dict):
            continue
        try:
            record_index = int(ref.get("record_index"))
        except (TypeError, ValueError):
            continue
        if record_index < 0 or record_index >= len(trajectory) or trajectory[record_index].get("role") == "meta":
            continue
        event_ids = list(dict.fromkeys(
            str(event_id) for event_id in ref.get("source_event_ids", []) if str(event_id) in by_event_id
        ))
        if not event_ids:
            continue
        first = by_event_id[event_ids[0]]
        trace_id = str(first.get("trace_id") or "")
        turn_no = int(first.get("trace_turn") or 1)
        event_ids = [
            event_id for event_id in event_ids
            if str(by_event_id[event_id].get("trace_id") or "") == trace_id
            and int(by_event_id[event_id].get("trace_turn") or 1) == turn_no
        ]
        if not trace_id or not event_ids or record_index in covered:
            continue
        covered.add(record_index)
        normalized_refs.append({
            "record_index": record_index,
            "trace_id": trace_id,
            "turn_id": f"{trace_id}:{turn_no}",
            "turn_no": turn_no,
            "source_event_ids": event_ids,
        })

    required = {
        index for index, record in enumerate(trajectory) if record.get("role") != "meta"
    }
    if covered != required:
        return {}
    return {"trajectory": trajectory, "provenance": normalized_refs}
