"""Read embedded legacy tool calls without changing their original Trace."""

import json
import re


def normalize_tool_events(events):
    expanded, pending = [], []
    explicit_ids = {e["payload"].get("tool_call_id") or e["payload"].get("call_id")
                    for e in events if e["event_type"].endswith("tool_call")}
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "assistant_turn":
            pending = []  # Never match a result against a previous model turn.
            expanded.append(event)
            for call in payload.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments", call.get("input", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        continue
                tool = function.get("name") or call.get("name")
                if not tool or not isinstance(arguments, dict):
                    continue
                call_id = call.get("id") or ""
                if call_id and call_id in explicit_ids:
                    continue
                item = {**event, "event_type": "tool_call", "payload": {
                    "tool": tool, "params": arguments, "tool_call_id": call_id,
                    "run_id": payload.get("run_id", ""), "embedded_call": True}}
                pending.append(item)
                expanded.append(item)
            continue
        if event["event_type"].endswith("tool_call"):
            pending.append(event)
        elif event["event_type"].endswith("tool_result"):
            call_id = payload.get("tool_call_id") or payload.get("call_id")
            if call_id:
                candidates = [c for c in pending if
                              (c["payload"].get("tool_call_id") or c["payload"].get("call_id")) == call_id]
            elif event.get("parent_event_id"):
                candidates = [c for c in pending if event["parent_event_id"] == c["event_id"]]
            elif payload.get("tool"):
                candidates = [c for c in pending if c["payload"].get("tool") == payload["tool"]]
            else:
                candidates = []
            candidates = [c for c in candidates if str(c["payload"].get("run_id") or "main")
                          == str(payload.get("run_id") or "main")]
            if not call_id and not event.get("parent_event_id") and not payload.get("tool"):
                data = payload.get("data")
                if isinstance(data, dict) and data.get("queued") is True and data.get("reason"):
                    candidates = [c for c in pending if c["payload"].get("tool") == "CreateTraceCheckpoint"
                                  and c["payload"]["params"].get("reason") == data["reason"]]
                elif len(pending) == 1:
                    call = pending[0]
                    # An unlabelled report must independently identify the task's target.
                    task = (call["payload"].get("params") or {}).get("task", "")
                    paths = re.findall(r"(?:chapters|outlines|world|characters)/[^\s`，。()]+\.md", task)
                    if call["payload"].get("tool") == "SubAgent" and isinstance(data, str) and any(p in data for p in paths):
                        candidates = [call]
            if len(candidates) == 1 and (not payload.get("tool") or payload["tool"] == candidates[0]["payload"].get("tool")):
                call = candidates[0]
                pending.remove(call)
                event = {**event, "payload": {**payload,
                    "tool": call["payload"]["tool"], "call_params": call["payload"].get("params") or {},
                    "call_source_event_id": call["event_id"]}}
        expanded.append(event)
    return expanded
