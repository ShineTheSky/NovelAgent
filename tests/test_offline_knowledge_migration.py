import json
import sqlite3

import pytest

from novelagent.history.store import DocumentHistoryStore
from novelagent.memory.knowledge_store import KnowledgeStore
from novelagent.memory.offline_migration import offline_plan, offline_snapshot, source_manifest, verify_snapshot_runtime
from novelagent.memory.offline_history_repair import repair_snapshot_sources


def fixture_project(tmp_path):
    project = tmp_path / "source"
    history = DocumentHistoryStore(project)
    hid = history.start_request("r", "s", "保持自然衔接")
    history.close_request("r", "completed", "已完成")
    eid = hid + ":input"
    legacy = project / ".memory/project/old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(f"---\nid: old\nhistory_ids: [{hid}]\nhistory_event_ids: [{eid}]\n---\n旧摘要", encoding="utf-8")
    with sqlite3.connect(history.db_path) as db:
        event = dict(zip(["event_id", "history_id", "kind", "content", "revision_id"],
                         db.execute("SELECT event_id,history_id,kind,content,revision_id FROM history_events WHERE event_id=?", (eid,)).fetchone()))
    store = KnowledgeStore(project)
    memory = store.save_memory({"title": "衔接", "direction": "段落衔接", "background": "用户提出正文要求",
        "requirement": "保持自然衔接", "source_text": "保持自然衔接",
        "statements": [{"classifications": [{"domain": "writing", "categories": ["polishing"]}], "claim": "用户要求保持自然衔接", "source_event_ids": [eid]}]}, [event], [1., 0., 0.], "offline-vector-fixture")
    insight = store.publish_insights([{"payload": {"title": "衔接规则", "claim": "段落要自然衔接", "kind": "rule", "classifications": [{"domain": "writing", "categories": ["polishing"]}], "purpose": "连贯",
        "sources": [{"memory_id": memory["id"], "version": 1, "statement_ids": []}]}, "evidence": [event],
        "vector": [1., 0., 0.], "model_id": "offline-vector-fixture"}], {memory["id"]: 1})[0]
    return project, store, memory, insight, event


