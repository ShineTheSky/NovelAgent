"""异步记忆预取。"""

import asyncio
import json

from novelagent.memory.index_manager import IndexManager


class Prefetcher:
    MAX_FILES = 5
    MAX_TOKENS = 3000
    _ALLOWED_PREFIXES = ("memory/", "user/", "feedback/", "reference/", "project/")

    def __init__(self, index_manager: IndexManager, file_store, llm_client=None, timeout: float = 1.5):
        self.index_manager = index_manager
        self.file_store = file_store
        self.llm_client = llm_client
        self.timeout = timeout

    async def fetch(self, user_message: str) -> list[str] | None:
        """预取相关的已提升记忆；Evidence 与 Pattern 由专用流程处理。"""
        if self.llm_client is None:
            return None
        try:
            return await asyncio.wait_for(self._do_fetch(user_message), timeout=self.timeout)
        except asyncio.TimeoutError:
            return None

    async def _do_fetch(self, user_message: str) -> list[str]:
        memory_md = self.index_manager.get_content()
        if not memory_md:
            return []
        prompt = f"""根据以下记忆索引和用户输入，选择最相关的已提升记忆文件（最多 {self.MAX_FILES} 条）。
不要选择 evidence/ 或 pattern/ 路径。

记忆索引:
{memory_md}

用户输入:
{user_message}

只返回 JSON: {{"relevant_files": [".memory/memory/xxx.md", ...]}}"""
        response_text = ""
        async for chunk in self.llm_client.chat(
            position="memory_prefetch",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
        ):
            if chunk.type == "text_delta":
                response_text += chunk.content
        try:
            selected = json.loads(response_text).get("relevant_files", [])[:self.MAX_FILES]
        except json.JSONDecodeError:
            return []

        results, total_tokens = [], 0
        for raw_path in selected:
            if not isinstance(raw_path, str):
                continue
            rel_path = raw_path.removeprefix(".memory/").replace("\\", "/")
            if not rel_path.startswith(self._ALLOWED_PREFIXES) or rel_path.startswith(("evidence/", "pattern/")):
                continue
            try:
                content = self.file_store.read(rel_path)
            except ValueError:
                continue
            if not content:
                continue
            estimate = len(content) // 2
            if total_tokens + estimate > self.MAX_TOKENS:
                remaining = self.MAX_TOKENS - total_tokens
                if remaining <= 200:
                    break
                content = content[:remaining * 2] + "\n[内容截断]"
            results.append(content)
            total_tokens += min(estimate, self.MAX_TOKENS - total_tokens)
        return results
