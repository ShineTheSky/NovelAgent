"""Build trace-backed compressed context without another LLM call."""

from __future__ import annotations


def _turn_messages(turn: dict) -> list[dict]:
    messages = []
    if turn.get("user_content"):
        messages.append({"role": "user", "content": str(turn["user_content"])})
    if turn.get("assistant_content"):
        messages.append({"role": "assistant", "content": str(turn["assistant_content"])})
    return messages


def _plain_messages(messages: list[dict] | None) -> list[dict]:
    return [
        {"role": str(message.get("role")), "content": str(message.get("content") or "")}
        for message in (messages or [])
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        and not message.get("tool_calls")
    ]


async def build_cumulative_compression(
    trace_store, session_id: str, *, current_user_message: str = "",
    legacy_messages: list[dict] | None = None,
) -> dict:
    """Compose the prior baseline, every new Memory summary, and raw gaps.

    Memory analysis is the sole summary producer. Compression only arranges
    persisted summaries and Turns, so it never invokes an Agent or LLM.
    """
    state = await trace_store.get_session_compression_state(session_id)
    previous_boundary = int(state.get("last_compressed_turn_no") or 0)
    baseline_messages = _plain_messages(state.get("messages"))
    summaries = await trace_store.list_trace_window_summaries_after(
        session_id, previous_boundary,
    )
    turns = await trace_store.list_session_turns(session_id, after_turn=previous_boundary)
    all_turns = await trace_store.list_session_turns(session_id)

    summaries_by_start: dict[int, dict] = {}
    for summary in summaries:
        start = int(summary.get("summary_start_turn_no") or summary.get("start_turn_no") or 0)
        end = int(summary.get("summary_end_turn_no") or summary.get("end_turn_no") or 0)
        text = str(summary.get("summary") or "").strip()
        if start <= previous_boundary or end < start or not text:
            continue
        existing = summaries_by_start.get(start)
        existing_end = int((existing or {}).get("summary_end_turn_no") or 0)
        if existing is None or end > existing_end:
            summaries_by_start[start] = {
                **summary, "summary_start_turn_no": start, "summary_end_turn_no": end,
            }

    turns_by_no = {int(turn["turn_no"]): turn for turn in turns}
    additions: list[dict] = []
    used_summaries: list[dict] = []
    uncovered_turn_count = 0
    latest_turn_no = int(all_turns[-1]["turn_no"]) if all_turns else previous_boundary
    cursor = previous_boundary + 1
    while cursor <= latest_turn_no:
        summary = summaries_by_start.get(cursor)
        if summary:
            end = min(int(summary["summary_end_turn_no"]), latest_turn_no)
            additions.append({
                "role": "user",
                "content": f"[Memory Summary · Turn {cursor}-{end}] {str(summary['summary']).strip()}",
            })
            used_summaries.append(summary)
            cursor = end + 1
            continue
        turn = turns_by_no.get(cursor)
        if turn:
            additions.extend(_turn_messages(turn))
            uncovered_turn_count += 1
        cursor += 1

    state_messages = [*baseline_messages, *additions]

    # Legacy sessions may not have session_trace_turns yet. Preserve their
    # complete model conversation instead of silently reducing it to nothing.
    if not all_turns and previous_boundary == 0 and not baseline_messages:
        state_messages = _plain_messages(legacy_messages)

    messages = list(state_messages)
    if current_user_message:
        messages.append({"role": "user", "content": current_user_message})

    return {
        "messages": messages,
        # The unfinished current request is intentionally excluded. This is the
        # exact baseline to persist for the next compression boundary.
        "state_messages": state_messages,
        "memory_summaries": used_summaries,
        "summary_window_ids": [str(item.get("window_id") or "") for item in used_summaries],
        "summary_trace_ids": [str(item.get("summary_trace_id") or "") for item in used_summaries],
        "summary_count": len(used_summaries),
        "previous_compression_turn_no": previous_boundary,
        "last_compressed_turn_no": latest_turn_no,
        "uncovered_turn_count": uncovered_turn_count,
    }
