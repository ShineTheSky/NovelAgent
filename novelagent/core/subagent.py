"""子Agent创建与编排"""

import json
from novelagent.tools.subagent_tool import SubAgentTool
from novelagent.core.session import Session, ResponseChunk
from novelagent.tools.base import ToolContext, ToolResult
from novelagent.core.llm_turn import query


class SubAgentRunner:
    """子Agent执行器"""

    def __init__(self, llm_client, tool_registry, permission_checker, context_builder, working_dir: str = "./workspace"):
        self.llm = llm_client
        self.tools = tool_registry
        self.security = permission_checker
        self.context = context_builder
        self.presets_tool = SubAgentTool()
        self.working_dir = working_dir
        from novelagent.context.prompt_manager import PromptManager
        self.prompt_manager = PromptManager("prompts")

    def _build_system_prompt(self, preset: dict, extra_context: str = "", attachments: list[dict] | None = None) -> str:
        from datetime import datetime
        tools_desc = "\n".join(f"- {name}" for name in preset.get("tools", []))
        att_text = ""
        if attachments:
            att_lines = ["## 参考文件内容（由主Agent提供，请直接使用，无需再读文件）"]
            for att in attachments:
                if isinstance(att, str):
                    att_lines.append(f"\n{att}")
                elif isinstance(att, dict):
                    att_lines.append(f"\n### {att.get('path', '')}\n{att.get('content', '')}")
            att_text = "\n".join(att_lines)
        ctx = f"{att_text}\n\n{extra_context}" if att_text else extra_context
        return self.prompt_manager.render("base_subagent.j2", {
            "role_definition": preset.get("system_prompt", ""),
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "project": {"name": "", "genre": "", "word_count": 0},
            "memory_md_content": "",
            "memory_enabled": preset.get("memory_enabled", False),
            "tools_description": f"可用工具: {tools_desc}" if tools_desc else "",
            "extra_context": ctx,
        })

    async def spawn_and_run(
        self, preset_name: str, task: str, parent_session: Session,
        inherit_history: bool | None = None, extra_context: str = "",
        attachments: list[dict] | None = None,
    ):
        preset = self.presets_tool.get_preset(preset_name)
        if preset is None:
            yield ResponseChunk(type="error", data={"message": f"子Agent预设 '{preset_name}' 不存在"})
            return

        inherit = inherit_history if inherit_history is not None else preset.get("inherit_history", True)
        max_turns = preset.get("max_turns", 8)
        tool_names = preset.get("tools", [])

        yield ResponseChunk(type="thinking", data={"content": f"启动子Agent: {preset_name} — {preset.get('description', '')}"})

        system_prompt = self._build_system_prompt(preset, extra_context, attachments)
        initial_messages = []
        if inherit:
            parent_msgs = parent_session.messages[-20:] if len(parent_session.messages) > 20 else parent_session.messages
            initial_messages.extend(parent_msgs)
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

        async def execute_tool(tool_name, params, tool_call_id):
            if tool_name == "AskUserQuestion":
                import asyncio
                parent_session.question_event = asyncio.Event()
                parent_session.question_answers = None
                await parent_session.question_event.wait()
                answers = parent_session.question_answers or []
                return True, json.dumps(answers, ensure_ascii=False), ""
            try:
                tool = self.tools.get(tool_name)
                sub_ctx = ToolContext(
                    session_id=parent_session.session_id,
                    project_id=parent_session.project_id,
                    working_dir=f"{self.working_dir}/{parent_session.project_id}",
                )
                result = await tool.execute(params, sub_ctx)
                data = result.data if isinstance(result.data, str) else json.dumps(result.data, ensure_ascii=False)
                return result.success, data, result.error
            except Exception as e:
                return False, "", str(e)

        try:
            async for ev in query(
                self.llm, position="sub_agent", messages=initial_messages,
                system_prompt=system_prompt, tools=sub_tool_schemas if sub_tool_schemas else None,
                max_turns=max_turns, tag=f":sub/{preset_name}",
                execute_tool=execute_tool, sub_type=preset_name,
            ):
                src = {"source": "subagent"}
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
                elif ev["type"] == "error":
                    yield ResponseChunk(type="error", data={"message": f"子Agent执行失败: {ev['error']}"})
                    return
                elif ev["type"] == "result":
                    result_text = ev["final_text"].strip() or "(子Agent未返回内容)"
        except Exception as e:
            yield ResponseChunk(type="error", data={"message": f"子Agent执行失败: {e}"})
            return

        print(f"[sub/{preset_name}] 完成: result_len={len(result_text)} preview={result_text[:100]}...", flush=True)
        yield ResponseChunk(type="subagent_done", data={"result": result_text})
