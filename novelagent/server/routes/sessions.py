"""会话API路由"""

import json
import time
import asyncio
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from novelagent.core.session import InternalRequest, Session
from novelagent.server.sse import sse_stream
from novelagent.storage.database import get_connection

router = APIRouter(prefix="/api")


class CreateSessionRequest(BaseModel):
    pass


class SendMessageRequest(BaseModel):
    content: str
    action: str = "send"
    accept_edits: bool = False


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """删除会话"""
    conn = await get_connection()
    await conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    await conn.commit()
    await conn.close()
    if session_id in _sessions:
        del _sessions[session_id]
    return {"status": "deleted"}


@router.post("/sessions/{session_id}/compress")
async def force_compress(session_id: str, request: Request):
    """立即执行上下文压缩，保留最近消息 + 摘要，重整索引"""
    import sys
    from novelagent.context.token_counter import TokenCounter

    session = await _get_or_load_session(session_id, request)
    msg_dicts = session.messages
    if not msg_dicts:
        return {"status": "ok", "token_count": 0, "original_count": 0, "message": "无消息可压缩"}

    token_counter = TokenCounter()
    original_count = token_counter.count_messages(msg_dicts)
    token_limit = request.app.state.config.get("session", {}).get("token_limit", 64000)

    # 压缩: 保留最近6条纯对话消息（剔除工具调用，防止 tool_call_id 断裂）
    keep_n = 6
    pure_msgs = [m for m in msg_dicts if m.get("role") in ("user", "assistant") and not m.get("tool_calls")]
    recent = pure_msgs[-keep_n:] if len(pure_msgs) > keep_n else pure_msgs
    older = pure_msgs[:-keep_n] if len(pure_msgs) > keep_n else []

    if older:
        try:
            llm = request.app.state.agent_loop.llm
            history_text = "\n".join(f"[{m.get('role','')}]: {(m.get('content','') or '')[:500]}" for m in older[-50:])
            prompt = f"请将以下对话历史压缩为一段简洁摘要（300字以内），保留关键决策、人物变更、用户偏好：\n\n{history_text[-8000:]}"
            summary = ""
            async for chunk in llm.chat(
                position="context_compression", messages=[{"role": "user", "content": prompt}],
                tools=None, stream=False, tag=":compress",
            ):
                if chunk.type == "text_delta": summary += chunk.content
            summary_text = summary.strip()[:500] or "之前的对话已压缩。"
        except Exception:
            summary_text = "之前的对话已压缩。"
    else:
        summary_text = "对话开始"

    compressed = [{"role": "user", "content": f"[上下文压缩] {summary_text}"}] + recent

    new_count = token_counter.count_messages(compressed)

    # 更新会话缓存 + 持久化
    session.messages = compressed
    session.token_count = new_count
    from novelagent.storage import models
    await models.save_messages(session_id, compressed, new_count, llm_client=request.app.state.agent_loop.llm)

    print(f"[compress] session={session_id[:8]}... {original_count} → {new_count} tokens", flush=True)
    return {
        "status": "ok",
        "token_count": new_count,
        "original_count": original_count,
        "message": f"压缩完成: {original_count} → {new_count} tokens",
    }


class ToggleAcceptEditsRequest(BaseModel):
    enabled: bool


class PermissionResponseRequest(BaseModel):
    allow: bool


class QuestionResponseRequest(BaseModel):
    answers: list


# In-memory session cache (backed by SQLite)
_sessions: dict[str, Session] = {}
_active_locks: dict[str, bool] = {}  # 防止同会话并发请求


async def _get_or_load_session(session_id: str, request: Request) -> Session:
    if session_id in _sessions:
        s = _sessions[session_id]
        print(f"[session] 缓存命中: {session_id[:8]}... messages={len(s.messages)}", flush=True)
        return s
    from novelagent.storage import models
    db_session = await models.load_session(session_id)
    if db_session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = json.loads(db_session.messages_json) if db_session.messages_json else []
    print(f"[session] 从DB加载: {session_id[:8]}... messages={len(msgs)}", flush=True)
    s = Session(
        session_id=db_session.session_id,
        project_id=db_session.project_id,
        title=db_session.title,
        messages=msgs,
        token_count=db_session.token_count,
    )
    _sessions[session_id] = s
    return s


@router.post("/projects/{project_id}/sessions")
async def create_session(project_id: str, request: Request):
    """创建新会话"""
    from novelagent.storage import models
    db_session = await models.create_session(project_id)
    session = Session(session_id=db_session.session_id, project_id=project_id)
    _sessions[session.session_id] = session
    return {"session_id": session.session_id, "project_id": project_id}


