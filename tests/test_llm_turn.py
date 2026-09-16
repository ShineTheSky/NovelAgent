import pytest

from novelagent.core.llm_turn import FINALIZATION_PROMPT, query
from novelagent.llm.client import LLMResponse


class FakeLLM:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        for chunk in self.turns.pop(0):
            yield chunk


@pytest.mark.asyncio
async def test_query_finishes_normally_without_extra_turn():
    llm = FakeLLM([[
        LLMResponse(type="text_delta", content="完成"),
        LLMResponse(type="done"),
    ]])

    async def execute_tool(*_args):
        raise AssertionError("不应执行工具")

    events = [event async for event in query(
        llm, position="sub_agent", messages=[{"role": "user", "content": "任务"}],
        system_prompt="系统", tools=[{"name": "Read"}], max_turns=2,
        tag=":test", execute_tool=execute_tool,
    )]

    assert events[-1] == {"type": "result", "final_text": "完成"}
    assert not any(event["type"] == "max_turns_exhausted" for event in events)
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_query_forces_tool_free_finalization_after_last_tool_turn():
    llm = FakeLLM([
        [
            LLMResponse(
                type="tool_use", tool_name="Write", tool_call_id="call-1",
                tool_input={"path": "chapter.md", "content": "正文"},
            ),
            LLMResponse(type="done"),
        ],
        [
            LLMResponse(type="text_delta", content="已写入章节，任务完成。"),
            LLMResponse(type="done"),
        ],
    ])
    executed = []

    async def execute_tool(tool_name, params, tool_call_id):
        executed.append((tool_name, params, tool_call_id))
        return True, "写入成功", ""

    events = [event async for event in query(
        llm, position="sub_agent", messages=[{"role": "user", "content": "写章节"}],
        system_prompt="系统", tools=[{"name": "Write"}], max_turns=1,
        tag=":test", execute_tool=execute_tool, sub_type="chapter_writer",
    )]

    exhausted = next(event for event in events if event["type"] == "max_turns_exhausted")
    assert exhausted["turns"] == 1
    assert executed == [("Write", {"path": "chapter.md", "content": "正文"}, "call-1")]
    assert llm.calls[1]["tools"] is None
    assert llm.calls[1]["tag"] == ":test:finalize"
    assert llm.calls[1]["messages"][-1] == {"role": "user", "content": FINALIZATION_PROMPT}
    assert events[-1] == {"type": "result", "final_text": "已写入章节，任务完成。"}
