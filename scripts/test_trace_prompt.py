"""Validate Evidence/Memory classification without writing Trace data."""

import asyncio
import json

from novelagent.llm.client import LLMClient


PROMPT = '''只输出 JSON：{"records":[{"layer":"evidence|memory","claim":"","source_event_ids":[""],"signal":"weak|strong"}]}。
Evidence：单次反馈或异常质疑，例如“为什么调用 AskUserQuestion 工具”。
Memory：明确、长期有效的偏好、修正或约束，例如“把林深的初始性格改为恐惧回避型”。
events:
- {"event_id":"evt_feedback","content":"为什么调用 AskUserQuestion 工具？"}
- {"event_id":"evt_correction","content":"把林深的初始性格改为恐惧回避型，以后大纲都遵循这个设定。"}
'''


async def main() -> None:
    client = LLMClient("config/llm_config.yaml")
    text = ""
    async for chunk in client.chat(
        position="main_loop", messages=[{"role": "user", "content": PROMPT}],
        tools=None, stream=False, tag=":trace-classification-test", max_tokens=512,
    ):
        if chunk.type == "text_delta":
            text += chunk.content
    print(json.dumps(json.loads(text[text.find("{"):text.rfind("}") + 1]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
