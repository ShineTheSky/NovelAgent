"""SSE流式管道"""

import json
import asyncio
from novelagent.core.session import ResponseChunk


def chunk_to_sse(chunk: ResponseChunk) -> str:
    """将ResponseChunk转为SSE格式文本"""
    return chunk.to_sse()


async def sse_stream(chunks, stop_event: asyncio.Event = None):
    """将异步chunk迭代器转为SSE流"""
    async for chunk in chunks:
        if stop_event and stop_event.is_set():
            yield chunk_to_sse(ResponseChunk(type="done", data={"finish_reason": "interrupted"}))
            return
        yield chunk_to_sse(chunk)
