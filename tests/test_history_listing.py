from novelagent.history import DocumentHistoryStore


def test_project_history_list_is_paginated_and_omits_full_revision_body(tmp_path):
    project = tmp_path / "project-a"
    project.mkdir()
    store = DocumentHistoryStore(project)
    assert store.list_units() == {"items": [], "total": 0}
    assert not store.db_path.exists()

    store.start_request("trace-chat", "session-a", "讨论人物动机")
    store.close_request("trace-chat", "completed", "好的", ["trace-chat"], 1)
    long_request = "修改第一节" + "细" * 300
    store.start_request("trace-file", "session-a", long_request)
    store.record_revision(project / "chapters" / "content_1.1.1.md", "rev-1", None,
                          "完整正文", "完整正文", evidence={"history_request_id": "trace-file"})
    store.close_request("trace-file", "completed", "已修改", ["trace-file"], 2)

    first = store.list_units(limit=1)
    second = store.list_units(limit=1, offset=1)
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["kind"] == "document"
    assert first["items"][0]["user_input"] == long_request[:240]
    assert "body_md" not in first["items"][0]
    assert second["items"][0]["kind"] == "conversation"
    assert store.list_units(kind="document")["total"] == 1
    assert store.list_units(kind="conversation")["total"] == 1
    assert store.list_files()["items"] == [{
        "path": "chapters/content_1.1.1.md", "history_count": 1,
        "updated_at": first["items"][0]["created_at"],
        "latest_history_id": first["items"][0]["history_id"],
    }]
    assert store.list_units(kind="document", path="chapters/content_1.1.1.md")["total"] == 1
    assert store.list_units(kind="document", path="chapters/other.md")["total"] == 0
    assert store.get_unit(first["items"][0]["history_id"])["revisions"][0]["body_md"] == "完整正文"

    other = tmp_path / "project-b"
    other.mkdir()
    assert DocumentHistoryStore(other).list_units()["items"] == []


def test_file_history_groups_multiple_runs_and_exposes_previous_id(tmp_path):
    project = tmp_path / "project-a"
    store = DocumentHistoryStore(project)
    path = "chapters/content_1.1.2.md"
    for request_id, revision_id, parent_id in (
        ("writer", "rev-1", None), ("polisher", "rev-2", "rev-1"),
    ):
        store.start_request(request_id, "session-a", request_id)
        store.append_request_event(request_id, "main_delegation", f"{request_id} {path}")
        store.record_revision(project / path, revision_id, parent_id,
                              revision_id, revision_id,
                              previous_body=parent_id or "",
                              previous_rendered=parent_id or "",
                              evidence={"history_request_id": request_id})
        store.close_request(request_id, "completed", "已修改")
    files = store.list_files()
    assert files["total"] == 1
    assert files["items"][0]["history_count"] == 2
    entries = store.list_units(kind="document", path=path)
    assert entries["total"] == 2
    assert entries["items"][0]["task_summary"] == f"polisher {path}"
    assert entries["items"][0]["previous_history_id"] == entries["items"][1]["history_id"]