def test_offline_plan_never_writes_sources_or_relabels_legacy(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    before = source_manifest(project)
    report = offline_plan(project)
    assert source_manifest(project) == before
    assert report["counts"]["verified_published"] == 2
    assert report["legacy"][0]["status"] == "evidence_staged"
    assert report["legacy"][0]["payload"]["semantic_status"] == "unverified_legacy_summary"
    assert report["legacy"][0]["payload"]["learning_status"] == "not_started"


def test_snapshot_preserves_ids_versions_vectors_sources_and_holds_jobs(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    # Force a pending job to prove isolated snapshots cannot silently schedule model work.
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='pending'")
    before = source_manifest(project)
    destination = tmp_path / "isolated"
    report = offline_snapshot(project, destination)
    assert report["snapshot_verified"] and source_manifest(project) == before
    copied = KnowledgeStore(destination)
    assert copied.get(memory["id"])["payload"] == store.get(memory["id"])["payload"]
    assert copied.get(insight["id"])["payload"]["sources"] == insight["payload"]["sources"]
    assert copied.get(memory["id"])["vector"] == memory["vector"]
    with copied.connect() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "migration_hold"
    with sqlite3.connect(destination / "migration_evidence.sqlite3") as db:
        assert db.execute("SELECT status FROM bundles").fetchone()[0] == "evidence_staged"
    assert len(copied.list()) == 2  # No legacy summary was promoted by renaming.


def test_multiple_memory_versions_keep_insight_pinned_to_original_version(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    updated = store.save_memory(memory["payload"], [event], [1., 0., 0.], "offline-vector-fixture",
                                memory["id"], expected_version=1)
    offline_snapshot(project, tmp_path / "isolated")
    copied = KnowledgeStore(tmp_path / "isolated")
    assert copied.get(memory["id"])["version"] == updated["version"] == 2
    assert copied.get(memory["id"], 1)["payload"] == memory["payload"]
    assert copied.get(insight["id"])["payload"]["sources"][0]["version"] == 1


def test_snapshot_accepts_fragment_punctuation_without_changing_sources(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    history = DocumentHistoryStore(project)
    path = project / 'world/a.md'
    before, after = '【村庄】：“危险”……', '【村庄】：“安全”——有九叔。'
    history.record_revision(path, 'v1', None, before, before)
    history.record_revision(path, 'v2', 'v1', after, after)
    payload = dict(memory['payload'])
    payload.update(artifact_paths=['world/a.md'], fragments=[{
        'path': 'world/a.md', 'revision_id': 'v2', 'previous_revision_id': 'v1',
        'before': "[村庄]:'危险'...", 'after': "[村庄]:'安全'--有九叔.",
    }])
    updated = store.save_memory(payload, [event], [1., 0., 0.], 'offline-vector-fixture', memory['id'], expected_version=1)
    manifest = source_manifest(project)
    report = offline_snapshot(project, tmp_path / 'isolated')
    assert report['counts']['verified_published'] == 2
    assert source_manifest(project) == manifest
    assert KnowledgeStore(tmp_path / 'isolated').get(memory['id'])['payload'] == updated['payload']


def trace_fixture(project, event, tmp_path, *, trace_project=None, content=None):
    path = tmp_path / "trace.sqlite3"
    with sqlite3.connect(project / ".history/history.sqlite3") as db:
        db.execute("UPDATE history_units SET trace_ids_json='[\"trace-1\"]'")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, project_id TEXT)")
        db.execute("CREATE TABLE trace_events (event_id TEXT PRIMARY KEY, trace_id TEXT, event_type TEXT, payload_json TEXT)")
        db.execute("INSERT INTO traces VALUES ('trace-1',?)", (trace_project or project.name,))
        db.execute("INSERT INTO trace_events VALUES ('old-evt','trace-1','user_message',?)",
                   (json.dumps({"content": event["content"] if content is None else content}, ensure_ascii=False),))
    legacy = project / ".memory/project/old.md"
    legacy.write_text("---\nid: old\nsource_event_ids: [old-evt]\n---\n旧摘要", encoding="utf-8")
    return path


def test_exact_trace_id_and_content_restore_legacy_mapping_without_models(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    trace = trace_fixture(project, event, tmp_path)
    row = offline_plan(project, trace)["legacy"][0]
    assert row["status"] == "evidence_staged"
    assert row["exact_trace_history_mappings"] == ["old-evt"]
    assert row["evidence"][0]["event_id"] == event["event_id"]


def test_trace_from_other_project_is_not_accepted(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    trace = trace_fixture(project, event, tmp_path, trace_project="other-project")
    row = offline_plan(project, trace)["legacy"][0]
    assert row["status"] == "review_required" and row["trace_evidence"] == []


def test_original_trace_without_exact_history_content_requires_rebuild(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    trace = trace_fixture(project, event, tmp_path, content="另一个准确原文")
    row = offline_plan(project, trace)["legacy"][0]
    assert row["status"] == "trace_rebuild_required"
    assert row["trace_evidence"][0]["event_id"] == "old-evt"
    assert not row["exact_trace_history_mappings"]


def test_equal_content_on_different_trace_does_not_create_false_mapping(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    trace = trace_fixture(project, event, tmp_path)
    with sqlite3.connect(project / ".history/history.sqlite3") as db:
        db.execute("UPDATE history_units SET trace_ids_json='[\"unrelated-trace\"]'")
    assert offline_plan(project, trace)["legacy"][0]["status"] == "trace_rebuild_required"


def test_withdrawn_fact_cannot_support_active_insight(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    payload = dict(memory["payload"])
    payload["statements"][0]["status"] = "withdrawn"
    with store.connect() as db:
        db.execute("UPDATE versions SET payload=? WHERE id=?", (json.dumps(payload), memory["id"]))
    row = next(r for r in offline_plan(project)["structured"] if r["id"] == insight["id"])
    assert "withdrawn_fact_support" in row["issues"]


def test_payload_fact_change_with_unknown_previous_fact_is_quarantined(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    payload = dict(memory["payload"])
    payload["statement_changes"] = [{"kind": "corrected", "previous_statement_ids": ["unknown-previous"],
                                "current_statement_ids": [payload["statements"][0]["statement_id"]], "reason": "修正", "source_event_ids": [event["event_id"]]}]
    with store.connect() as db:
        db.execute("UPDATE versions SET payload=? WHERE id=?", (json.dumps(payload), memory["id"]))
    report = offline_snapshot(project, tmp_path / "isolated")
    row = next(r for r in report["structured"] if r["id"] == memory["id"])
    assert "unavailable_previous_fact_change" in row["issues"]
    assert not KnowledgeStore(tmp_path / "isolated").list()


def test_invalid_lineage_table_endpoint_cannot_bypass_payload_validation(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    with store.connect() as db:
        db.execute("INSERT INTO fact_changes VALUES (?,?,?,?,?,?,?,?)", (memory["id"], 0, 1,
            "unknown-previous", memory["payload"]["statements"][0]["statement_id"], "corrected", "修正", json.dumps([event["event_id"]])))
    row = next(r for r in offline_plan(project)["structured"] if r["id"] == memory["id"])
    assert "invalid_lineage_previous_endpoint" in row["issues"]


def test_artifact_parent_revision_must_be_the_actual_parent(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    history = DocumentHistoryStore(project)
    path = project / "chapters/content_1.1.1.md"
    history.record_revision(path, "rev-a", None, "相同正文", "相同正文")
    history.record_revision(path, "rev-b", "rev-a", "相同正文", "相同正文")
    history.record_revision(path, "rev-c", "rev-b", "修改正文", "修改正文")
    payload = dict(memory["payload"])
    payload["fragments"] = [{"path": "chapters/content_1.1.1.md", "revision_id": "rev-c", "previous_revision_id": "rev-a",
                             "before": "相同正文", "after": "修改正文"}]
    with store.connect() as db:
        db.execute("UPDATE versions SET payload=? WHERE id=?", (json.dumps(payload), memory["id"]))
    row = next(r for r in offline_plan(project)["structured"] if r["id"] == memory["id"])
    assert "artifact_parent_revision_mismatch" in row["issues"]


@pytest.mark.asyncio
async def test_snapshot_full_recall_context_use_and_pattern_path_without_models(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    before = source_manifest(project)
    destination = tmp_path / "isolated"
    offline_snapshot(project, destination)
    report = await verify_snapshot_runtime(destination)
    assert report["verified"] and report["insights_checked"] == 1
    assert report["context_attachments_checked"] == report["score_and_dedup_checked"] == report["pattern_default_checked"] == 1
    assert source_manifest(project) == before
    assert store.get(insight["id"])["layer"] == "insight"
    assert KnowledgeStore(destination).get(insight["id"])["layer"] == "pattern"


@pytest.mark.asyncio
async def test_runtime_verification_cannot_run_on_unmarked_production_project(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    with pytest.raises(ValueError, match="isolated migration snapshot"):
        await verify_snapshot_runtime(project)


def tool_repair_fixture(tmp_path, *, failed=False, mismatch=False, second_run=False):
    project, store, memory, insight, event = fixture_project(tmp_path)
    trace = tmp_path / "tools.sqlite3"
    with sqlite3.connect(trace) as db:
        db.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, project_id TEXT)")
        db.execute("""CREATE TABLE trace_events (event_id TEXT PRIMARY KEY, trace_id TEXT, event_type TEXT,
            payload_json TEXT, parent_event_id TEXT, sequence_no INTEGER, created_at TEXT)""")
        db.execute("INSERT INTO traces VALUES ('trace-1',?)", (project.name,))
        sources = [
            ("call-a", "subagent_tool_call", {"tool": "Edit", "run_id": "run-a", "params": {"path": "chapters/a.md", "old_string": "旧A", "new_string": "新A"}}),
            ("call-b", "subagent_tool_call", {"tool": "Edit", "run_id": "run-b" if second_run else "run-a", "params": {"path": "chapters/b.md", "old_string": "旧B", "new_string": "新B"}}),
            ("result-a", "subagent_tool_result", {"tool": "Edit", "run_id": "run-a", "success": not failed, "data": "文件: chapters/wrong.md\n准确原始结果" if mismatch else "文件: chapters/a.md\n准确原始结果A"}),
            ("result-b", "subagent_tool_result", {"tool": "Edit", "run_id": "run-b" if second_run else "run-a", "success": True, "data": "文件: chapters/b.md\n准确原始结果B"})]
        for number, (eid, kind, payload) in enumerate(sources, 1):
            db.execute("INSERT INTO trace_events VALUES (?,'trace-1',?,?,NULL,?,?)", (eid, kind, json.dumps(payload, ensure_ascii=False),
                number, "2026-10-04T12:00:0" + str(number)))
    with sqlite3.connect(project / ".history/history.sqlite3") as db:
        for label, run in (("a", "run-a"), ("b", "run-b" if second_run else "run-a")):
            db.execute("""INSERT INTO history_units
                (history_id,request_id,session_id,kind,path,status,trace_ids_json) VALUES (?,?,?,'document',?,'completed',?)""",
                ("doc-" + label, "trace-1:run:" + run, "s", "chapters/" + label + ".md", '["trace-1"]'))
    (project / ".memory/project/old.md").write_text("---\nid: old\nsource_event_ids: [call-a, result-a, call-b, result-b]\n---\n旧摘要", encoding="utf-8")
    return project, trace


def test_isolated_tool_pairs_keep_multiple_files_and_exact_order_separate(tmp_path):
    project, trace = tool_repair_fixture(tmp_path)
    before = source_manifest(project)
    destination = tmp_path / "isolated"
    report = offline_snapshot(project, destination, trace, repair_history=True)
    assert report["history_repair"]["sources_mapped"] == 4
    assert report["legacy"][0]["status"] == "evidence_staged"
    assert source_manifest(project) == before
    with sqlite3.connect(destination / ".history/history.sqlite3") as db:
        events = db.execute("""SELECT e.history_id,e.kind,e.content,o.event_order,o.occurred_at FROM history_events e
            JOIN history_event_order o ON o.event_id=e.event_id WHERE e.kind='file_change_attempt' ORDER BY o.event_order""").fetchall()
        assert [(r[0], json.loads(r[2])["params"]["path"], r[3]) for r in events] == [("doc-a", "chapters/a.md", 2000), ("doc-b", "chapters/b.md", 3000)]
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0  # No fabricated historical bodies.
    repeated = repair_snapshot_sources(destination, trace, project.name, offline_plan(project, trace)["legacy"])
    assert repeated["events_added"] == repeated["source_links_added"] == 0


@pytest.mark.parametrize("failed,mismatch,reason", [(True, False, "file_operation_failed"), (False, True, "tool_pair_result_path_not_verified")])
def test_unsuccessful_or_wrong_path_tool_pairs_do_not_gain_history_evidence(tmp_path, failed, mismatch, reason):
    project, trace = tool_repair_fixture(tmp_path, failed=failed, mismatch=mismatch)
    report = offline_snapshot(project, tmp_path / "isolated", trace, repair_history=True)
    assert report["history_repair"]["sources_mapped"] == 2
    assert report["legacy"][0]["status"] == "trace_rebuild_required"
    assert report["history_repair"]["unresolved"][0]["reason"] == reason
    with sqlite3.connect(tmp_path / "isolated/.history/history.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM history_source_events WHERE source_event_id='result-a'").fetchone()


def test_parallel_run_ids_cannot_mix_file_call_result_pairs(tmp_path):
    project, trace = tool_repair_fixture(tmp_path, second_run=True)
    report = offline_snapshot(project, tmp_path / "isolated", trace, repair_history=True)
    assert report["history_repair"]["sources_mapped"] == 4
    mappings = {m["source_event_id"]: m["history_id"] for m in report["history_repair"]["mappings"]}
    assert mappings == {"call-a": "doc-a", "result-a": "doc-a", "call-b": "doc-b", "result-b": "doc-b"}


def test_dispatch_return_maps_only_exact_review_run_and_special_review_owner(tmp_path):
    project, trace = tool_repair_fixture(tmp_path)
    with sqlite3.connect(trace) as db:
        for eid, kind, payload, parent, sequence in (
            ("dispatch", "tool_call", {"tool": "SubAgent", "params": {"preset": "reviewer"}}, None, 5),
            ("start", "subagent_subagent_start", {"preset": "reviewer", "run_id": "review-run"}, "dispatch", 6),
            ("return", "tool_result", {"tool": "SubAgent", "success": True, "data": "准确的审阅报告"}, "dispatch", 7)):
            db.execute("INSERT INTO trace_events VALUES (?,'trace-1',?,?,?,?,?)", (eid, kind, json.dumps(payload, ensure_ascii=False), parent, sequence, "2026-10-04T12:00:07"))
    with sqlite3.connect(project / ".history/history.sqlite3") as db:
        for hid, kind, run in (("review-good", "review", "review-run"), ("document-not-review", "document", "review-run"), ("other-review", "review", "other-run")):
            db.execute("""INSERT INTO history_units (history_id,request_id,session_id,kind,path,status,trace_ids_json)
                VALUES (?,?,?,?,'chapters/a.md','completed','["trace-1"]')""", (hid, "trace-1:run:" + run, "s", kind))
            db.execute("INSERT INTO history_events(event_id,history_id,kind,content) VALUES (?,?,'reviewer_output',?)", (hid + "-output", hid, "准确的审阅报告"))
    (project / ".memory/project/old.md").write_text("---\nid: old\nsource_event_ids: [return]\n---\n旧审阅摘要", encoding="utf-8")
    report = offline_snapshot(project, tmp_path / "isolated", trace, repair_history=True)
    assert report["legacy"][0]["status"] == "evidence_staged"
    assert report["history_repair"]["mappings"][0]["history_id"] == "review-good"
    assert report["history_repair"]["source_links_added"] == 1


def test_old_error_only_record_stays_badcase_and_is_not_repaired_into_history(tmp_path):
    project, trace = tool_repair_fixture(tmp_path)
    with sqlite3.connect(trace) as db:
        db.execute("INSERT INTO trace_events VALUES ('error','trace-1','error',?,NULL,5,'2026-10-04')", (json.dumps({"message": "失败"}),))
    (project / ".memory/project/old.md").write_text("---\nid: old\nsource_event_ids: [error]\n---\n旧错误记录", encoding="utf-8")
    report = offline_snapshot(project, tmp_path / "isolated", trace, repair_history=True)
    assert report["legacy"][0]["status"] == "badcase_only"
    assert report["history_repair"]["events_added"] == report["history_repair"]["sources_mapped"] == 0


def test_production_project_is_rejected_by_isolated_history_repair(tmp_path):
    project, trace = tool_repair_fixture(tmp_path)
    before = source_manifest(project)
    with pytest.raises(ValueError, match="isolated migration snapshot"):
        repair_snapshot_sources(project, trace, project.name, offline_plan(project, trace)["legacy"])
    assert source_manifest(project) == before


def test_missing_evidence_quarantines_memory_and_its_insight(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    with sqlite3.connect(project / ".history/history.sqlite3") as db:
        db.execute("UPDATE history_events SET content='不同原文' WHERE event_id=?", (event["event_id"],))
    report = offline_snapshot(project, tmp_path / "isolated")
    assert report["counts"]["review_published"] == 2
    assert KnowledgeStore(tmp_path / "isolated").list() == []
    assert all(r["status"] == "migration_review" for r in KnowledgeStore(tmp_path / "isolated").list(include_inactive=True))


def test_legacy_excerpt_mismatch_is_not_verified(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    path = project / ".memory/project/mismatch.md"
    path.write_text("---\nid: mismatched\nsemantic_evidence:\n- history_id: " + event["history_id"] +
                    "\n  history_event_ids: [" + event["event_id"] + "]\n  history_excerpts:\n  - event_id: " + event["event_id"] +
                    "\n    content: 从未出现的内容\n---\n旧摘要", encoding="utf-8")
    row = next(r for r in offline_plan(project)["legacy"] if r["legacy_id"] == "mismatched")
    assert row["status"] == "review_required" and row["mismatched_excerpt_ids"] == [event["event_id"]]


def test_missing_sources_and_archived_duplicates_are_not_silently_activated(tmp_path):
    project, store, memory, insight, event = fixture_project(tmp_path)
    archive = project / ".memory/archive/duplicate.md"
    archive.parent.mkdir()
    archive.write_text("---\nid: old\n---\n备份", encoding="utf-8")
    rules = project / ".memory/project_rules.md"
    rules.write_text("## [rule] 规则\n<!-- memory-entry\nid: rule-old\n-->\n没有事件依据", encoding="utf-8")
    report = offline_plan(project)
    assert len(report["legacy"]) == 2
    assert next(r for r in report["legacy"] if r["legacy_id"] == "rule-old")["status"] == "review_required"


@pytest.mark.parametrize("target", ["source", "source/sub", ".", "occupied"])
def test_snapshot_rejects_source_overwrite_or_nonempty_destination(tmp_path, target):
    project, store, memory, insight, event = fixture_project(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "user.txt").write_text("keep", encoding="utf-8")
    before = source_manifest(project)
    with pytest.raises(ValueError, match="Destination"):
        offline_snapshot(project, tmp_path / target)
    assert source_manifest(project) == before
    assert (occupied / "user.txt").read_text(encoding="utf-8") == "keep"
