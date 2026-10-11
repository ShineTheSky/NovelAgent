"""Billing exclusion and recoverable cleanup; synthetic SQLite only."""

import json
import sqlite3
import zipfile

import pytest

from scripts.purge_balance_bad_cases import cleanup
from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer


def fixture_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "novelagent.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE traces(trace_id TEXT PRIMARY KEY,operation_kind TEXT,source_agent TEXT,status TEXT,
                                previous_trace_id TEXT,next_trace_id TEXT);
            CREATE TABLE trace_events(event_id TEXT PRIMARY KEY,trace_id TEXT REFERENCES traces(trace_id),event_type TEXT,payload_json TEXT);
            CREATE TABLE bad_case_trace_links(bad_case_trace_id TEXT PRIMARY KEY REFERENCES traces(trace_id),resolved_trace_id TEXT REFERENCES traces(trace_id));
            CREATE TABLE session_trace_turns(source_trace_id TEXT);
        """)
        conn.executemany("INSERT INTO traces VALUES (?,?,?,?,NULL,NULL)", [
            ("balance-case", "agent_bad_case", "bad_case_analyzer", "failed"),
            ("analysis", "agent_run", "bad_case_analyzer", "failed"),
            ("normal-case", "agent_bad_case", "memory:retain", "failed"),
            ("original-task", "agent_run", "knowledge:retain", "failed"),
        ])
        for event, trace, kind, payload in [
            ("balance-meta", "balance-case", "bad_case_context", {"error": "HTTP 402: Insufficient Balance", "source_trace_id": "analysis"}),
            ("normal-meta", "normal-case", "bad_case_context", {"error": "HTTP 500", "source_trace_id": "original-task"}),
            ("analysis-request", "analysis", "agent_request_message", {"content": "nested request" * 100}),
            ("analysis-finish", "analysis", "agent_finished", {"error": "HTTP 402: Insufficient Balance"}),
            ("original-error", "original-task", "error", {"message": "HTTP 402: Insufficient Balance"}),
        ]:
            conn.execute("INSERT INTO trace_events VALUES (?,?,?,?)", (event, trace, kind, json.dumps(payload)))
        conn.execute("INSERT INTO bad_case_trace_links VALUES ('balance-case','analysis')")
    cases = data / "agent_bad_cases"
    cases.mkdir()
    (cases / "balance.md").write_text("---\nid: balance-case\n---\n## 错误\nHTTP 402: Insufficient Balance", encoding="utf-8")
    (cases / "normal.md").write_text("---\nid: normal-case\n---\n## 错误\nHTTP 500", encoding="utf-8")
    (cases / "analysis").mkdir()
    (cases / "analysis/stale.md").write_text("---\nsource_case_ids: [balance-case]\n---\nold diagnosis", encoding="utf-8")
    return db, cases


def test_preview_does_not_mutate_or_archive(tmp_path):
    db, cases = fixture_db(tmp_path)
    before = db.read_bytes()
    result = cleanup(db)
    assert result["balance_cases"] == result["analysis_runs"] == 1
    assert result["files"] == 2
    assert db.read_bytes() == before
    assert (cases / "balance.md").exists() and not (db.parent / "cleanup_archives").exists()


def test_cleanup_archives_only_billing_cases_and_analysis_runs(tmp_path):
    db, cases = fixture_db(tmp_path)
    normal = (cases / "normal.md").read_bytes()
    result = cleanup(db, apply=True, vacuum=True)
    with sqlite3.connect(db) as conn:
        assert {r[0] for r in conn.execute("SELECT trace_id FROM traces")} == {"original-task", "normal-case"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM trace_events").fetchone()[0] == 2
    assert not (cases / "balance.md").exists() and not (cases / "analysis/stale.md").exists()
    assert (cases / "normal.md").read_bytes() == normal
    with zipfile.ZipFile(result["archive"]) as backup:
        assert backup.testzip() is None
        rows = [json.loads(line) for line in backup.read("database.jsonl").decode().splitlines()]
        assert {r["row"]["trace_id"] for r in rows if r["table"] == "traces"} == {"balance-case", "analysis"}
        assert "files/balance.md" in backup.namelist() and "files/analysis/stale.md" in backup.namelist()
    assert cleanup(db)["balance_cases"] == 0
    # Reinsert the archived delta into the surviving database to prove recoverability.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for entry in sorted(rows, key=lambda item: item["table"] != "traces"):
            record = entry["row"]
            columns = ','.join(record)
            placeholders = ','.join('?' for _ in record)
            conn.execute(f"INSERT INTO {entry['table']} ({columns}) VALUES ({placeholders})", list(record.values()))
        assert conn.execute("SELECT count(*) FROM traces").fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_evidence_reference_preserves_source_analysis_run(tmp_path):
    db, _ = fixture_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO session_trace_turns VALUES ('analysis')")
    result = cleanup(db, apply=True)
    assert result["analysis_runs"] == 0 and result["protected_ids"] == ["analysis"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT 1 FROM traces WHERE trace_id='analysis'").fetchone()


def test_surviving_badcase_link_preserves_analysis_run(tmp_path):
    db, _ = fixture_db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO bad_case_trace_links VALUES ('normal-case','analysis')")
    result = cleanup(db, apply=True)
    assert result["analysis_runs"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT resolved_trace_id FROM bad_case_trace_links WHERE bad_case_trace_id='normal-case'").fetchone() == ('analysis',)


def test_failed_archive_does_not_delete_database_or_files(tmp_path, monkeypatch):
    db, cases = fixture_db(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("archive unavailable")

    monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    with pytest.raises(OSError, match="archive unavailable"):
        cleanup(db, apply=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM traces").fetchone()[0] == 4
    assert (cases / "balance.md").exists()


def test_analyzer_excludes_legacy_balance_files_but_keeps_other_api_errors(tmp_path):
    analyzer = BadCaseAnalyzer(None, str(tmp_path / "workspace"))
    analyzer.directory.mkdir(parents=True)
    for name, error in (("balance", "HTTP 402: Insufficient Balance"), ("server", "HTTP 500"), ("rate", "HTTP 429")):
        (analyzer.directory / (name + ".md")).write_text(
            f"---\nid: {name}\nclassification: llm_api\nfailure_kind: llm_call_failure\n---\n## 错误\n{error}", encoding="utf-8")
    assert {item["id"] for item in analyzer._load_cases("llm_api")} == {"server", "rate"}
