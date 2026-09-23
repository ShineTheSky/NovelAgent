import asyncio

import pytest

import novelagent.core.agent_loop as agent_loop_module
from novelagent.core.agent_loop import AgentLoop, MainTurnResult
from novelagent.context.builder import Context
from novelagent.core.session import Session
from novelagent.llm.client import LLMResponse
from novelagent.tools.ask_user_question import AskUserQuestionTool
from novelagent.tools.base import PermissionResult
from novelagent.tools.registry import ToolRegistry


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


@pytest.mark.asyncio
async def test_ask_user_answer_forces_feedback_trace_capture(monkeypatch, tmp_path):
    class FakeProjectMemory:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare_context(self, _content):
            async def prefetch():
                return []

            return "", prefetch()

    class FakeTokenCounter:
        @staticmethod
        def count_messages(_messages):
            return 10

        @staticmethod
        def needs_compression(*_args):
            return False

    class FakeContextBuilder:
        token_counter = FakeTokenCounter()

        async def build(self, **kwargs):
            return Context("system", kwargs["history_messages"], None)

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMResponse(
                    type="tool_use", tool_name="AskUserQuestion", tool_call_id="ask-1",
                    tool_input={"questions": [{"question": "保留设定吗？"}]},
                )
            else:
                yield LLMResponse(type="text_delta", content="已按反馈继续。")
            yield LLMResponse(type="done")

    class FakeTraceStore:
        def __init__(self):
            self.captures = []

        async def append_session_turn(self, *_args):
            return 1

        async def pending_trace_turn_count(self, _session_id):
            return 1

        async def capture_pending_trace_window(self, _session_id, _project_id, messages,
                                               token_count, reason):
            self.captures.append({
                "messages": messages, "token_count": token_count, "reason": reason,
            })
            return None

    class FakeTrace:
        def __init__(self):
            self.store = FakeTraceStore()
            self.sequence = 0

        async def start(self, *_args):
            return "trace-1"

        async def record(self, *_args):
            self.sequence += 1
            return f"event-{self.sequence}"

        async def finish(self, *_args):
            pass

    class FakeSecurity:
        @staticmethod
        def check(*_args):
            return PermissionResult.ALLOW

    class BackgroundTasks:
        def __init__(self):
            self.tasks = []

        def submit(self, awaitable, *, label):
            self.tasks.append(asyncio.create_task(awaitable, name=label))

        async def drain(self):
            await asyncio.gather(*self.tasks)

    async def ignore_save(*_args, **_kwargs):
        pass

    monkeypatch.setattr(agent_loop_module, "MemoryManager", FakeProjectMemory)
    from novelagent.storage import models
    monkeypatch.setattr(models, "save_messages", ignore_save)

    registry = ToolRegistry()
    registry.register(AskUserQuestionTool())
    trace = FakeTrace()
    background = BackgroundTasks()
    loop = AgentLoop(
        FakeLLM(), registry, FakeSecurity(), FakeContextBuilder(), object(),
        {"working_dir": str(tmp_path), "trace_interval": 5},
        trace_recorder=trace, background_tasks=background,
    )
    session = Session(session_id="session-1", project_id="project-1")

    async def answer_question():
        while not getattr(session, "question_event", None):
            await asyncio.sleep(0)
        session.question_answers = ["保留，以后都这样写"]
        session.question_event.set()

    answer_task = None
    async for chunk in loop.run(
        agent_loop_module.InternalRequest(
            session_id=session.session_id, project_id=session.project_id, content="继续规划",
        ),
        session,
    ):
        if chunk.type == "question_ask":
            answer_task = asyncio.create_task(answer_question())

    await answer_task
    await background.drain()

    assert trace.store.captures[0]["reason"] == "user_feedback"
