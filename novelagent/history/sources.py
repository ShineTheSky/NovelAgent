"""Root entry points and independent review nodes, separate from exact evidence."""

from contextlib import closing
from pathlib import Path
import sqlite3


def root_history_ids(project: Path, history_ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(history_ids))
    db_path = Path(project) / ".history/history.sqlite3"
    if not ids or not db_path.exists():
        return ids
    with closing(sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)) as db:
        if "requirement_history_id" not in {row[1] for row in db.execute("PRAGMA table_info(history_units)")}:
            return ids
        roots = dict(db.execute(f"""SELECT h.history_id,
            CASE WHEN h.kind = 'document' AND r.kind = 'requirement'
                 THEN r.history_id ELSE h.history_id END
            FROM history_units h LEFT JOIN history_units r
                ON r.history_id = h.requirement_history_id
            WHERE h.history_id IN ({','.join('?' for _ in ids)})""", ids))
    # Reviews are independent sources even when owned by a requirement root.
    # Do not guess a parent for missing/legacy units or unrelated conversations.
    return list(dict.fromkeys(roots.get(hid, hid) for hid in ids))


def normalize_history_roots(project: Path, record: dict) -> dict:
    result = {}
    if "history_ids" in record:
        result["history_ids"] = root_history_ids(project, record["history_ids"])
    if "related_history_ids" in record:
        direct = set(result.get("history_ids", []))
        result["related_history_ids"] = [hid for hid in root_history_ids(project, record["related_history_ids"])
                                         if hid not in direct]
    return result
