"""共享 LLM query 执行器 — 主Agent和SubAgent共用"""

import json
from dataclasses import dataclass, field


FINALIZATION_PROMPT = """你已达到本次任务允许的最大执行轮数，刚才的工具结果是当前可用的最后信息。现在是额外的强制收尾轮，不能再调用任何工具。
请立即根据已有信息给出最终回复：
1. 如果工作已经完成，明确总结完成结果、写入内容和必要的验证信息。
2. 如果工作未完成，明确说明当前完成到哪一步、未完成事项、无法完成的具体原因，以及建议的下一步。
3. 不得声称未实际完成的操作已经成功，不得只回复空内容或继续请求调用工具。"""


async def query(
    llm_client,
    *,
    position: str,
    messages: list[dict],
    system_prompt: str,
    tools: list[dict] | None,
    max_turns: int,
    tag: str,
    execute_tool,      # async fn(tool_name, params, tool_call_id) -> (success, data, error)
    stop_check=None,   # fn() -> bool
    sub_type=None,
):
    """
    执行一次完整的 ReAct query（async generator）。

    Yields event dicts:
      {"type": "thinking", "content": str}
      {"type": "text_delta", "content": str}
      {"type": "tool_call", "tool": str, "params": dict, "tool_call_id": str}
      {"type": "tool_result", "tool": str, "success": bool, "data": str, "error": str}
      {"type": "max_turns_exhausted", "turns": int, "messages": list[dict]}
      {"type": "done"}
      {"type": "error", "error": str}
      {"type": "result", "final_text": str}  # 最后一条
    """
    full_messages = [{"role": "system", "content": system_prompt}] + list(messages)

    for turn in range(max_turns):
        if stop_check and stop_check():
            break

        turn_result = _TurnResult()
        async for chunk in llm_client.chat(
            position=position, messages=full_messages,
            tools=tools if tools else None,
            stream=True, sub_type=sub_type, tag=tag,
        ):
            if chunk.type == "thinking":
                turn_result.full_reasoning += chunk.content
                yield {"type": "thinking", "content": chunk.content}
            elif chunk.type == "text_delta":
                turn_result.full_response += chunk.content
                yield {"type": "text_delta", "content": chunk.content}
            elif chunk.type == "tool_use":
                if chunk.tool_name:
                    turn_result.tool_calls.append(chunk)
                    yield {"type": "tool_call", "tool": chunk.tool_name, "params": chunk.tool_input, "tool_call_id": chunk.tool_call_id}
            elif chunk.type == "done":
                yield {"type": "done"}
                break
            elif chunk.type == "error":
                turn_result.error = chunk.error
                yield {"type": "error", "error": chunk.error}
                return

        if turn_result.error:
            return

        if not turn_result.tool_calls:
            full_messages.append({
                "role": "assistant",
                "content": turn_result.full_response,
                **({"reasoning_content": turn_result.full_reasoning} if turn_result.full_reasoning else {}),
            })
            yield {"type": "result", "final_text": turn_result.full_response}
            return

        full_messages.append(build_assistant_message(
            turn_result.full_response, turn_result.full_reasoning,
            turn_result.tool_calls, turn))

        for tc in turn_result.tool_calls:
            tid = getattr(tc, 'tool_call_id', '') or tc.tool_name
            if tc.tool_name == "AskUserQuestion":
                yield {"type": "question_ask", "questions": tc.tool_input.get("questions", [])}
            success, data, error = await execute_tool(tc.tool_name, tc.tool_input, tid)
            yield {"type": "tool_result", "tool": tc.tool_name, "success": success, "data": data, "error": error}
            full_messages.append({
                "role": "tool_result",
                "tool_call_id": tid,
                "content": data if success else f"Error: {error}",
            })
    else:
        yield {"type": "max_turns_exhausted", "turns": max_turns, "messages": list(full_messages)}
        full_messages.append({"role": "user", "content": FINALIZATION_PROMPT})
        final_result = _TurnResult()
        async for chunk in llm_client.chat(
            position=position, messages=full_messages, tools=None,
            stream=True, sub_type=sub_type, tag=f"{tag}:finalize",
        ):
            if chunk.type == "thinking":
                final_result.full_reasoning += chunk.content
                yield {"type": "thinking", "content": chunk.content}
            elif chunk.type == "text_delta":
                final_result.full_response += chunk.content
                yield {"type": "text_delta", "content": chunk.content}
            elif chunk.type == "done":
                yield {"type": "done"}
                break
            elif chunk.type == "error":
                yield {"type": "error", "error": chunk.error}
                return
        yield {"type": "result", "final_text": final_result.full_response}
        return

    yield {"type": "result", "final_text": ""}


def build_assistant_message(full_response, full_reasoning, tool_calls, turn):
    msg = {"role": "assistant", "content": full_response or None}
    if full_reasoning:
        msg["reasoning_content"] = full_reasoning
    msg["tool_calls"] = [
        {
            "id": getattr(tc, 'tool_call_id', '') or f"call_{turn}_{i}",
            "type": "function",
            "function": {
                "name": tc.tool_name,
                "arguments": json.dumps(tc.tool_input, ensure_ascii=False) if isinstance(tc.tool_input, dict) else str(tc.tool_input),
            },
        }
        for i, tc in enumerate(tool_calls)
    ]
    return msg


@dataclass
class _TurnResult:
    full_response: str = ""
    full_reasoning: str = ""
    tool_calls: list = field(default_factory=list)
    error: str = ""