@router.get("/projects/{project_id}/sessions")
async def list_sessions(project_id: str, request: Request):
    """获取项目下的会话列表"""
    from novelagent.storage import models
    sessions = await models.list_sessions(project_id)
    return [{"session_id": s.session_id, "title": s.title or "新会话",
             "created_at": s.created_at, "updated_at": s.updated_at} for s in sessions]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    """获取会话详情"""
    session = await _get_or_load_session(session_id, request)
    return {
        "session_id": session.session_id,
        "project_id": session.project_id,
        "messages": session.messages,
        "accept_edits_mode": session.accept_edits_mode,
        "token_count": getattr(session, 'token_count', 0),
    }


@router.post("/sessions/{session_id}/message")
async def send_message(session_id: str, body: SendMessageRequest, request: Request):
    """发送消息 → SSE流式响应"""
    import sys
    print(f"[API] 收到消息: session={session_id}, content={body.content[:50]}...", flush=True)

    # 防止同一会话并发请求（带超时自动释放，防止死锁）
    lock_ts = _active_locks.get(session_id, 0)
    if lock_ts and (time.time() - lock_ts) < 300:  # 5分钟内未释放视为活跃
        raise HTTPException(status_code=409, detail="会话正在处理中，请等待当前回复完成")
    _active_locks[session_id] = time.time()

    session = await _get_or_load_session(session_id, request)
    agent_loop = request.app.state.agent_loop

    internal_req = InternalRequest(
        session_id=session_id,
        project_id=session.project_id,
        content=body.content,
        action=body.action,
        accept_edits=body.accept_edits,
    )

    # Load project info
    working_dir = Path(request.app.state.agent_loop.working_dir)
    project_dir = working_dir / session.project_id
    project_info = {}
    project_yaml = project_dir / "project.yaml"
    if project_yaml.exists():
        import yaml
        with open(project_yaml, encoding="utf-8") as f:
            project_info = yaml.safe_load(f) or {}

    stop_event = asyncio.Event()

    async def event_generator():
        import traceback
        try:
            async for chunk in agent_loop.run(internal_req, session, project_info):
                if stop_event.is_set():
                    yield f"event: done\ndata: {json.dumps({'finish_reason': 'interrupted'})}\n\n"
                    break
                yield chunk.to_sse()
        except Exception as e:
            traceback.print_exc()
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'message': f'服务器内部错误: {e}', 'timestamp': ''})}\n\n"
        finally:
            active_trace_id = getattr(session, "active_trace_id", "")
            if active_trace_id and agent_loop.trace:
                await agent_loop.trace.finish(active_trace_id, "interrupted")
                session.active_trace_id = ""
            # 先释放会话锁，防止save_messages（含LLM标题生成）阻塞后续请求
            _active_locks[session_id] = 0
            if session.messages:
                from novelagent.storage import models
                try:
                    await models.save_messages(session_id, session.messages, session.token_count, llm_client=request.app.state.agent_loop.llm)
                    print(f"[API] 消息已保存: session={session_id}, messages={len(session.messages)}", flush=True)
                except Exception as _e:
                    print(f"[API] 保存消息失败: {_e}", flush=True)

    try:
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    except Exception:
        _active_locks[session_id] = 0
        raise


@router.post("/sessions/{session_id}/stop")
async def stop_generation(session_id: str, request: Request):
    """中断生成"""
    session = await _get_or_load_session(session_id, request)
    session.stop_requested = True
    return {"status": "stopped"}


@router.post("/sessions/{session_id}/permission-response")
async def permission_response(session_id: str, body: PermissionResponseRequest, request: Request):
    """用户对权限请求的响应"""
    session = await _get_or_load_session(session_id, request)
    session.permission_granted = body.allow
    if hasattr(session, 'permission_event') and session.permission_event:
        session.permission_event.set()
    return {"status": "ok", "allowed": body.allow}


@router.post("/sessions/{session_id}/question-response")
async def question_response(session_id: str, body: QuestionResponseRequest, request: Request):
    """用户对AskUserQuestion的响应"""
    session = await _get_or_load_session(session_id, request)
    session.question_answers = body.answers
    if hasattr(session, 'question_event') and session.question_event:
        session.question_event.set()
    return {"status": "ok"}


@router.post("/sessions/{session_id}/accept-edits")
async def toggle_accept_edits(session_id: str, body: ToggleAcceptEditsRequest, request: Request):
    """切换acceptEdits模式"""
    session = await _get_or_load_session(session_id, request)
    session.accept_edits_mode = body.enabled
    return {"session_id": session_id, "accept_edits_mode": session.accept_edits_mode}
