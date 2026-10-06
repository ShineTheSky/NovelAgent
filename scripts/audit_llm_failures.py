"""Read-only failure inventory; excludes diagnostic copies of the same run."""

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


def audit(database):
    counts, examples, seen = Counter(), {}, set()
    database = Path(database).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""SELECT t.trace_id,t.source_agent,t.agent_position,t.started_at,
            e.payload_json FROM traces t JOIN trace_events e ON e.trace_id=t.trace_id
            WHERE t.status='failed' AND t.agent_position!='bad_case'
              AND e.event_type IN ('agent_finished','error') ORDER BY t.started_at,e.sequence_no""")
        for trace_id, actor, position, started, raw in rows:
            payload = json.loads(raw)
            error = str(payload.get("error") or payload.get("message") or "")
            if not error or trace_id in seen:
                continue
            seen.add(trace_id)
            reason = re.sub(r"\(request_id:.*", "", error.splitlines()[0]).strip()
            reason = re.sub(r"line \d+ column \d+ \(char \d+\)", "line N column N", reason)
            reason = re.sub(r"sk-[\w-]+", "[REDACTED]", reason)
            key = (actor, position, reason)
            counts[key] += 1
            examples.setdefault(key, {"trace_id": trace_id, "started_at": started})
    return {"mode": "read_only", "failed_runs_with_errors": len(seen),
            "failures": [{"actor": k[0], "position": k[1], "reason": k[2], "count": n,
                          "example": examples[k]} for k, n in counts.most_common()],
            "note": "Counts are failed runs, not billing tokens or fees. Post-publication failures also have separate local BadCases."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/novelagent.db")
    args = parser.parse_args()
    print(json.dumps(audit(args.database), ensure_ascii=False, indent=2))
