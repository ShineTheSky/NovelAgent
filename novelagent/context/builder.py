"""上下文组装器"""

from datetime import datetime
from novelagent.context.prompt_manager import PromptManager
from novelagent.context.message_manager import MessageManager, Message
from novelagent.context.token_counter import TokenCounter


class Context:
    def __init__(self, system_prompt: str, messages: list[Message], tool_context):
        self.system_prompt = system_prompt
        self.messages = messages
        self.tool_context = tool_context

    def to_llm_messages(self) -> list[dict]:
        result = [{"role": "system", "content": self.system_prompt}]
        result.extend(m.to_dict() for m in self.messages)
        return result


class ContextBuilder:
    def __init__(self, prompt_dir: str = "prompts", token_limit: int = 150_000):
        self.prompt_manager = PromptManager(prompt_dir)
        self.token_counter = TokenCounter()
        self.token_limit = token_limit

    async def build(
        self,
        project_name: str = "",
        project_genre: str = "",
        project_word_count: int = 0,
        memory_md_content: str = "",
        tools_description: str = "",
        history_messages: list[Message] | None = None,
        memory_injection: list[str] | None = None,
        preference_context: str = "",
    ) -> Context:
        # Build System Prompt
        system_prompt = self.prompt_manager.render("base_system.j2", {
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "project": {
                "name": project_name or "未命名项目",
                "genre": project_genre,
                "word_count": project_word_count,
            },
            "memory_md_content": memory_md_content or "暂无记忆索引",
            "preference_context": preference_context,
            "tools_description": tools_description,
        })

        # Build Messages
        messages = list(history_messages) if history_messages else []

        # Inject prefetched memories before user message
        if memory_injection:
            injection_text = "[相关记忆]\n" + "\n---\n".join(memory_injection)
            messages.insert(-1 if messages else 0, Message(role="system", content=injection_text))

        return Context(system_prompt=system_prompt, messages=messages, tool_context=None)
