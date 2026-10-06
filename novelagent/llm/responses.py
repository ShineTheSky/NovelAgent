"""Responses 协议与 Agent 使用的消息／流事件之间的转换。"""

import json


def build_request(config, messages: list[dict], tools: list[dict] | None, stream: bool) -> dict:
    items = []
    for message in messages:
        role = message["role"]
        content = message.get("content")
        if role in {"tool", "tool_result"}:
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if content:
            items.append({"role": "developer" if role == "system" else role, "content": content})
        for call in message.get("tool_calls", []):
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                          "arguments": function["arguments"]})
    body = {"model": config.model, "input": items, "stream": stream,
            "store": not config.disable_response_storage, "max_output_tokens": config.max_tokens}
    # GPT 推理模型不使用 Chat Completions 的 temperature / reasoning_effort 字段。
    if config.reasoning_effort:
        body["reasoning"] = {"effort": config.reasoning_effort}
    if tools:
        body["tools"] = [{"type": "function", "name": tool["name"],
                          "description": tool.get("description", ""),
                          "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                          "strict": False} for tool in tools]
    return body


def parse_item(item: dict, response_type) -> list:
    kind = item.get("type")
    if kind == "function_call":
        try:
            arguments = json.loads(item.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            return [response_type(type="error", error="Responses 工具参数不是有效 JSON")]
        if not isinstance(arguments, dict):
            return [response_type(type="error", error="Responses 工具参数必须是对象")]
        return [response_type(type="tool_use", tool_name=item.get("name", ""),
                              tool_call_id=item.get("call_id", ""), tool_input=arguments)]
    if kind == "message":
        return [response_type(type="text_delta", content=part.get("text", ""))
                for part in item.get("content", []) if part.get("type") == "output_text"]
    if kind == "reasoning":
        return [response_type(type="thinking", content=part.get("text", ""))
                for part in item.get("summary", []) if part.get("type") == "summary_text"]
    return []


def terminal(response: dict, response_type) -> list:
    status = response.get("status", "completed")
    if status != "completed":
        detail = response.get("error") or response.get("incomplete_details") or {}
        return [response_type(type="error", error=f"Responses {status}: {detail}")]
    usage = dict(response.get("usage") or {})
    # Context / Trace 延用 Chat Completions 的计数字段，同时保留 Responses 原始 usage。
    usage["prompt_tokens"] = usage.get("input_tokens", 0)
    usage["completion_tokens"] = usage.get("output_tokens", 0)
    usage["prompt_cache_hit_tokens"] = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    has_tools = any(item.get("type") == "function_call" for item in response.get("output", []))
    return [response_type(type="done", finish_reason="tool_calls" if has_tools else "stop", usage=usage)]


def parse_response(data: dict, response_type) -> list:
    results = [chunk for item in data.get("output", []) for chunk in parse_item(item, response_type)]
    return results + terminal(data, response_type)


def parse_event(data: dict, response_type) -> list:
    kind = data.get("type")
    if kind == "response.output_text.delta":
        return [response_type(type="text_delta", content=data.get("delta", ""))]
    if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
        return [response_type(type="thinking", content=data.get("delta", ""))]
    # done 中有完整工具参数；只在这里发送一次，不在每个 arguments.delta 中执行工具。
    if kind == "response.output_item.done" and data.get("item", {}).get("type") == "function_call":
        return parse_item(data["item"], response_type)
    if kind in {"response.completed", "response.failed", "response.incomplete"}:
        return terminal(data.get("response", {}), response_type)
    if kind == "error":
        return [response_type(type="error", error=data.get("message", "Responses 流发生错误"))]
    return []
