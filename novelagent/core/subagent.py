"""子Agent创建与编排"""

import json
import uuid
from novelagent.tools.subagent_tool import SubAgentTool
from novelagent.core.session import Session, ResponseChunk
from novelagent.tools.base import ToolContext, ToolResult
from novelagent.tools.ask_user_question import resolve_question_answers
from novelagent.core.llm_turn import query
from novelagent.trace.agent_run import AgentRunTrace, AgentTraceTaskManager


class SubAgentRunner:
    """子Agent执行器"""

    def __init__(self, llm_client, tool_registry, permission_checker, context_builder,
                 working_dir: str = "./workspace", bad_case_recorder=None,
                 trace_tasks: AgentTraceTaskManager | None = None):
        self.llm = llm_client
        self.tools = tool_registry
        self.security = permission_checker
        self.context = context_builder
        self.presets_tool = SubAgentTool()
        self.working_dir = working_dir
        self.bad_cases = bad_case_recorder
        self.trace_tasks = trace_tasks
        from novelagent.context.prompt_manager import PromptManager
        self.prompt_manager = PromptManager("prompts")

    @staticmethod
    def _artifact_context(artifact_context: str, attachments: list[dict] | None) -> str:
        if artifact_context:
            return artifact_context
        if not attachments:
            return ""
        att_lines = ["## 参考文件内容（由主Agent提供，请直接使用，无需再读文件）"]
        for att in attachments:
            if isinstance(att, str):
                att_lines.append(f"\n{att}")
            elif isinstance(att, dict):
                att_lines.append(f"\n### {att.get('path', '')}\n{att.get('content', '')}")
        return "\n".join(att_lines)

    def _build_system_prompt(
        self, preset: dict, extra_context: str = "", attachments: list[dict] | None = None,
        artifact_context: str = "",
    ) -> str:
        from datetime import datetime
        tool_names = preset.get("tools", [])
        tools_desc = self.tools.get_tools_prompt(tool_names) if tool_names else ""
        return self.prompt_manager.render("base_subagent.j2", {
            "role_definition": preset.get("system_prompt", ""),
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "project": {"name": "", "genre": "", "word_count": 0},
            "memory_md_content": "",
            "memory_enabled": preset.get("memory_enabled", False),
            "tools_description": tools_desc,
            "artifact_context": self._artifact_context(artifact_context, attachments),
            "extra_context": extra_context,
        })

    async def spawn_and_run(
        self, preset_name: str, task: str, parent_session: Session,
        inherit_history: bool | None = None, extra_context: str = "",
        attachments: list[dict] | None = None, artifact_context: str = "",
        actor: str | None = None, operation_id: str = "",
        run_id: str = "", parent_run_id: str = "", workflow: str = "",
        context_as_user_message: bool = False, source_trace_id: str = "",
    ):
        preset = self.presets_tool.get_preset(preset_name)
        if preset is None:
            yield ResponseChunk(type="error", data={"message": f"子Agent预设 '{preset_name}' 不存在"})
            return

        inherit = inherit_history if inherit_history is not None else preset.get("inherit_history", True)
        max_turns = preset.get("max_turns", 20)
        tool_names = preset.get("tools", [])

        current_run_id = run_id or uuid.uuid4().hex
        src = {
            "source": "subagent",
            "run_id": current_run_id,
            "parent_run_id": parent_run_id or None,
            "preset": preset_name,
            "workflow": workflow or None,
        }
        yield ResponseChunk(type="subagent_start", data={
            **src,
            "task_summary": task.strip().replace("\n", " ")[:160],
            "task_prompt": task.strip(),
        })

        fixed_context = self._artifact_context(artifact_context, attachments)
        system_prompt = self._build_system_prompt(
            preset,
            "" if context_as_user_message else extra_context,
            None if context_as_user_message else attachments,
            "" if context_as_user_message else artifact_context,
        )
        initial_messages = []
        if inherit:
            parent_msgs = parent_session.messages[-20:] if len(parent_session.messages) > 20 else parent_session.messages
            initial_messages.extend(parent_msgs)
        if context_as_user_message:
            runtime_context = "\n\n".join(part for part in (fixed_context, extra_context) if part)
            if runtime_context:
                initial_messages.append({"role": "user", "content": runtime_context})
        initial_messages.append({"role": "user", "content": task})

        # Debug
        import sys
        print(f"[sub/{preset_name}] tools: {tool_names}", flush=True)

        sub_tool_schemas = []
        for name in tool_names:
            if name == "SubAgent":
                continue
            try:
                sub_tool_schemas.append(self.tools.get(name).get_schema())
            except Exception:
                pass

        revision_events: list[dict] = []
        agent_trace = None
        if self.bad_cases:
            agent_trace = await AgentRunTrace.try_start(
                self.bad_cases.store, session_id=parent_session.session_id,
                project_id=parent_session.project_id, actor=f"subagent:{preset_name}",
                position="sub_agent", source_trace_id=source_trace_id,
                title=f"[subagent:{preset_name}] {task.strip()[:120]}",
            )
            if agent_trace:
                agent_trace.add_request(
                    position="sub_agent", tag=f":sub/{preset_name}",
                    messages=[{"role": "system", "content": system_prompt}, *initial_messages],
                    tools=sub_tool_schemas,
                )

        def record_agent_event(event_type: str, payload: dict) -> None:
            if agent_trace:
                agent_trace.add(event_type, payload, actor=f"subagent:{preset_name}")

        async def capture_bad_case(failure_kind: str, error: str, *, messages=None,
                                   tool: str = "SubAgent", params: dict | None = None) -> None:
            if not self.bad_cases:
                return
            if agent_trace:
                await agent_trace.flush()
            await self.bad_cases.capture(
                source_trace_id=agent_trace.trace_id if agent_trace else (
                    source_trace_id or operation_id or parent_run_id or current_run_id
                ),
                session_id=parent_session.session_id, project_id=parent_session.project_id,
                failure_kind=failure_kind, actor=f"subagent:{preset_name}", error=error,
                tool=tool, params=params or {
                    "preset": preset_name, "task": task, "run_id": current_run_id,
                },
                messages=messages or [{"role": "system", "content": system_prompt}, *initial_messages],
            )

        async def execute_tool(tool_name, params, tool_call_id):
            if tool_name == "AskUserQuestion":
                import asyncio
                parent_session.question_event = asyncio.Event()
                parent_session.question_answers = None
                await parent_session.question_event.wait()
                answers = parent_session.question_answers or []
                resolved_answers = resolve_question_answers(params.get("questions", []), answers)
                return True, json.dumps(resolved_answers, ensure_ascii=False), ""
            try:
                tool = self.tools.get(tool_name)
                sub_ctx = ToolContext(
                    session_id=parent_session.session_id,
                    project_id=parent_session.project_id,
                    working_dir=f"{self.working_dir}/{parent_session.project_id}",
                    actor=actor or preset_name,
                    operation_id=operation_id,
                    revision_events=revision_events,
                    permission_decision="not_checked",
                    source_trace_id=operation_id,
                )
                result = await tool.execute(params, sub_ctx)
                data = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)
                return result.success, data, result.error
            except Exception as e:
                return False, "", str(e)

        result_text = ""
        empty_result = True
        try:
            async for ev in query(
                self.llm, position="sub_agent", messages=initial_messages,
                system_prompt=system_prompt, tools=sub_tool_schemas if sub_tool_schemas else None,
                max_turns=max_turns, tag=f":sub/{preset_name}",
                execute_tool=execute_tool, sub_type=preset_name,
            ):
                if ev["type"] in {"thinking", "text_delta"}:
                    record_agent_event(ev["type"], {"content": ev["content"]})
                elif ev["type"] == "tool_call":
                    record_agent_event("tool_call", {
                        "tool": ev["tool"], "params": ev["params"],
                        "tool_call_id": ev["tool_call_id"],
                    })
                elif ev["type"] == "tool_result":
                    record_agent_event("tool_result", {
                        "tool": ev["tool"], "success": ev["success"],
                        "data": ev["data"], "error": ev["error"],
                    })
                    if not ev["success"]:
                        await capture_bad_case(
                            "tool_failure", ev["error"] or f"{ev['tool']} 执行失败",
                            tool=ev["tool"], params={"preset": preset_name},
                        )
                if ev["type"] == "thinking":
                    yield ResponseChunk(type="thinking", data={**src, "content": ev["content"]})
                elif ev["type"] == "text_delta":
                    yield ResponseChunk(type="text_delta", data={**src, "delta": ev["content"]})
                elif ev["type"] == "tool_call":
                    yield ResponseChunk(type="tool_call", data={**src, "tool": ev["tool"], "params": ev["params"]})
                elif ev["type"] == "tool_result":
                    yield ResponseChunk(type="tool_result", data={**src, "tool": ev["tool"], "success": ev["success"], "data": ev["data"], "error": ev["error"]})
                elif ev["type"] == "question_ask":
                    yield ResponseChunk(type="question_ask", data={**src, "questions": ev["questions"]})
                elif ev["type"] == "max_turns_exhausted":
                    await capture_bad_case(
                        "max_turns_exhausted",
                        f"子 Agent 达到 {ev['turns']} 轮上限，最后一轮仍调用工具，已触发强制收尾",
                        messages=ev["messages"],
                    )
                elif ev["type"] == "error":
                    record_agent_event("error", {"message": ev["error"]})
                    if agent_trace:
                        await agent_trace.finish("failed", error=ev["error"])
                    await capture_bad_case("subagent_failure", ev["error"])
                    yield ResponseChunk(type="error", data={**src, "message": f"子Agent执行失败: {ev['error']}"})
                    return
                elif ev["type"] == "result":
                    empty_result = not ev["final_text"].strip()
                    result_text = ev["final_text"].strip() or "(子Agent未返回内容)"
        except Exception as e:
            record_agent_event("error", {"message": str(e)})
            if agent_trace:
                await agent_trace.finish("failed", error=str(e))
            await capture_bad_case("subagent_failure", str(e))
            yield ResponseChunk(type="error", data={**src, "message": f"子Agent执行失败: {e}"})
            return

        if empty_result:
            if agent_trace:
                await agent_trace.finish("failed", error="子 Agent 未返回有效内容")
            await capture_bad_case("empty_response", "子 Agent 未返回有效内容")
        elif agent_trace:
            if self.trace_tasks:
                self.trace_tasks.submit(
                    agent_trace.finish("completed", result_text),
                    label=f"trace:{agent_trace.trace_id}",
                )
            else:
                await agent_trace.finish("completed", result_text)

        print(f"[sub/{preset_name}] 完成: result_len={len(result_text)} preview={result_text[:100]}...", flush=True)
        yield ResponseChunk(type="subagent_done", data={
            **src, "result": result_text, "empty_result": empty_result,
            "revision_events": revision_events,
            "agent_trace_id": agent_trace.trace_id if agent_trace else "",
        })
