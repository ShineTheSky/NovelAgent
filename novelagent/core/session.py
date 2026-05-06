"""会话与请求/响应数据类"""

from dataclasses import dataclass, field
from typing import Literal, AsyncIterator


@dataclass
class InternalRequest:
    session_id: str
    project_id: str | None = None
    content: str = ""
    action: Literal["send", "stop"] = "send"
    accept_edits: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class ResponseChunk:
    type: Literal["text_delta", "thinking", "tool_call", "tool_result", "subagent_done", "subagent_result", "permission_ask", "question_ask", "done", "error"]
    data: dict = field(default_factory=dict)
    timestamp: str = ""

    def to_sse(self) -> str:
        import json
        from datetime import datetime, timezone
        ts = self.timestamp or datetime.now(timezone.utc).isoformat()
        payload = {"type": self.type, **self.data, "timestamp": ts}
        return f"event: {self.type}\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n"


@dataclass
class Session:
    session_id: str
    project_id: str
    title: str = ""
    messages: list = field(default_factory=list)
    token_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    # 会话内状态
    accept_edits_mode: bool = False
    stop_requested: bool = False
    previously_allowed: set = field(default_factory=set)
    # 权限等待（运行时动态设置，非dataclass字段初始化）
    permission_granted: bool = False
    # 用户问答（运行时动态设置）
    question_answers: list | None = None
