"""Agent Loop — ReAct循环引擎"""

import asyncio
import copy
import uuid
import hashlib
import json
import time
from datetime import datetime, timezone
from novelagent.core.session import InternalRequest, ResponseChunk, Session
from novelagent.tools.base import ToolContext, ToolResult, PermissionResult
from novelagent.context.message_manager import Message, MessageManager
from novelagent.security.audit import audit_log
from novelagent.core.llm_turn import FINALIZATION_PROMPT, build_assistant_message
from novelagent.trace.recorder import TraceRecorder, sanitize_payload
from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
from novelagent.trace.stream_compaction import TraceStreamBuffer
from novelagent.memory.memory_manager import MemoryManager
from novelagent.core.review_workflow import ReviewPolishWorkflow


class AgentLoop:
    def __init__(self, llm_client, tool_registry, permission_checker, context_builder, memory_manager,
                 config: dict | None = None, subagent_runner=None, trace_recorder: TraceRecorder | None = None,
                 post_turn_analyzer=None, preference_context_provider=None, rag_store=None, bash_case_recorder=None,
                 bad_case_analyzer=None):
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
        self.bad_cases = AgentBadCaseRecorder(trace_recorder.store, self.working_dir, bad_case_analyzer) if trace_recorder else None
        self.bash_cases = bash_case_recorder
        self.post_turn_analyzer = post_turn_analyzer
        self.preference_context_provider = preference_context_provider
        self.rag_store = rag_store
        self.review_workflow = ReviewPolishWorkflow(subagent_runner, self.working_dir) if subagent_runner else None
        self._trace_capture_locks: dict[str, asyncio.Lock] = {}

    async def run(self, request: InternalRequest, session: Session, project_info: dict | None = None) -> ResponseChunk:
        trace_id = await self.trace.start(session.session_id, session.project_id, request.content) if self.trace else f"op_{uuid.uuid4().hex}"
        # The HTTP route can finish this trace if a client disconnects while the
        # generator is awaiting a tool or model response.
        session.active_trace_id = trace_id
        deferred_trace_events: list[dict] = []
        trace_event_sequence = 0

        async def record(event_type: str, actor: str, payload: dict | None = None,
                         parent_event_id: str | None = None, duration_ms: float | None = None):
            nonlocal trace_event_sequence
            trace_event_sequence += 1
            event_id = await self.trace.record(trace_id, event_type, actor, payload, parent_event_id, duration_ms) if self.trace else f"deferred_{trace_event_sequence}"
            deferred_trace_events.append({
                "event_id": event_id,
                "event_type": event_type,
                "actor": actor,
                "payload": sanitize_payload(payload or {}),
                "duration_ms": duration_ms,
            })
            return event_id

        async def finish(status: str, answer: str = "", token_count: int = 0):
            if self.trace:
                await self.trace.finish(trace_id, status, answer, token_count)
            if getattr(session, "active_trace_id", "") == trace_id:
                session.active_trace_id = ""

        pi = project_info or {}
        trace_checkpoint_requested = False

        # 项目记忆预取仍与项目工作区绑定；自动 Trace 记忆则持久化到 SQLite。
        import os as _os
        project_memory = MemoryManager(
            _os.path.join(self.working_dir, session.project_id),
            self.llm,
            self._config.get("global_memory_dir"),
        )

        # 1. Read memory.md once and start relevance prefetch from that same snapshot.
        memory_md, memory_prefetch = project_memory.prepare_context(request.content)
        prefetch_task = asyncio.create_task(memory_prefetch)
        main_tool_names = [tool.name for tool in self.tools.list_all() if tool.name != "SearchRag"]
        tools_prompt = self.tools.get_tools_prompt(main_tool_names)
        main_tool_schemas = self.tools.get_schemas(main_tool_names)
        preference_context = ""
        if self.preference_context_provider:
            try:
                preference_context = await self.preference_context_provider.render(session.project_id)
            except Exception as exc:
                print(f"[trace] preference context failed: {exc}", flush=True)

        # 2. Build context while memory relevance prefetch runs in parallel.
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

        def capture_bad_case(failure_kind: str, actor: str, error: str, *, tool: str = "",
                             params: dict | None = None, duration_ms: float | None = None) -> None:
            if not self.bad_cases:
                return
            asyncio.create_task(self.bad_cases.capture(
                source_trace_id=trace_id,
                session_id=session.session_id,
                project_id=session.project_id,
                failure_kind=failure_kind,
                actor=actor,
                error=error,
                tool=tool,
                params=params,
                messages=ctx.to_llm_messages(),
                duration_ms=duration_ms,
            ))

        def capture_bash_case(*, permission: str, status: str, success: bool | None = None,
                              result: object = "", error: object = "", params: dict | None = None,
                              actor: str = "main_agent", duration_ms: float | None = None) -> None:
            if not self.bash_cases:
                return
            asyncio.create_task(self.bash_cases.capture(
                session_id=session.session_id,
                project_id=session.project_id,
                source_trace_id=trace_id,
                actor=actor,
                operation_id=f"op_{session.session_id}_{trace_id}",
                params=params,
                permission=permission,
                status=status,
                success=success,
                result=result,
                error=error,
                duration_ms=duration_ms,
            ))

        async def capture_trace_if_due(
            answer: str,
            messages_snapshot: list[dict],
            events_snapshot: list[dict],
            token_count: int,
            should_capture: bool,
            reason: str,
            append_turn: bool = True,
        ) -> None:
            """Persist an immutable Trace snapshot outside the interactive response."""
            if not self.trace:
                return
            try:
                capture_lock = self._trace_capture_locks.setdefault(session.session_id, asyncio.Lock())
                async with capture_lock:
                    if append_turn:
                        await self.trace.store.append_session_turn(
                            session.session_id, request.content, answer, events_snapshot, trace_id,
                        )
                        pending_turns = await self.trace.store.pending_trace_turn_count(session.session_id)
                        if not should_capture and (self.trace_interval <= 0 or pending_turns < self.trace_interval):
                            return
                    captured = await self.trace.store.capture_pending_trace_window(
                        session.session_id, session.project_id, messages_snapshot, token_count, reason,
                    )
                    if captured and self.post_turn_analyzer:
                        asyncio.create_task(self.post_turn_analyzer.analyze_window(
                            captured["window_id"], session.project_id, captured["messages"], main_tool_schemas,
                        ))
            except Exception as exc:
                print(f"[trace] checkpoint capture failed: {exc}", flush=True)

        def schedule_trace_capture(answer: str = "", *, force_capture: bool = False,
                                   append_turn: bool = True, messages_snapshot: list[dict] | None = None,
                                   token_count: int | None = None, reason: str = "interval") -> None:
            if not self.trace:
                return
            snapshot = copy.deepcopy(messages_snapshot if messages_snapshot is not None else ctx.to_llm_messages())
            event_snapshot = copy.deepcopy(deferred_trace_events)
            asyncio.create_task(capture_trace_if_due(
                answer,
                snapshot,
                event_snapshot,
                token_count if token_count is not None else self.context_builder.token_counter.count_messages(snapshot),
                trace_checkpoint_requested or force_capture,
                "agent_request" if trace_checkpoint_requested else reason,
                append_turn,
            ))

        await record("context_built", "system", {
            "history_message_count": len(history_msgs),
            "memory_index_present": bool(memory_md),
            "confirmed_preference_count": preference_context.count("\n-") + (1 if preference_context.startswith("-") else 0),
        })

        # 3. Wait for memory prefetch (1.5s timeout)
        memory_injection = []
        try:
            memories = await asyncio.wait_for(prefetch_task, timeout=1.5)
            if memories:
                memory_injection = memories
                injection_text = "[相关记忆附件]\n" + "\n---\n".join(memories)
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
            operation_id=f"op_{session.session_id}_{trace_id}",
            actor="main_agent",
            source_trace_id=trace_id,
        )
        ctx.tool_context = tool_ctx

        # 5.5. 进入循环前先检查是否需要压缩
        pre_check_tokens = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
        if self.context_builder.token_counter.needs_compression(ctx.to_llm_messages(), self.token_limit):
            t0 = time.time()
            print(f"[compress] 进入循环前触发压缩: {pre_check_tokens} tokens", flush=True)
            yield ResponseChunk(type="thinking", data={"content": "上下文过长，正在压缩…"})
            await record("context_compression", "system", {"before_token_count": pre_check_tokens})
            # Copy the complete context before compression, then persist and
            # analyze it in the background without delaying compression.
            schedule_trace_capture(
                force_capture=True,
                append_turn=False,
                messages_snapshot=ctx.to_llm_messages(),
                token_count=pre_check_tokens,
                reason="compression",
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
            session.token_count = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
            try:
                project_memory.rebuild_index()
            except OSError as exc:
                # The compression result is still valid if a project folder is
                # temporarily locked; the next successful rebuild will refresh
                # the sliding-window snapshot.
                print(f"[memory] rebuild after compression failed: {exc}", flush=True)
            print(f"[compress] 完成: {pre_check_tokens} → {session.token_count} tokens ({time.time()-t0:.2f}s)", flush=True)

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
                schedule_trace_capture(final_text)
                yield ResponseChunk(type="done", data={"finish_reason": "interrupted", "message": "用户中断",
                                                        "trace_id": ""})
                return

            llm_messages = ctx.to_llm_messages()
            tool_schemas = main_tool_schemas
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
                        capture_bad_case("main_agent_failure", "main_agent", chunk.error)
                        await finish("failed")
                        schedule_trace_capture(full_response)
                        yield ResponseChunk(type="error", data={"message": chunk.error,
                                                                  "trace_id": ""})
                        return
                    elif chunk.type == "done":
                        break
            except Exception as e:
                import traceback
                print(f"[LLM] 调用异常: {e}", flush=True)
                traceback.print_exc()
                await record("error", "main_agent", {"message": str(e), "turn": turn})
                capture_bad_case("main_agent_failure", "main_agent", str(e))
                await finish("failed")
                schedule_trace_capture(full_response)
                yield ResponseChunk(type="error", data={"message": f"LLM调用失败: {e}",
                                                          "trace_id": ""})
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
                revision_event_start = len(tool_ctx.revision_events)
                tool_event_id = await record("tool_call", "main_agent", {"tool": tool_name, "params": params, "turn": turn})

                # Security check
                tool = self.tools.get(tool_name)
                perm = self.security.check(tool, params, tool_ctx)

                if perm == PermissionResult.BLOCK:
                    error_msg = f"操作被安全策略禁止: {tool_name}"
                    await record("permission", "system", {"tool": tool_name, "decision": "block"}, tool_event_id)
                    await record("tool_result", "tool", {"tool": tool_name, "success": False, "error": error_msg}, tool_event_id)
                    if tool_name == "Bash":
                        capture_bash_case(permission="blocked", status="blocked", success=False, error=error_msg, params=params)
                    else:
                        capture_bad_case("tool_failure", "main_agent", error_msg, tool=tool_name, params=params)
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
                        if tool_name == "Bash":
                            capture_bash_case(permission="user_denied", status="not_executed", success=False, error=error_msg, params=params)
                        else:
                            capture_bad_case("tool_failure", "main_agent", error_msg, tool=tool_name, params=params)
                        ctx.messages.append(Message(role="tool_result", content=error_msg, tool_call_id=tool_name))
                        continue
                    # 用户同意 → 记录到会话内批准列表
                    await record("permission_response", "user", {"tool": tool_name, "allowed": True}, tool_event_id)
                    tool_ctx.mark_allowed(f"{tool_name}:{hashlib.md5(str(params).encode()).hexdigest()[:8]}")

                if tool_name == "Bash":
                    tool_ctx.permission_decision = "ask_user_approved" if perm == PermissionResult.ASK else "allowed"

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
                    artifact_context = ""
                    subagent_extra_context = ""
                    # A direct user review/polish receives the same service-built ordering as
                    # the automatic pipeline: newest body, volume outline, chapter outline.
                    if preset in {"reviewer", "chapter_polisher"} and self.review_workflow:
                        chapter_path = self.review_workflow.find_chapter_path(task, attachments)
                        if chapter_path:
                            try:
                                artifact_context, subagent_extra_context = self.review_workflow.build_context(
                                    session.project_id, chapter_path,
                                )
                            except (OSError, ValueError) as exc:
                                print(f"[workflow] build manual context failed: {exc}", flush=True)
                    elif preset == "chapter_writer" and self.review_workflow:
                        chapter_path = self.review_workflow.find_chapter_path(task, attachments)
                        if chapter_path:
                            try:
                                artifact_context, subagent_extra_context = self.review_workflow.build_writer_context(
                                    session.project_id, chapter_path,
                                )
                                handoff_marker = "[自动审阅与润色已完成]"
                                handoffs = [
                                    str(message.get("content", "")).split(handoff_marker, 1)[1].strip()
                                    for message in session.messages
                                    if handoff_marker in str(message.get("content", ""))
                                ]
                                if handoffs:
                                    subagent_extra_context += (
                                        "\n\n## 上一次润色交接摘要（正文在固定工件区，不要重复回传）\n"
                                        + handoffs[-1][:2_000]
                                    )
                            except (OSError, ValueError) as exc:
                                print(f"[workflow] build writer context failed: {exc}", flush=True)
                    t0 = time.time()
                    subagent_result_text = ""
                    subagent_failed = False
                    writer_revision_events = []
                    subagent_run_id = uuid.uuid4().hex
                    subagent_trace_stream = TraceStreamBuffer(record)
                    async for sub_chunk in self.subagent_runner.spawn_and_run(
                        preset, task, session, inherit,
                        attachments=attachments if not artifact_context else None,
                        extra_context=subagent_extra_context,
                        artifact_context=artifact_context,
                        actor=preset,
                        operation_id=f"{tool_ctx.operation_id}:{preset}",
                        run_id=subagent_run_id,
                        context_as_user_message=preset == "reviewer",
                    ):
                        await subagent_trace_stream.add(
                            f"subagent_{sub_chunk.type}", f"subagent:{preset}", sub_chunk.data, tool_event_id,
                        )
                        if sub_chunk.type == "tool_result" and not sub_chunk.data.get("success", False):
                            capture_bad_case(
                                "tool_failure", f"subagent:{preset}", sub_chunk.data.get("error", "子 Agent 工具调用失败"),
                                tool=sub_chunk.data.get("tool", ""), params=sub_chunk.data.get("params", {}),
                            )
                        if sub_chunk.type == "error":
                            subagent_failed = True
                            subagent_result_text = f"Error: {sub_chunk.data.get('message', '子 Agent 执行失败')}"
                            capture_bad_case(
                                "subagent_failure", f"subagent:{preset}", sub_chunk.data.get("message", "子 Agent 执行失败"),
                                tool="SubAgent", params={"preset": preset, "task": task},
                            )
                        if sub_chunk.type == "subagent_done":
                            subagent_result_text = sub_chunk.data.get("result", "")
                            writer_revision_events = sub_chunk.data.get("revision_events", [])
                            if sub_chunk.data.get("empty_result"):
                                subagent_failed = True
                                subagent_result_text = "Error: 子 Agent 未返回有效内容"
                                capture_bad_case(
                                    "empty_response", f"subagent:{preset}", "子 Agent 未返回有效内容",
                                    tool="SubAgent", params={"preset": preset, "task": task, "run_id": subagent_run_id},
                                )
                            # 转发subagent_done让前端显示完成消息+结果预览
                            yield sub_chunk
                            if show_result and not sub_chunk.data.get("empty_result") and subagent_result_text and not subagent_result_text.startswith("Error:"):
                                yield ResponseChunk(type="subagent_result", data={
                                    "source": "subagent", "run_id": subagent_run_id,
                                    "preset": preset, "content": subagent_result_text,
                                })
                        else:
                            yield sub_chunk  # 转发子Agent的所有事件到前端
                    await subagent_trace_stream.flush()

                    # Writer commits are the only automatic trigger.  Polisher commits do not recurse.
                    workflow_summaries = []
                    if preset == "chapter_writer" and self.review_workflow:
                        chapter_events = [event for event in writer_revision_events if self.review_workflow.is_chapter(event.get("path", ""))]
                        for event in chapter_events:
                            yield ResponseChunk(type="thinking", data={"content": "章节已写入，正在自动审阅并润色…", "workflow": "auto_review"})
                            workflow_trace_stream = TraceStreamBuffer(record)
                            async for workflow_chunk in self.review_workflow.run(
                                session, event["path"], task, f"{tool_ctx.operation_id}:{event['revision_id']}",
                                parent_run_id=subagent_run_id,
                            ):
                                if (
                                    workflow_chunk.type == "subagent_done"
                                    and workflow_chunk.data.get("workflow") == "auto_polish"
                                    and not workflow_chunk.data.get("empty_result")
                                ):
                                    workflow_summaries.append(workflow_chunk.data.get("result", ""))
                                await workflow_trace_stream.add(
                                    f"workflow_{workflow_chunk.type}",
                                    f"workflow:{workflow_chunk.data.get('workflow', 'review_polish')}",
                                    workflow_chunk.data, tool_event_id,
                                )
                                if workflow_chunk.type == "error":
                                    capture_bad_case(
                                        workflow_chunk.data.get("failure_kind", "subagent_failure"),
                                        f"workflow:{workflow_chunk.data.get('workflow', 'review_polish')}",
                                        workflow_chunk.data.get("message", "自动审阅或润色失败"),
                                        tool="SubAgent", params={"workflow": workflow_chunk.data.get("workflow", "")},
                                    )
                                elif workflow_chunk.type == "tool_result" and not workflow_chunk.data.get("success", False):
                                    capture_bad_case(
                                        "tool_failure",
                                        f"workflow:{workflow_chunk.data.get('workflow', 'review_polish')}",
                                        workflow_chunk.data.get("error", "工作流工具调用失败"),
                                        tool=workflow_chunk.data.get("tool", ""), params=workflow_chunk.data.get("params", {}),
                                    )
                                yield workflow_chunk
                            await workflow_trace_stream.flush()

                    if workflow_summaries:
                        # Keep only the concise post-write handoff in the coordinator history;
                        # the full body stays in the artifact file and is never copied back here.
                        handoff = "\n\n".join(summary[:2_000] for summary in workflow_summaries)
                        subagent_result_text += f"\n\n[自动审阅与润色已完成]\n{handoff}"

                    if subagent_result_text.startswith("Error:"):
                        result = ToolResult(success=False, error=subagent_result_text)
                    else:
                        result = ToolResult(success=True, data=subagent_result_text)
                    duration = (time.time() - t0) * 1000
                else:
                    t0 = time.time()
                    try:
                        result = await self.tools.execute(tool_name, params, tool_ctx)
                    except Exception as exc:
                        result = ToolResult(success=False, error=str(exc))
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
                    "revision_events": tool_ctx.revision_events[revision_event_start:],
                }, tool_event_id, duration)
                if not result.success and not (tool_name == "SubAgent" and subagent_failed):
                    capture_bad_case(
                        "tool_failure", "main_agent", result.error or f"{tool_name} 执行失败",
                        tool=tool_name, params=params, duration_ms=duration,
                    )

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
            session.token_count = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
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
                    schedule_trace_capture(final_text)
                    yield ResponseChunk(type="done", data={"finish_reason": "loop_detected",
                                                            "trace_id": ""})
                    return
            else:
                self._loop_count = 0
            last_tool_calls = current_tool_calls

        if not final_text.strip() and turn >= self.max_turns and last_tool_calls:
            capture_bad_case(
                "max_turns_exhausted", "main_agent",
                f"主 Agent 达到 {self.max_turns} 轮上限，最后一轮仍调用工具，已触发强制收尾",
                params={"max_turns": self.max_turns, "last_tool_call_count": len(last_tool_calls)},
            )
            ctx.messages.append(Message(role="user", content=FINALIZATION_PROMPT))
            await record("llm_request", "main_agent", {
                "turn": turn + 1, "position": "main_loop", "message_count": len(ctx.to_llm_messages()),
                "tool_count": 0, "forced_finalization": True,
            })
            final_reasoning = ""
            try:
                async for chunk in self.llm.chat(
                    position="main_loop", messages=ctx.to_llm_messages(),
                    tools=None, stream=True, tag=":main:finalize",
                ):
                    if chunk.type == "thinking":
                        final_reasoning += chunk.content
                        yield ResponseChunk(type="thinking", data={"content": chunk.content})
                    elif chunk.type == "text_delta":
                        final_text += chunk.content
                        yield ResponseChunk(type="text_delta", data={"delta": chunk.content})
                    elif chunk.type == "error":
                        capture_bad_case("main_agent_failure", "main_agent", chunk.error,
                                         params={"forced_finalization": True})
                        break
                    elif chunk.type == "done":
                        break
            except Exception as exc:
                capture_bad_case("main_agent_failure", "main_agent", str(exc),
                                 params={"forced_finalization": True})
            if final_text.strip():
                ctx.messages.append(Message(
                    role="assistant", content=final_text, reasoning_content=final_reasoning,
                ))
                await record("assistant_turn", "main_agent", {
                    "turn": turn + 1, "content": final_text,
                    "reasoning_content": final_reasoning, "tool_names": [],
                    "forced_finalization": True,
                })

        # 7. The analyzer derives trace-backed atomic memories after the immutable trace is complete.
        if not final_text.strip():
            capture_bad_case(
                "empty_response", "main_agent", "主 Agent 未返回有效内容",
                params={"turns": turn, "last_tool_call_count": len(last_tool_calls)},
            )
        token_count = self.context_builder.token_counter.count_messages(ctx.to_llm_messages())
        session.messages = [m.to_dict() for m in ctx.messages]
        session.token_count = token_count
        await finish("completed", final_text, token_count)

        # Queue capture before yielding the terminal SSE event.  It remains
        # asynchronous, but is not lost if the client closes immediately.
        schedule_trace_capture(final_text)

        # 8. Done
        yield ResponseChunk(type="done", data={
            "finish_reason": "complete",
            "turns": turn,
            "token_count": token_count,
            # Trace capture runs after the UI receives the terminal event.
            "trace_id": "",
        })
