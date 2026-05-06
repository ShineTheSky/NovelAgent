"""异步记忆预取"""

import asyncio
from novelagent.memory.index_manager import IndexManager


class Prefetcher:
    MAX_FILES = 5
    MAX_TOKENS = 3000

    def __init__(self, index_manager: IndexManager, file_store, llm_client=None, timeout: float = 1.5):
        self.index_manager = index_manager
        self.file_store = file_store
        self.llm_client = llm_client
        self.timeout = timeout

    async def fetch(self, user_message: str) -> list[str] | None:
        """异步预取相关记忆文件原文。超时返回None。"""
        import sys
        if self.llm_client is None:
            print("[prefetch] 跳过: LLM客户端未配置", flush=True)
            return None

        try:
            print(f"[prefetch] 开始预取: {user_message[:50]}...", flush=True)
            result = await asyncio.wait_for(
                self._do_fetch(user_message),
                timeout=self.timeout
            )
            print(f"[prefetch] 完成: {len(result) if result else 0} 条记忆", flush=True)
            return result
        except asyncio.TimeoutError:
            print("[prefetch] 超时跳过", flush=True)
            return None

    async def _do_fetch(self, user_message: str) -> list[str]:
        memory_md = self.index_manager.get_content()
        if not memory_md:
            return []

        prompt = f"""根据以下记忆索引和用户输入，判断哪些记忆最相关（最多{self.MAX_FILES}条），返回文件路径列表。

记忆索引:
{memory_md}

用户输入:
{user_message}

只返回JSON: {{"relevant_files": [".memory/*/xxx.md", ...]}}"""

        # LLM call to determine relevance
        response_text = ""
        async for chunk in self.llm_client.chat(
            position="memory_prefetch",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
        ):
            if chunk.type == "text_delta":
                response_text += chunk.content

        # Parse response
        import json
        try:
            data = json.loads(response_text)
            files = data.get("relevant_files", [])[:self.MAX_FILES]
        except json.JSONDecodeError:
            return []

        # Read file contents
        results = []
        total_tokens = 0
        for file_path in files:
            content = self.file_store.read(file_path.replace(".memory/", ""))
            if not content:
                continue
            # Rough token estimate (4 chars ≈ 1 token for Chinese)
            est_tokens = len(content) // 2
            if total_tokens + est_tokens > self.MAX_TOKENS:
                remaining = self.MAX_TOKENS - total_tokens
                if remaining > 200:
                    content = content[:remaining * 2] + "\n[内容截断]"
                else:
                    break
            results.append(content)
            total_tokens += est_tokens

        return results
