"""Agent Loop — ReAct循环引擎"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from novelagent.core.session import InternalRequest, ResponseChunk, Session
from novelagent.tools.base import ToolContext, ToolResult, PermissionResult
from novelagent.context.message_manager import Message, MessageManager
from novelagent.security.audit import audit_log
from novelagent.core.llm_turn import build_assistant_message
from novelagent.trace.recorder import TraceRecorder
from novelagent.memory.memory_manager import MemoryManager


class AgentLoop:
    def __init__(self, llm_client, tool_registry, permission_checker, context_builder, memory_manager,
                 config: dict | None = None, subagent_runner=None, trace_recorder: TraceRecorder | None = None,
                 post_turn_analyzer=None, preference_context_provider=None, rag_store=None):
        self.llm = llm_client
        self.tools = tool_registry
        self.security = permission_checker
        self.context_builder = context_builder
        self.memory = memory_manager
        self.subagent_runner = subagent_runner
        self._config = config or {}
        self.max_turns = self._config.get("max_turns", 20)
        self.token_limit = self._config.get("token_limit", 150_000)
        self.loop_threshold = self._config.get("loop_detection_threshold", 3)
        self.trace_interval = self._config.get("trace_interval", 5)
        self.working_dir = self._config.get("working_dir", "./workspace")
        self.trace = trace_recorder
        self.post_turn_analyzer = post_turn_analyzer
        self.preference_context_provider = preference_context_provider
        self.rag_store = rag_store

    async def run(self, request: InternalRequest, session: Session, project_info: dict | None = None) -> ResponseChunk:
        trace_id = hashlib.md5(f"{session.session_id}{time.time()}".encode()).hexdigest()[:8]

        async def record(event_type: str, actor: str, payload: dict | None = None,
                         parent_event_id: str | None = None, duration_ms: float | None = None):
            return None

        async def finish(status: str, answer: str = "", token_count: int = 0):
            return None

        pi = project_info or {}
        trace_checkpoint_requested = False

        # 项目记忆预取仍与项目工作区绑定；自动 Trace 记忆则持久化到 SQLite。
        import os as _os
        project_memory = MemoryManager(_os.path.join(self.working_dir, session.project_id), self.llm)

        # 1. Async memory prefetch
        prefetch_task = asyncio.create_task(project_memory.on_user_input(request.content))

        # 2. Get memory index
        memory_md = project_memory.get_index_content()
        tools_prompt = self.tools.get_tools_prompt()
        preference_context = ""
        if self.preference_context_provider:
            try:
                preference_context = await self.preference_context_provider.render(session.project_id)
            except Exception as exc:
                print(f"[trace] preference context failed: {exc}", flush=True)

        # Retrieve shared reference material before building the prompt.
        # This is intentionally best-effort: an empty or unavailable library
        # must never prevent ordinary writing from working.
        rag_context = ""
        rag_results = []
        if self.rag_store:
            try:
                rag_context, rag_results = await self.rag_store.format_context(
                    request.content, limit=5,
                )
            except Exception as exc:
                print(f"[rag] retrieval failed: {exc}", flush=True)

        # 3. Build context (sync with prefetch)
        history_msgs = [Message.from_dict(m) for m in session.messages] if session.messages else []
        current_user_msg = Message(role="user", content=request.content)

        ctx = await self.context_builder.build(
            project_name=pi.get("name", ""),
            project_genre=pi.get("genre", ""),
            project_word_count=pi.get("word_count", 0),
            memory_md_content=memory_md,
            tools_description=tools_prompt,
            history_messages=history_msgs + [current_user_msg],
            preference_context=preference_context,
        )
        await record("context_built", "system", {
            "history_message_count": len(history_msgs),
            "memory_index_present": bool(memory_md),
            "confirmed_preference_count": preference_context.count("\n-") + (1 if preference_context.startswith("-") else 0),
            "rag_result_count": len(rag_results),
        })

        if rag_context:
            ctx.messages.insert(-1, Message(role="system", content=rag_context))

        # 4. Wait for prefetch (1.5s timeout)
        memory_injection = []
        try:
            memories = await asyncio.wait_for(prefetch_task, timeout=1.5)
            if memories:
                memory_injection = memories
                injection_text = "[相关记忆]\n" + "\n---\n".join(memories)
                ctx.messages.insert(-1, Message(role="system", content=injection_text))
        except asyncio.TimeoutError:
            pass

        # 5. Tool context — working_dir 指向具体项目目录
        project_working_dir = _os.path.join(self.working_dir, session.project_id)
        tool_ctx = ToolContext(
            session_id=session.session_id,
            project_id=session.project_id,
            working_dir=project_working_dir,
            accept_edits_mode=session.accept_edits_mode or request.accept_edits,
            allow_rules=self._config.get("write_allow_rules", []),
            previously_allowed=session.previously_allowed,
        )
        ctx.tool_context = tool_ctx

        # 5.5. 进入循环前先检查是否需要压缩
        pre_check_tokens = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
        if self.context_builder.token_counter.needs_compression(ctx.to_llm_messages(), self.token_limit):
            t0 = time.time()
            print(f"[compress] 进入循环前触发压缩: {pre_check_tokens} tokens", flush=True)
            yield ResponseChunk(type="thinking", data={"content": "上下文过长，正在压缩…"})
            await record("context_compression", "system", {"before_token_count": pre_check_tokens})
            if self.post_turn_analyzer and self.trace:
                captured = await self.trace.store.capture_pending_trace(
                    session.session_id, session.project_id, ctx.to_llm_messages(), pre_check_tokens,
                )
                if captured:
                    await self.post_turn_analyzer.analyze(
                        captured["trace_id"],
                        session.project_id,
                        captured["messages"],
                        self.tools.get_schemas(),
                    )
            # 发送完整消息历史给 LLM 生成摘要
            try:
                non_system = [m for m in ctx.messages if m.role != "system"]
                history_text = "\n".join(f"[{m.role}]: {m.content[:500]}" for m in non_system if m.content)
                prompt = f"请将以下对话历史压缩为一段简洁摘要（300字以内），保留关键决策、人物变更、用户偏好：\n\n{history_text[-8000:]}"
                summary = ""
                async for chunk in self.llm.chat(
                    position="context_compression",
                    messages=[{"role": "user", "content": prompt}],
                    tools=None, stream=False, tag=":compress",
                ):
                    if chunk.type == "text_delta":
                        summary += chunk.content
                summary = summary.strip()[:500]
            except Exception:
                summary = f"项目={pi.get('name', '')}，已完成{len(session.messages)}轮对话"
            # 保留最近4条消息 + LLM摘要
            recent = non_system[-4:] if len(non_system) > 4 else non_system
            summary_msg = Message(role="user", content=f"[上下文压缩] {summary}")
            ctx.messages = [summary_msg] + recent
            session.messages = [m.to_dict() for m in ctx.messages]
            print(f"[compress] 完成: {pre_check_tokens} → {self.context_builder.token_counter.count_messages(ctx.to_llm_messages())} tokens ({time.time()-t0:.2f}s)", flush=True)

        # 确保用户消息已持久化，防止ReAct循环异常退出时丢失
        session.messages = [m.to_dict() for m in ctx.messages]
        # 重置中断标记，防止上次中断影响本次请求
        session.stop_requested = False

        # 6. ReAct loop
        turn = 0
        last_tool_calls = []
        final_text = ""

        while turn < self.max_turns:
            turn += 1
            if session.stop_requested:
                await record("interrupted", "user", {"reason": "stop_requested"})
                await finish("interrupted")
                yield ResponseChunk(type="done", data={"finish_reason": "interrupted", "message": "用户中断"})
                return

            llm_messages = ctx.to_llm_messages()
            tool_schemas = self.tools.get_schemas()
            await record("llm_request", "main_agent", {
                "turn": turn, "position": "main_loop", "message_count": len(llm_messages), "tool_count": len(tool_schemas),
            })

            # Debug: 打印请求消息
            import sys
            sys.stdout.flush()
            print(f"[LLM:main] 请求: messages={len(llm_messages)}", flush=True)
            for i, m in enumerate(llm_messages):
                print(f"  [{i}] {json.dumps(m, ensure_ascii=False)}", flush=True)

            # LLM 流式调用
            full_response = ""
            full_reasoning = ""
            current_tool_calls = []
            try:
                async for chunk in self.llm.chat(
                    position="main_loop", messages=llm_messages,
                    tools=tool_schemas, stream=True, tag=":main",
                ):
                    if chunk.type == "thinking":
                        full_reasoning += chunk.content
                        yield ResponseChunk(type="thinking", data={"content": chunk.content})
                    elif chunk.type == "text_delta":
                        full_response += chunk.content
                        yield ResponseChunk(type="text_delta", data={"delta": chunk.content})
                    elif chunk.type == "tool_use":
                        if not chunk.tool_name:
                            continue
                        current_tool_calls.append(chunk)
                        yield ResponseChunk(type="tool_call", data={"tool": chunk.tool_name, "params": chunk.tool_input})
                    elif chunk.type == "error":
                        print(f"[LLM:main] 请求消息数={len(llm_messages)}", flush=True)
                        for i, m in enumerate(llm_messages[-6:]):
                            print(f"  [{i}] {json.dumps(m, ensure_ascii=False)[:200]}", flush=True)
                        await record("error", "main_agent", {"message": chunk.error, "turn": turn})
                        await finish("failed")
                        yield ResponseChunk(type="error", data={"message": chunk.error, "trace_id": trace_id})
                        return
                    elif chunk.type == "done":
                        break
            except Exception as e:
                import traceback
                print(f"[LLM] 调用异常: {e}", flush=True)
                traceback.print_exc()
                await record("error", "main_agent", {"message": str(e), "turn": turn})
                await finish("failed")
                yield ResponseChunk(type="error", data={"message": f"LLM调用失败: {e}", "trace_id": trace_id})
                return

            await record("assistant_turn", "main_agent", {
                "turn": turn,
                "content": full_response,
                "reasoning_content": full_reasoning,
                "tool_names": [call.tool_name for call in current_tool_calls],
            })
            print(f"[LLM:main] 循环结束: turn={turn}, text_len={len(full_response)}, tool_calls={len(current_tool_calls)}", flush=True)

            # No tool calls → final answer
            if not current_tool_calls:
                final_text = full_response
                ctx.messages.append(Message(role="assistant", content=final_text, reasoning_content=full_reasoning))
                break

            # 追加assistant(tool_calls)消息
            assistant_msg = build_assistant_message(full_response, full_reasoning, current_tool_calls, turn)
            ctx.messages.append(Message(**assistant_msg))

            # Process tool calls
            for tc in current_tool_calls:
                tool_name = tc.tool_name
                params = tc.tool_input
                tool_event_id = await record("tool_call", "main_agent", {"tool": tool_name, "params": params, "turn": turn})

                # Security check
                tool = self.tools.get(tool_name)
                perm = self.security.check(tool, params, tool_ctx)

                if perm == PermissionResult.BLOCK:
                    error_msg = f"操作被安全策略禁止: {tool_name}"
                    await record("permission", "system", {"tool": tool_name, "decision": "block"}, tool_event_id)
                    await record("tool_result", "tool", {"tool": tool_name, "success": False, "error": error_msg}, tool_event_id)
                    yield ResponseChunk(type="error", data={"message": error_msg})
                    ctx.messages.append(Message(role="tool_result", content=error_msg, tool_call_id=tool_name))
                    continue

                if perm == PermissionResult.ASK:
                    # 创建等待事件，暂停执行直到用户响应
                    session.permission_event = asyncio.Event()
                    yield ResponseChunk(type="permission_ask", data={
                        "tool": tool_name,
                        "params_summary": str(params)[:200],
                    })
                    await record("permission", "system", {"tool": tool_name, "decision": "ask", "params_summary": str(params)[:200]}, tool_event_id)
                    # 阻塞等待用户确认
                    await session.permission_event.wait()
                    if not session.permission_granted:
                        error_msg = f"用户拒绝了操作: {tool_name}"
                        yield ResponseChunk(type="tool_result", data={
                            "tool": tool_name, "success": False, "error": error_msg,
                        })
                        await record("permission_response", "user", {"tool": tool_name, "allowed": False}, tool_event_id)
                        await record("tool_result", "tool", {"tool": tool_name, "success": False, "error": error_msg}, tool_event_id)
                        ctx.messages.append(Message(role="tool_result", content=error_msg, tool_call_id=tool_name))
                        continue
                    # 用户同意 → 记录到会话内批准列表
                    await record("permission_response", "user", {"tool": tool_name, "allowed": True}, tool_event_id)
                    tool_ctx.mark_allowed(f"{tool_name}:{hashlib.md5(str(params).encode()).hexdigest()[:8]}")

                # Execute tool — AskUserQuestion pauses and waits for user
                if tool_name == "AskUserQuestion":
                    yield ResponseChunk(type="question_ask", data={
                        "questions": params.get("questions", []),
                    })
                    await record("question_ask", "main_agent", {"questions": params.get("questions", [])}, tool_event_id)
                    session.question_event = asyncio.Event()
                    session.question_answers = None
                    await session.question_event.wait()
                    answers = session.question_answers or []
                    ctx.messages.append(Message(
                        role="tool_result",
                        content=json.dumps(answers, ensure_ascii=False),
                        tool_call_id=getattr(tc, 'tool_call_id', '') or f"call_{turn}",
                    ))
                    yield ResponseChunk(type="tool_result", data={
                        "tool": tool_name,
                        "success": True,
                        "data": json.dumps(answers, ensure_ascii=False),
                    })
                    await record("user_answer", "user", {"answers": answers}, tool_event_id)
                    await record("tool_result", "tool", {"tool": tool_name, "success": True, "data": answers}, tool_event_id)
                    continue

                # Execute tool — SubAgent gets special streaming treatment
                if tool_name == "SubAgent" and self.subagent_runner:
                    preset = params.get("preset", "")
                    task = params.get("task", "")
                    inherit = params.get("inherit_history", None)
                    attachments = params.get("attachments", None)
                    show_result = params.get("show_result", None)
                    if show_result is None:
                        # 从预设配置读取默认值
                        preset_config = self.subagent_runner.presets_tool.get_preset(preset)
                        show_result = preset_config.get("show_result", False) if preset_config else False
                    t0 = time.time()
                    subagent_result_text = ""
                    async for sub_chunk in self.subagent_runner.spawn_and_run(
                        preset, task, session, inherit,
                        extra_context=rag_context,
                        attachments=attachments,
                    ):
                        await record(f"subagent_{sub_chunk.type}", f"subagent:{preset}", sub_chunk.data, tool_event_id)
                        if sub_chunk.type == "subagent_done":
                            subagent_result_text = sub_chunk.data.get("result", "")
                            # 转发subagent_done让前端显示完成消息+结果预览
                            yield sub_chunk
                            if show_result and subagent_result_text and not subagent_result_text.startswith("Error:"):
                                yield ResponseChunk(type="subagent_result", data={"preset": preset, "content": subagent_result_text})
                        else:
                            yield sub_chunk  # 转发子Agent的所有事件到前端

                    if subagent_result_text.startswith("Error:"):
                        result = ToolResult(success=False, error=subagent_result_text)
                    else:
                        result = ToolResult(success=True, data=subagent_result_text)
                    duration = (time.time() - t0) * 1000
                else:
                    t0 = time.time()
                    result = await self.tools.execute(tool_name, params, tool_ctx)
                    duration = (time.time() - t0) * 1000
                if tool_name == "CreateTraceCheckpoint" and result.success:
                    trace_checkpoint_requested = True
                duration = (time.time() - t0) * 1000

                audit_log(trace_id, tool_name, params,
                          "BLOCK" if perm == PermissionResult.BLOCK else "ALLOWED",
                          result.data if result.success else result.error,
                          duration)
                await record("tool_result", "tool", {
                    "tool": tool_name,
                    "success": result.success,
                    "data": result.data if result.success else "",
                    "error": result.error if not result.success else "",
                }, tool_event_id, duration)

                # SubAgent的结果已通过subagent_done事件推送给前端，不再重复发送tool_result
                if tool_name != "SubAgent":
                    yield ResponseChunk(type="tool_result", data={
                        "tool": tool_name,
                        "success": result.success,
                        "data": result.data,
                        "error": result.error,
                    })

                # Use LLM-assigned tool_call_id, fallback to our own
                tc_id = getattr(tc, 'tool_call_id', '') or f"call_{turn}"
                ctx.messages.append(Message(
                    role="tool_result",
                    content=result.data if result.success else f"Error: {result.error}",
                    tool_call_id=tc_id,
                ))

            # 每轮结束后更新 + 立即同步持久化
            session.messages = [m.to_dict() for m in ctx.messages]
            try:
                from novelagent.storage import models
                await models.save_messages(session.session_id, session.messages, session.token_count, llm_client=self.llm)
            except Exception as _e:
                print(f"[save] 保存失败: {_e}", flush=True)

            # Loop detection
            if current_tool_calls == last_tool_calls:
                loop_count = getattr(self, '_loop_count', 0) + 1
                self._loop_count = loop_count
                if loop_count >= self.loop_threshold:
                    await record("loop_detected", "system", {"turn": turn})
                    await finish("interrupted")
                    yield ResponseChunk(type="done", data={"finish_reason": "loop_detected"})
                    return
            else:
                self._loop_count = 0
            last_tool_calls = current_tool_calls

        # 7. The analyzer derives trace-backed atomic memories after the immutable trace is complete.
        token_count = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
        session.messages = [m.to_dict() for m in ctx.messages]
        captured_trace = None
        if self.trace:
            await self.trace.store.append_session_turn(session.session_id, request.content, final_text)
            pending_turns = await self.trace.store.pending_trace_turn_count(session.session_id)
            if trace_checkpoint_requested or (self.trace_interval > 0 and pending_turns >= self.trace_interval):
                captured_trace = await self.trace.store.capture_pending_trace(
                    session.session_id, session.project_id, ctx.to_llm_messages(), token_count,
                )
        if self.post_turn_analyzer and captured_trace:
            asyncio.create_task(self.post_turn_analyzer.analyze(
                captured_trace["trace_id"],
                session.project_id,
                captured_trace["messages"],
                self.tools.get_schemas(),
            ))

        # 8. Done
        yield ResponseChunk(type="done", data={
            "finish_reason": "complete",
            "turns": turn,
            "token_count": token_count,
            "trace_id": captured_trace["trace_id"] if captured_trace else "",
        })
