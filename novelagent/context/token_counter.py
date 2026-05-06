"""Token精确计算"""

import tiktoken


class TokenCounter:
    def __init__(self, model: str = "cl100k_base"):
        try:
            self.encoder = tiktoken.get_encoding(model)
        except Exception:
            self.encoder = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        return len(self.encoder.encode(text))

    def count_messages(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            total += self.count(str(msg.get("content", "")))
            total += self.count(msg.get("role", ""))
            # Tool calls overhead
            for tc in msg.get("tool_calls", []):
                total += self.count(str(tc.get("name", "")))
                total += self.count(str(tc.get("input", "")))
        return total

    def needs_compression(self, messages: list[dict], token_limit: int, threshold: float = 0.8) -> bool:
        return self.count_messages(messages) >= token_limit * threshold
