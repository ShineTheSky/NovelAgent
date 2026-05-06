"""消息列表管理"""

import json
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # user / assistant / system / tool_result
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    reasoning_content: str = ""

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            role=d.get("role", ""),
            content=d.get("content", ""),
            tool_calls=d.get("tool_calls", []),
            tool_call_id=d.get("tool_call_id", ""),
            name=d.get("name", ""),
            reasoning_content=d.get("reasoning_content", ""),
        )


class MessageManager:
    def __init__(self):
        self._messages: list[Message] = []

    def append(self, msg: Message):
        self._messages.append(msg)

    def get_messages(self) -> list[Message]:
        return list(self._messages)

    def clear(self):
        self._messages = []

    def get_tool_pair(self, tool_use_id: str) -> tuple[Message | None, Message | None]:
        tool_use = None
        tool_result = None
        for m in self._messages:
            if m.tool_calls:
                for tc in m.tool_calls:
                    if tc.get("id") == tool_use_id:
                        tool_use = m
            if m.tool_call_id == tool_use_id:
                tool_result = m
        return tool_use, tool_result

    def merge_subagent_result(self, preset_name: str, result_text: str):
        """合并子Agent结果到消息列表"""
        self._messages.append(Message(
            role="tool_result",
            content=result_text,
            name="SubAgent",
            tool_call_id=f"subagent_{preset_name}",
        ))

    def to_dicts(self) -> list[dict]:
        return [m.to_dict() for m in self._messages]

    def load_from_dicts(self, dicts: list[dict]):
        self._messages = [Message.from_dict(d) for d in dicts]

    def to_json(self) -> str:
        return json.dumps(self.to_dicts(), ensure_ascii=True)

    def load_from_json(self, json_str: str):
        self.load_from_dicts(json.loads(json_str))
