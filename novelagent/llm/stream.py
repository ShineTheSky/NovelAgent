"""SSE流式响应解析适配"""

import json
from novelagent.llm.client import LLMResponse


def parse_sse_line(line: str) -> LLMResponse | None:
    """解析单行SSE数据为 LLMResponse"""
    if not line.startswith("data: "):
        return None
    data_str = line[6:]
    if data_str == "[DONE]":
        return LLMResponse(type="done", finish_reason="stop")
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return None
    return _parse_chunk(data)


def _parse_chunk(data: dict) -> LLMResponse | None:
    """解析Anthropic/OpenAI兼容的chunk数据"""
    event_type = data.get("type", "")

    # Anthropic content_block_delta
    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return LLMResponse(type="text_delta", content=delta.get("text", ""))
        # input_json_delta: 参数流式累积，不单独返回chunk
        return None

    # Anthropic content_block_start (tool_use)
    if event_type == "content_block_start":
        block = data.get("content_block", {})
        if block.get("type") == "tool_use":
            return LLMResponse(
                type="tool_use",
                tool_name=block.get("name", ""),
                tool_input=block.get("input", {}),
            )
        return None

    # Anthropic message_delta (done signal)
    if event_type == "message_delta":
        usage = data.get("usage", {})
        return LLMResponse(type="done", finish_reason="stop", usage=usage)

    # OpenAI-compatible delta
    choices = data.get("choices", [])
    for choice in choices:
        delta = choice.get("delta", {})
        if "content" in delta and delta["content"]:
            return LLMResponse(type="text_delta", content=delta["content"])
        if "tool_calls" in delta:
            for tc in delta["tool_calls"]:
                func = tc.get("function", {})
                return LLMResponse(
                    type="tool_use",
                    tool_name=func.get("name", ""),
                    tool_input=json.loads(func.get("arguments", "{}")),
                )
        if choice.get("finish_reason"):
            return LLMResponse(type="done", finish_reason=choice["finish_reason"])

    return None
