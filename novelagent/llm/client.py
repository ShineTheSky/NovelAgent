"""多Provider LLM客户端"""

import json
import time
import asyncio
import uuid
from typing import AsyncIterator, Literal
from dataclasses import dataclass, field, replace

import httpx
from novelagent.llm.config_loader import LLMConfigLoader, LLMConfig
from novelagent.llm import responses
from novelagent.llm.failures import RecordedLLMError, recorded_bad_case_id, llm_failure_context, is_balance_error


@dataclass
class LLMResponse:
    type: Literal["text_delta", "thinking", "tool_use", "done", "error"]
    content: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input: dict = field(default_factory=dict)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""


class AuthenticationError(Exception): pass
class RateLimitError(Exception): pass
class APIError(Exception): pass
class ProviderBlockedError(APIError): pass
class TimeoutError(Exception): pass
class ProviderNotFoundError(Exception): pass


class LLMClient:
    def __init__(self, config_path: str = "config/llm_config.yaml"):
        self.loader = LLMConfigLoader(config_path)
        self._pending_tool_name = None
        self._pending_tool_args = None
        self._blocked_providers: dict[tuple, str] = {}
        self.bad_case_recorder = None

    def set_runtime_provider_settings(self, provider: str, base_url: str, api_key: str, models: list[str], **options) -> None:
        self.loader.set_runtime_provider_settings(provider, base_url, api_key, models, **options)

    def clear_runtime_provider_settings(self, provider: str) -> None:
        self.loader.clear_runtime_provider_settings(provider)

    def has_runtime_provider_settings(self, provider: str) -> bool:
        return self.loader.has_runtime_provider_settings(provider)

    def get_runtime_provider_base_url(self, provider: str) -> str | None:
        return self.loader.get_runtime_provider_base_url(provider)

    def get_runtime_provider_models(self, provider: str) -> list[str] | None:
        return self.loader.get_runtime_provider_models(provider)

    async def chat(
        self,
        position: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = True,
        sub_type: str = None,
        tag: str = "",
        max_tokens: int | None = None,
        json_mode: bool = False,
        bad_case_context: dict | None = None,
        reasoning_summary: str | None = None,
    ) -> AsyncIterator[LLMResponse]:
        self._log_tag = tag
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        config = None
        context = {**(llm_failure_context.get() or {}), **(bad_case_context or {})}

        async def record_error(error, kind, attempt=0):
            if is_balance_error(error) or not self.bad_case_recorder or recorded_bad_case_id(error):
                return error
            try:
                identity = await self.bad_case_recorder.capture(
                    source_trace_id=context.get("source_trace_id", ""),
                    session_id=context.get("session_id", "") or f"llm:{position}",
                    project_id=context.get("project_id", ""),
                    failure_kind="llm_call_failure", actor=f"llm:{sub_type or position}",
                    error=str(error), messages=messages,
                    params={**context, "request_id": request_id, "position": position,
                            "sub_type": sub_type, "tag": tag, "error_kind": kind,
                            "attempt": attempt, "stream": stream,
                            "reasoning_summary": reasoning_summary,
                            "provider": config.provider if config else "",
                            "model": config.model if config else "",
                            "wire_api": config.wire_api if config else "",
                            "error_type": type(error).__name__,
                            "http_status": getattr(error, "status_code", None),
                            "response_detail": getattr(error, "response_detail", ""),
                            "tool_names": [tool.get("name", "") for tool in (tools or [])]},
                    duration_ms=(time.monotonic() - started) * 1000,
                    schedule_analysis=False,
                )
                if identity:
                    if isinstance(error, Exception):
                        error.bad_case_id = identity
                    return RecordedLLMError(str(error), identity)
            except Exception as exc:
                print(f"[llm_bad_case] capture failed: {exc}", flush=True)
            return error

        try:
            config = self.loader.get_config(position, sub_type)
            if max_tokens is not None:
                config = replace(config, max_tokens=max_tokens)
            body = self._build_request(config, messages, tools, stream)
            if reasoning_summary and config.wire_api == "responses" and not config.is_anthropic:
                body.setdefault("reasoning", {})["summary"] = reasoning_summary
            if json_mode and config.provider == "deepseek":
                body["response_format"] = {"type": "json_object"}
            if json_mode and config.wire_api == "responses" and not config.is_anthropic:
                body["text"] = {"format": {"type": "json_object"}}
        except Exception as exc:
            await record_error(exc, "request_setup")
            raise
        provider_key = (config.provider, config.base_url, config.api_key, tuple(sorted(config.headers.items())))
        if provider_key in self._blocked_providers:
            error = await record_error(self._blocked_providers[provider_key], "provider_blocked")
            yield LLMResponse(type="error", error=error)
            return

        last_error = None
        max_retries = config.retry.get("max_retries", 3)
        for attempt in range(max_retries + 1):
            try:
                async for chunk in self._send_request(config, body, stream):
                    if chunk.type == "error":
                        chunk.error = await record_error(chunk.error or "LLM 返回未说明原因的错误", "response", attempt + 1)
                    yield chunk
                    if chunk.type == "error":
                        return
                return
            except RateLimitError as e:
                last_error = await record_error(e, "rate_limit", attempt + 1)
                if attempt < max_retries:
                    delay = min(2 ** attempt, 30)
                    await asyncio.sleep(delay)
            except (AuthenticationError, ProviderBlockedError) as e:
                self._blocked_providers[provider_key] = str(e)
                error = await record_error(e, "provider_blocked", attempt + 1)
                yield LLMResponse(type="error", error=str(error) if isinstance(error, Exception) else error)
                return
            except (APIError, TimeoutError) as e:
                error = await record_error(e, "request", attempt + 1)
                yield LLMResponse(type="error", error=str(error) if isinstance(error, Exception) else error)
                return
            except Exception as exc:
                await record_error(exc, "request", attempt + 1)
                raise

        error = f"重试{max_retries}次后仍失败: {last_error}"
        identity = recorded_bad_case_id(last_error)
        yield LLMResponse(type="error", error=RecordedLLMError(error, identity) if identity else error)

    def _build_request(
        self, config: LLMConfig, messages: list[dict], tools: list[dict] | None, stream: bool
    ) -> dict:
        if config.wire_api == "responses" and not config.is_anthropic:
            return responses.build_request(config, messages, tools, stream)
        body = {
            "model": config.model,
            "messages": messages,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "stream": stream,
        }
        # reasoning_effort 是 OpenAI 兼容接口的可选参数；未设置时完全不发送。
        if config.reasoning_effort and not config.is_anthropic:
            body["reasoning_effort"] = config.reasoning_effort
        # Anthropic: system prompt is separate from messages
        if config.is_anthropic:
            system_msgs = [m for m in messages if m["role"] == "system"]
            body["messages"] = [m for m in messages if m["role"] != "system"]
            if system_msgs:
                body["system"] = "\n\n".join(m["content"] for m in system_msgs)
            if tools:
                body["tools"] = self._to_anthropic_tools(tools)
        else:
            # OpenAI-compatible: convert tool_result → tool role, fix content for tool_calls
            body["messages"] = []
            for m in messages:
                m2 = dict(m)
                if m2["role"] == "tool_result":
                    m2["role"] = "tool"
                if m2["role"] == "tool" and not isinstance(m2.get("content"), str):
                    content = m2.get("content", "")
                    m2["content"] = json.dumps(content, ensure_ascii=False) if content is not None else ""
                # 有 tool_calls 的消息必须删除 content（None 或空字符串都会被 MiniMax 拒绝）
                if m2.get("tool_calls") and not m2.get("content"):
                    del m2["content"]
                body["messages"].append(m2)
            if tools:
                body["tools"] = self._to_openai_tools(tools)
        return body

    def _to_anthropic_tools(self, tools: list[dict]) -> list[dict]:
        result = []
        for t in tools:
            result.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}, "required": []}),
            })
        return result

    def _to_openai_tools(self, tools: list[dict]) -> list[dict]:
        result = []
        for t in tools:
            properties = t.get("parameters", {}).get("properties", {})
            required = t.get("parameters", {}).get("required", [])
            result.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return result

    async def _send_request(
        self, config: LLMConfig, body: dict, stream: bool
    ) -> AsyncIterator[LLMResponse]:
        headers = {
            "Content-Type": "application/json",
            **config.headers,
        }
        if config.is_anthropic:
            headers["x-api-key"] = config.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        endpoint = "messages" if config.is_anthropic else "responses" if config.wire_api == "responses" else "chat/completions"
        messages_url = f"{config.base_url.rstrip('/')}/{endpoint}"

        async with httpx.AsyncClient(timeout=config.timeout) as client:
            try:
                if stream:
                    async with client.stream("POST", messages_url, json=body, headers=headers) as response:
                        import sys
                        print(f"[LLM{self._log_tag}] HTTP {response.status_code}", flush=True)
                        error_detail = ""
                        if response.status_code != 200:
                            try:
                                error_body = await response.aread()
                                error_detail = error_body.decode(errors="replace")[:500]
                                print(f"[LLM{self._log_tag}] 错误响应体: {error_detail}", flush=True)
                            except Exception:
                                pass
                        self._check_status(response.status_code, messages_url, body, headers, error_detail)
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                print(f"[SSE{self._log_tag}] {line}", flush=True)
                                data_str = line[5:].strip()
                                if data_str == "[DONE]":
                                    yield LLMResponse(type="done", finish_reason="stop")
                                    return
                                try:
                                    data = json.loads(data_str)
                                    if config.is_anthropic:
                                        for chunk in self._parse_anthropic_event(data): yield chunk
                                    elif config.wire_api == "responses":
                                        for chunk in responses.parse_event(data, LLMResponse):
                                            yield chunk
                                            if chunk.type in {"done", "error"}:
                                                return
                                    else:
                                        for chunk in self._parse_openai_event(data): yield chunk
                                except json.JSONDecodeError as exc:
                                    yield LLMResponse(type="error", error=f"LLM 流返回无效 JSON: {exc}; data={data_str[:500]}")
                                    return
                else:
                    response = await client.post(messages_url, json=body, headers=headers)
                    import sys
                    print(f"[LLM{self._log_tag}] HTTP {response.status_code}", flush=True)
                    error_detail = ""
                    if response.status_code != 200:
                        try:
                            error_detail = response.text[:500]
                            print(f"[LLM{self._log_tag}] 错误响应体: {error_detail}", flush=True)
                        except Exception:
                            pass
                    self._check_status(response.status_code, messages_url, body, headers, error_detail)
                    data = response.json()
                    for chunk in self._parse_non_stream_response(config, data): yield chunk
            except httpx.TimeoutException:
                raise TimeoutError(f"请求超时 ({config.timeout}s)")
            except (AuthenticationError, RateLimitError, APIError) as exc:
                exc.status_code = response.status_code
                exc.response_detail = error_detail
                raise

    def _check_status(self, status_code: int, messages_url: str, body: dict, headers: dict, error_detail: str = ""):
        if status_code == 401:
            raise AuthenticationError(f"API key无效")
        if status_code == 403:
            raise AuthenticationError(f"API key无权限(403)")
        if status_code == 429:
            raise RateLimitError(f"速率限制")
        if status_code >= 500:
            raise APIError(f"API服务器错误: {status_code}")
        if status_code != 200:
            try:
                detail = json.loads(error_detail).get("error", {}).get("message", "")
            except (json.JSONDecodeError, AttributeError):
                detail = error_detail
            error_type = ProviderBlockedError if status_code == 402 else APIError
            raise error_type(f"HTTP {status_code}: {detail[:300] or '请求被服务端拒绝'}")

    def _parse_anthropic_event(self, data: dict) -> list[LLMResponse]:
        if data.get("type") == "error" or data.get("error"):
            return [LLMResponse(type="error", error=json.dumps(data.get("error") or data, ensure_ascii=False))]
        results = []
        event_type = data.get("type", "")
        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                results.append(LLMResponse(type="text_delta", content=delta.get("text", "")))
            elif delta.get("type") == "input_json_delta":
                pass  # tool_use参数增量，累积在调用侧处理
        elif event_type == "content_block_start":
            block = data.get("content_block", {})
            if block.get("type") == "tool_use":
                results.append(LLMResponse(
                    type="tool_use",
                    tool_name=block.get("name", ""),
                    tool_input=block.get("input", {}),
                ))
        elif event_type == "message_delta":
            usage = data.get("usage", {})
            if usage:
                results.append(LLMResponse(type="done", finish_reason="stop", usage=usage))
        return results

    def _parse_openai_event(self, data: dict) -> list[LLMResponse]:
        if data.get("error"):
            return [LLMResponse(type="error", error=json.dumps(data["error"], ensure_ascii=False))]
        results = []
        choices = data.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            if "reasoning_content" in delta and delta["reasoning_content"]:
                results.append(LLMResponse(type="thinking", content=delta["reasoning_content"]))
            if "content" in delta and delta["content"]:
                results.append(LLMResponse(type="text_delta", content=delta["content"]))
            if "tool_calls" in delta:
                for tc in delta["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args_str = func.get("arguments", "")
                    tc_id = tc.get("id", "")
                    # 保存LLM分配的tool_call_id
                    if tc_id:
                        self._pending_tool_id = tc_id
                    # 流式tool_calls: 第一帧有name+空args, 后续帧有args无name
                    if name:
                        self._pending_tool_name = name
                        self._pending_tool_args = ""
                    # 累积arguments
                    if args_str:
                        self._pending_tool_args = (self._pending_tool_args or "") + args_str
                        # 尝试解析完整JSON
                        try:
                            parsed = json.loads(self._pending_tool_args)
                            results.append(LLMResponse(
                                type="tool_use",
                                tool_name=self._pending_tool_name or name,
                                tool_call_id=self._pending_tool_id,
                                tool_input=parsed,
                            ))
                            self._pending_tool_name = None
                            self._pending_tool_args = None
                            self._pending_tool_id = None
                        except json.JSONDecodeError:
                            pass  # 参数JSON未完整，等待下一帧
            if choice.get("finish_reason"):
                if self._pending_tool_args:
                    results.append(LLMResponse(type="error", error=f"LLM 工具参数不是完整 JSON: {self._pending_tool_args[:500]}"))
                    self._pending_tool_name = self._pending_tool_args = self._pending_tool_id = None
                    return results
                results.append(LLMResponse(type="done", finish_reason=choice["finish_reason"]))
        return results

    def _parse_non_stream_response(self, config: LLMConfig, data: dict) -> list[LLMResponse]:
        if data.get("error"):
            return [LLMResponse(type="error", error=json.dumps(data["error"], ensure_ascii=False))]
        if config.wire_api == "responses" and not config.is_anthropic:
            return responses.parse_response(data, LLMResponse)
        results = []
        if config.is_anthropic:
            for block in data.get("content", []):
                if block.get("type") == "text":
                    results.append(LLMResponse(type="text_delta", content=block["text"]))
                elif block.get("type") == "tool_use":
                    results.append(LLMResponse(
                        type="tool_use",
                        tool_name=block.get("name", ""),
                        tool_call_id=block.get("id", ""),
                        tool_input=block.get("input", {}),
                    ))
        else:
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if msg.get("content"):
                    results.append(LLMResponse(type="text_delta", content=msg["content"]))
                if msg.get("reasoning_content"):
                    results.append(LLMResponse(type="thinking", content=msg["reasoning_content"]))
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    results.append(LLMResponse(
                        type="tool_use",
                        tool_name=func.get("name", ""),
                        tool_call_id=tc.get("id", ""),
                        tool_input=json.loads(func.get("arguments", "{}")),
                    ))
        finish = data.get("stop_reason", "stop") if config.is_anthropic else next(
            (c.get("finish_reason") for c in data.get("choices", []) if c.get("finish_reason")), "stop")
        results.append(LLMResponse(type="done", finish_reason=finish, usage=data.get("usage", {})))
        return results
