import asyncio

from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
from novelagent.trace.agent_run import AgentRunTrace, AgentTraceTaskManager
from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer
from novelagent.core.session import Session
from novelagent.core.subagent import SubAgentRunner
from novelagent.tools.registry import ToolRegistry


class _Store:
    def __init__(self):
        self.traces = {}
        self.events = {}

    async def create_trace(self, session_id, project_id, title, source_agent="", agent_position=""):
        trace_id = f"trace-{len(self.traces) + 1}"
        self.traces[trace_id] = {
            "session_id": session_id, "project_id": project_id, "title": title,
            "source_agent": source_agent, "agent_position": agent_position,
        }
        self.events[trace_id] = []
        return trace_id

    async def set_trace_operation(self, trace_id, operation_kind, analysis_status):
        self.traces[trace_id].update({
            "operation_kind": operation_kind, "analysis_status": analysis_status,
        })

    async def append_event(self, trace_id, sequence_no, event_type, actor, payload=None,
                           parent_event_id=None, duration_ms=None):
        self.events[trace_id].append({
            "sequence_no": sequence_no, "event_type": event_type,
            "actor": actor, "payload": payload or {},
        })
        return f"event-{trace_id}-{sequence_no}"

    async def finish_trace(self, trace_id, status, final_answer="", token_count=0):
        self.traces[trace_id].update({"status": status, "final_answer": final_answer})

    async def list_events(self, trace_id, limit=1_000, offset=0):
        events = self.events.get(trace_id, [])[offset:offset + limit]
        return [
            {"event_id": f"event-{trace_id}-{event['sequence_no']}", "trace_id": trace_id, **event}
            for event in events
        ]


def test_agent_run_trace_keeps_long_request_reconstructable():
    async def run():
        store = _Store()
        trace = await AgentRunTrace.start(
            store, session_id="session-a", project_id="project-a",
            actor="memory_analyzer", position="memory_summary_fallback", source_trace_id="trace-parent",
        )
        content = "x" * 25_000
        trace.add_request(
            position="memory_summary_fallback", tag=":trace-fork",
            messages=[{"role": "user", "content": content}], tools=[],
        )
        trace.add("tool_call", {"tool": "GetTraceContext", "params": {"trace_ids": ["trace-parent"]}})
        await trace.finish("failed", error="forced")

        events = store.events[trace.trace_id]
        chunks = [event["payload"]["content"] for event in events
                  if event["event_type"] == "agent_request_message"]
        assert "".join(chunks) == content
        assert any(event["event_type"] == "tool_call" for event in events)
        assert events[0]["payload"]["source_trace_id"] == "trace-parent"

    asyncio.run(run())


def test_agent_run_trace_merges_stream_fragments_before_persisting():
    async def run():
        store = _Store()
        trace = await AgentRunTrace.start(
            store, session_id="session-a", project_id="project-a",
            actor="subagent:outliner", position="sub_agent",
        )
        for fragment in ("现", "在", "写", "回执"):
            trace.add("thinking", {"content": fragment})
        await trace.finish("completed", "done")

        thinking = [event for event in store.events[trace.trace_id]
                    if event["event_type"] == "thinking"]
        assert len(thinking) == 1
        assert thinking[0]["payload"]["content"] == "现在写回执"
        assert thinking[0]["payload"]["stream_chunk_count"] == 4

    asyncio.run(run())


def test_agent_trace_task_manager_does_not_block_submit_and_drains():
    async def run():
        manager = AgentTraceTaskManager()
        release = asyncio.Event()
        completed = False

        async def persist():
            nonlocal completed
            await release.wait()
            completed = True

        manager.submit(persist(), label="trace:test")
        await asyncio.sleep(0)
        assert manager.pending_count == 1
        assert not completed

        release.set()
        await manager.drain()
        assert completed
        assert manager.pending_count == 0

    asyncio.run(run())


def test_subagent_done_does_not_wait_for_success_trace_persistence(monkeypatch):
    async def run():
        store = _Store()
        release = asyncio.Event()
        original_finish_trace = store.finish_trace

        async def delayed_finish_trace(*args, **kwargs):
            await release.wait()
            await original_finish_trace(*args, **kwargs)

        store.finish_trace = delayed_finish_trace

        async def fake_query(*_args, **_kwargs):
            yield {"type": "thinking", "content": "完成"}
            yield {"type": "result", "final_text": "回执"}

        monkeypatch.setattr("novelagent.core.subagent.query", fake_query)
        manager = AgentTraceTaskManager()
        bad_cases = type("BadCases", (), {"store": store})()
        runner = SubAgentRunner(
            None, ToolRegistry(), None, None, bad_case_recorder=bad_cases,
            trace_tasks=manager,
        )
        runner.presets_tool.get_preset = lambda _name: {
            "system_prompt": "", "tools": [], "inherit_history": False, "max_turns": 1,
        }
        stream = runner.spawn_and_run(
            "outliner", "write receipt", Session("session-a", "project-a"),
        )

        assert (await anext(stream)).type == "subagent_start"
        assert (await anext(stream)).type == "thinking"
        done = await anext(stream)
        assert done.type == "subagent_done"
        assert done.data["result"] == "回执"

        await asyncio.sleep(0)
        assert manager.pending_count == 1
        assert not any(trace.get("status") == "completed" for trace in store.traces.values())

        release.set()
        await manager.drain()
        assert any(trace.get("status") == "completed" for trace in store.traces.values())
        await stream.aclose()

    asyncio.run(run())


def test_bad_case_stays_lightweight_and_only_links_agent_trace(tmp_path):
    async def run():
        store = _Store()
        source = await AgentRunTrace.start(
            store, session_id="session-a", project_id="project-a",
            actor="subagent:reviewer", position="sub_agent",
        )
        source.add("thinking", {"content": "diagnostic reasoning"})
        await source.finish("failed", error="boom")

        recorder = AgentBadCaseRecorder(store, str(tmp_path / "workspace"))
        bad_case_id = await recorder.capture(
            source_trace_id=source.trace_id, session_id="session-a", project_id="project-a",
            failure_kind="subagent_failure", actor="subagent:reviewer", error="boom",
            messages=[{"role": "user", "content": str(index)} for index in range(12)],
        )

        bad_events = store.events[bad_case_id]
        assert [event["event_type"] for event in bad_events] == [
            "bad_case_context", "error", "context_snapshot",
        ]
        assert bad_events[0]["payload"]["source_trace_id"] == source.trace_id
        assert len(bad_events[2]["payload"]["messages"]) == 8
        assert not any(event["event_type"] == "thinking" for event in bad_events)
        assert any(event["event_type"] == "thinking" for event in store.events[source.trace_id])

    asyncio.run(run())


def test_bad_case_analyzer_reads_linked_trace_programmatically(tmp_path):
    async def run():
        store = _Store()
        source = await AgentRunTrace.start(
            store, session_id="session-a", project_id="project-a",
            actor="memory_analyzer", position="memory_summary_fallback",
        )
        source.add("tool_call", {"tool": "Read", "params": {"path": "outline.md"}})
        source.add("error", {"message": "tool call is not allowed on cached branch"})
        await source.finish("failed")

        analyzer = BadCaseAnalyzer(None, str(tmp_path / "workspace"), trace_store=store)
        context = await analyzer._load_trace_context(source.trace_id)
        assert any(item.get("tool") == "Read" for item in context)
        assert any(item.get("message") == "tool call is not allowed on cached branch" for item in context)

    asyncio.run(run())
