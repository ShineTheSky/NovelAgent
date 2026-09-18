import pytest

from novelagent.core.agent_loop import AgentLoop, MainTurnResult
from novelagent.core.session import Session
from novelagent.llm.client import LLMResponse


class FakeLLM:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_stream_main_turn_collects_content_reasoning_and_tools():
    tool_call = LLMResponse(
        type="tool_use",
        tool_name="Read",
        tool_input={"path": "chapter.md"},
        tool_call_id="call-1",
    )
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = FakeLLM([
        LLMResponse(type="thinking", content="检查文件"),
        LLMResponse(type="text_delta", content="正在读取"),
        tool_call,
        LLMResponse(type="done"),
    ])
    result = MainTurnResult()

    chunks = [chunk async for chunk in loop._stream_main_turn(
        [{"role": "user", "content": "检查章节"}],
        [{"name": "Read"}],
        ":main",
        result,
    )]

    assert [chunk.type for chunk in chunks] == ["thinking", "text_delta", "tool_call"]
    assert result.reasoning == "检查文件"
    assert result.text == "正在读取"
    assert result.tool_calls == [tool_call]
    assert result.error == ""
    assert loop.llm.calls[0]["position"] == "main_loop"
    assert loop.llm.calls[0]["tools"] == [{"name": "Read"}]


@pytest.mark.asyncio
async def test_stream_main_turn_stores_error_without_emitting_error_chunk():
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = FakeLLM([
        LLMResponse(type="text_delta", content="部分结果"),
        LLMResponse(type="error", error="模型失败"),
    ])
    result = MainTurnResult()

    chunks = [chunk async for chunk in loop._stream_main_turn(
        [{"role": "user", "content": "任务"}], None, ":main:finalize", result,
    )]

    assert [chunk.type for chunk in chunks] == ["text_delta"]
    assert result.text == "部分结果"
    assert result.error == "模型失败"
    assert loop.llm.calls[0]["tools"] is None
    assert loop.llm.calls[0]["tag"] == ":main:finalize"


@pytest.mark.asyncio
async def test_persist_turn_updates_session_and_storage(monkeypatch):
    saved = {}

    async def fake_save_messages(session_id, messages, token_count, llm_client):
        saved.update(
            session_id=session_id,
            messages=messages,
            token_count=token_count,
            llm_client=llm_client,
        )

    class FakeMessage:
        def to_dict(self):
            return {"role": "assistant", "content": "完成"}

    class FakeContext:
        messages = [FakeMessage()]

        @staticmethod
        def to_llm_messages():
            return [{"role": "assistant", "content": "完成"}]

    class FakeTokenCounter:
        @staticmethod
        def count_messages(messages):
            assert messages == [{"role": "assistant", "content": "完成"}]
            return 7

    from novelagent.storage import models

    monkeypatch.setattr(models, "save_messages", fake_save_messages)
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = object()
    loop.context_builder = type("ContextBuilder", (), {"token_counter": FakeTokenCounter()})()
    session = Session(session_id="session-1", project_id="project-1")

    await loop._persist_turn(session, FakeContext())

    assert session.messages == [{"role": "assistant", "content": "完成"}]
    assert session.token_count == 7
    assert saved == {
        "session_id": "session-1",
        "messages": session.messages,
        "token_count": 7,
        "llm_client": loop.llm,
    }
