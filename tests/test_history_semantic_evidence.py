import asyncio

from novelagent.history import DocumentHistoryStore
from novelagent.trace.file_analyzer import FileTraceAnalyzer
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.versioning import revision_manager
from novelagent.tools.get_history_revision import GetHistoryRevisionTool
from novelagent.tools.base import ToolContext, PermissionResult


class _TraceStore:
    async def save_trace_classification(self, *args):
        pass

    async def set_trace_analysis_status(self, *args):
        pass


def test_document_memory_keeps_precise_history_evidence(tmp_path):
    project = tmp_path / "project-a"
    path = project / "chapters" / "content_1.1.1.md"
    history = DocumentHistoryStore(project)
    revision = asyncio.run(revision_manager.commit(
        path, "林深只是握紧杯子。", "new", actor="writer", operation_id="op-1",
        history_store=history, history_evidence={
            "user_input": "写得克制一点", "main_delegation": "以动作表现压力",
            "writer_thinking": "减少解释句。",
        },
    ))
    events = [{
        "event_id": "feedback", "trace_id": "trace-1", "sequence_no": 1,
        "event_type": "user_message", "payload": {
            "turn": 1, "content": "这段还太直白", "artifact_path": "chapters/content_1.1.1.md",
        },
    }]
    data = {"items": [], "records": [{
        "layer": "memory", "category": "reference", "domain": "writing",
        "title": "克制地表现压力", "claim": "克制地表现压力",
        "content": "用动作而不是解释表现压力。", "source_event_ids": ["feedback"],
        "source_history_revision_ids": [revision.revision_id, "invented"],
        "signal": "strong", "artifact_path": "chapters/content_1.1.1.md",
        "semantic": {"what": "用户要求用动作表现压力", "assertion_status": "confirmed",
                     "why": "直接解释过于直白", "attributes": {"target": "这段正文"}},
    }]}
    analyzer = FileTraceAnalyzer(None, str(tmp_path), _TraceStore())
    asyncio.run(analyzer._apply_prepared_data(
        data, events, ["trace-1"], "trace-1", "project-a",
        allowed_source_event_ids=["feedback"],
    ))
    records = FileLifecycleStore(str(tmp_path), "project-a").list("memory")
    assert len(records) == 1
    saved = records[0]
    assert saved["history_revision_ids"] == [revision.revision_id]
    evidence = saved["semantic_evidence"][0]
    assert evidence["source_kind"] == "user"
    assert evidence["history_revision_ids"] == [revision.revision_id]
    assert any(row["kind"] == "main_delegation" for row in evidence["history_excerpts"])

    context = ToolContext(session_id="session", project_id="project-a",
                          working_dir=str(project), actor="trace_analyzer")
    tool = GetHistoryRevisionTool()
    assert tool.checkPermissions({"revision_id": revision.revision_id}, context) == PermissionResult.ALLOW
    recovered = asyncio.run(tool.execute({"revision_id": revision.revision_id}, context))
    assert recovered.success
    assert recovered.data["body_md"] == "林深只是握紧杯子。"
    assert recovered.data["evidence"][0]["content"] == "写得克制一点"
