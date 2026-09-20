import asyncio

from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
from novelagent.trace.agent_run import AgentRunTrace
from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer


class _Store:
    def __init__(self):
        self.traces = {}
        self.events = {}

    async def create_trace(self, session_id, project_id, title):
        trace_id = f"trace-{len(self.traces) + 1}"
        self.traces[trace_id] = {
            "session_id": session_id, "project_id": project_id, "title": title,
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
            actor="memory_analyzer", position="auto_memory", source_trace_id="trace-parent",
        )
        content = "x" * 25_000
        trace.add_request(
            position="auto_memory", tag=":trace-fork",
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
            actor="memory_analyzer", position="auto_memory",
        )
        source.add("tool_call", {"tool": "Read", "params": {"path": "outline.md"}})
        source.add("error", {"message": "tool call is not allowed on cached branch"})
        await source.finish("failed")

        analyzer = BadCaseAnalyzer(None, str(tmp_path / "workspace"), trace_store=store)
        context = await analyzer._load_trace_context(source.trace_id)
        assert any(item.get("tool") == "Read" for item in context)
        assert any(item.get("message") == "tool call is not allowed on cached branch" for item in context)

    asyncio.run(run())
