"""自动记忆轮询"""

import asyncio
from novelagent.memory.index_manager import IndexManager
from novelagent.trace.agent_run import AgentRunTrace


class AutoMemory:
    def __init__(self, index_manager: IndexManager, file_store=None, llm_client=None, interval: int = 5, *,
                 bad_case_recorder=None, session_id: str = "", project_id: str = "",
                 source_trace_id: str = ""):
        self.index_manager = index_manager
        self.file_store = file_store
        self.llm_client = llm_client
        self.interval = interval
        self.bad_cases = bad_case_recorder
        self.session_id = session_id
        self.project_id = project_id
        self.source_trace_id = source_trace_id

    def should_trigger(self, round_num: int) -> bool:
        return round_num > 0 and round_num % self.interval == 0

    def run_background(self, recent_messages: list):
        """后台触发自动记忆（不阻塞主流程）"""
        if self.llm_client is None:
            return
        asyncio.create_task(self._do_run(recent_messages))

    async def _do_run(self, recent_messages: list):
        """实际执行自动记忆轮询"""
        agent_trace = None
        if self.bad_cases:
            agent_trace = await AgentRunTrace.try_start(
                self.bad_cases.store, session_id=self.session_id, project_id=self.project_id,
                actor="auto_memory", position="auto_memory", source_trace_id=self.source_trace_id,
                title="[auto_memory] 自动记忆轮询",
            )
        execution_events: list[dict] = agent_trace.events if agent_trace else []
        messages: list[dict] = []
        try:
            memory_md = self.index_manager.get_content()
            recent_text = "\n".join(
                f"[{m.role}]: {m.content[:200]}"
                for m in recent_messages[-10:] if hasattr(m, 'role')
            )

            prompt = f"""以上对话中是否有值得长期记忆的信息？
            当前记忆索引:
            {memory_md}

            最近对话:
            {recent_text}

            如果有值得记忆的内容，请用 Write 工具写入 .memory/ 目录,并更新索引文件memory.md！格式:
            ---
            type: <user|feedback|reference|project>
            tags: [标签]
            summary: 摘要
            ---
            正文

            记忆类型说明:
            - user: 用户的写作偏好、习惯
            - feedback: 用户指出的错误和纠正
            - reference: 参考资料、外部知识
            - project: 项目相关的重要决策、人物变更

            如果没有值得记忆的内容，回复"无"。只回复"无"或调用Write工具，不要回复其他内容。"""

            response_text = ""
            messages = [{"role": "user", "content": prompt}]
            if agent_trace:
                agent_trace.add_request(
                    position="auto_memory", tag=":auto-memory", messages=messages, tools=None,
                )
            async for chunk in self.llm_client.chat(
                position="auto_memory",
                messages=messages,
                tools=None,
                stream=False,
            ):
                if chunk.type == "text_delta":
                    response_text += chunk.content
                    execution_events.append({
                        "event_type": "text_delta", "actor": "auto_memory",
                        "payload": {"content": chunk.content},
                    })
                elif chunk.type == "thinking":
                    execution_events.append({
                        "event_type": "thinking", "actor": "auto_memory",
                        "payload": {"content": chunk.content},
                    })
                elif chunk.type == "error":
                    execution_events.append({
                        "event_type": "error", "actor": "auto_memory",
                        "payload": {"message": chunk.error},
                    })
                    raise RuntimeError(chunk.error or "自动记忆调用失败")

            if "无" in response_text[:20] and len(response_text) < 50:
                if agent_trace:
                    await agent_trace.finish("completed", response_text)
                return

            print(f"[auto_memory] 发现可记忆内容: {response_text[:100]}...", flush=True)
            if agent_trace:
                await agent_trace.finish("completed", response_text)
        except Exception as e:
            print(f"[auto_memory] 失败: {e}", flush=True)
            if self.bad_cases:
                if agent_trace:
                    await agent_trace.finish("failed", error=str(e))
                await self.bad_cases.capture(
                    source_trace_id=agent_trace.trace_id if agent_trace else self.source_trace_id,
                    session_id=self.session_id,
                    project_id=self.project_id, failure_kind="auto_memory_failure",
                    actor="auto_memory", error=str(e), messages=messages,
                )
