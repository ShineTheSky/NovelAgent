"""Local prompt snapshots. Never commit the source project or call a model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4


PROJECT = Path(__file__).resolve().parents[1]
FILES = (
    "novelagent/memory/extraction_prompts.py",
    "novelagent/trace/semantic_extraction.py",
    "config/subagent_presets.yaml",
    "novelagent/memory/consolidation.py",
)
OFFLINE_TESTS = (
    "tests/test_prompt_history.py",
    "tests/test_prompt_history_hooks.py",
    "tests/test_memory_extraction_prompts.py",
    "tests/test_memory_classifications.py",
    "tests/test_memory_event_references.py",
    "tests/test_evidence_text.py",
)


def git(repo, *args, binary=False):
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.hooksPath=",
         "-c", "user.name=Prompt History", "-c", "user.email=prompt-history@localhost",
         "-C", str(repo), *args], capture_output=True, check=True,
    )
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe snapshot path: {relative}")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes its root: {relative}")
    return target


def managed(relative):
    return relative in FILES or (
        relative.startswith("prompts/") and relative.endswith(".j2")
    )


def current_files(project):
    paths = set(FILES)
    paths.update(p.relative_to(project).as_posix() for p in (project / "prompts").rglob("*.j2"))
    return {name: safe_path(project, name).read_bytes() for name in sorted(paths)
            if safe_path(project, name).is_file()}


def hashes(files):
    return {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}


def changes(old, new):
    return {name: "added" if name not in old else "removed" if name not in new else "modified"
            for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)}


def manifest(repo, project, ref="HEAD"):
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    data = json.loads(git(repo, "show", f"{commit}:snapshot.json"))
    if data.get("format") != "novelagent-prompt-history-v1" or Path(data["project"]).resolve() != project:
        raise ValueError("Backup repository belongs to another project or has an unknown format")
    for name in data["files"]:
        safe_path(project, name)
        if not managed(name):
            raise ValueError(f"Not a managed prompt file: {name}")
    return commit, data


def validate_repo(repo, project):
    if repo == project or repo.is_relative_to(project) or project.is_relative_to(repo):
        raise ValueError("Backup repository must be separate from the source project")
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("Backup path must be the root of an independent Git repository")
    commit, data = manifest(repo, project)
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Backup repository has unsaved changes; inspect it before proceeding")
    return commit, data


def snapshot(repo, project, phase, reason, *, initial=False, context=None):
    previous = {} if initial else validate_repo(repo, project)[1]["files"]
    files = current_files(project)
    digest = hashes(files)
    delta = changes(previous, digest)
    if not delta and not initial:
        print("No prompt changes; no new snapshot.")
        return git(repo, "rev-parse", "HEAD")
    if not files:
        raise ValueError("No managed prompt files found")
    for name in previous.keys() - files.keys():
        safe_path(repo / "snapshot", name).unlink()
    for name, body in files.items():
        target = safe_path(repo / "snapshot", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    source_commit = git(project, "rev-parse", "HEAD")
    data = {"format": "novelagent-prompt-history-v1", "project": str(project),
            "created_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
            "reason": reason, "source_commit": source_commit, "files": digest,
            "source_status": git(project, "status", "--porcelain", "--", *sorted(previous.keys() | files.keys()))}
    if context:
        data["context"] = context
    (repo / "snapshot.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    git(repo, "add", "--", "snapshot", "snapshot.json")
    git(repo, "commit", "-m", f"{phase}: {reason}")
    commit = git(repo, "rev-parse", "HEAD")
    print(json.dumps({"snapshot": commit, "phase": phase, "changes": delta}, ensure_ascii=False, indent=2))
    return commit


def task_db(repo):
    """BEGIN IMMEDIATE serializes snapshot/notes writes across Hook and CLI processes."""
    directory = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    db = sqlite3.connect(directory / "prompt_history.sqlite", timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL, start_commit TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            end_commit TEXT NOT NULL DEFAULT '', tests_exit INTEGER);
        CREATE TABLE IF NOT EXISTS bindings (
            session_id TEXT, turn_id TEXT, task_id TEXT NOT NULL,
            PRIMARY KEY(session_id, turn_id));
        CREATE TABLE IF NOT EXISTS snapshots (
            commit_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            tool_use_id TEXT NOT NULL, phase TEXT NOT NULL, task_id TEXT);
        CREATE TABLE IF NOT EXISTS reminders (
            session_id TEXT, turn_id TEXT, PRIMARY KEY(session_id, turn_id));
    """)
    db.execute("BEGIN IMMEDIATE")
    return db


def active_task(db, session_id, turn_id):
    return db.execute("""SELECT t.* FROM bindings b JOIN tasks t ON t.id=b.task_id
        WHERE b.session_id=? AND b.turn_id=? AND t.session_id=? AND t.status='active'""",
        (session_id, turn_id, session_id)).fetchone()


def write_note(repo, db, commit):
    row = dict(db.execute("SELECT * FROM snapshots WHERE commit_id=?", (commit,)).fetchone())
    if row["task_id"]:
        row["task"] = dict(db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone())
    git(repo, "notes", "--ref=prompt-history", "add", "-f", "-m",
        json.dumps(row, ensure_ascii=False), commit)


def record_snapshot(repo, project, db, phase, reason, session_id, turn_id, tool_use_id="", task=None):
    old = git(repo, "rev-parse", "HEAD")
    context = {"session_id": session_id, "turn_id": turn_id, "tool_use_id": tool_use_id,
               "task_id": task["id"] if task else None}
    commit = snapshot(repo, project, phase, reason, context=context)
    if commit != old:
        db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (commit, session_id, turn_id, tool_use_id, phase, context["task_id"]))
        write_note(repo, db, commit)
    return commit


def begin_task(repo, project, session_id, turn_id, reason):
    validate_repo(repo, project)
    db = task_db(repo)
    try:
        with db:
            existing = active_task(db, session_id, turn_id)
            if existing:
                if existing["reason"] != reason:
                    raise ValueError("This turn already has a different active task; finish it before starting another")
                return dict(existing)
            # Pre-existing drift is a baseline, not evidence of this task's changes.
            start = record_snapshot(repo, project, db, "before-task", reason, session_id, turn_id)
            unassigned = db.execute("""SELECT commit_id FROM snapshots WHERE session_id=? AND turn_id=?
                AND task_id IS NULL AND phase IN ('after-edit', 'finish') ORDER BY rowid""",
                (session_id, turn_id)).fetchall()
            if unassigned:
                start = git(repo, "rev-parse", f"{unassigned[0]['commit_id']}^")
            task_id = "prompt_" + uuid4().hex
            db.execute("INSERT INTO tasks(id, session_id, reason, status, start_commit) VALUES (?, ?, ?, 'active', ?)",
                (task_id, session_id, reason, start))
            db.execute("INSERT OR REPLACE INTO bindings VALUES (?, ?, ?)", (session_id, turn_id, task_id))
            for row in unassigned:
                db.execute("UPDATE snapshots SET task_id=? WHERE commit_id=?", (task_id, row["commit_id"]))
                write_note(repo, db, row["commit_id"])
            return dict(active_task(db, session_id, turn_id))
    finally:
        db.close()


def bind_task(repo, project, task_id, session_id, turn_id):
    validate_repo(repo, project)
    db = task_db(repo)
    try:
        with db:
            task = db.execute("SELECT * FROM tasks WHERE id=? AND session_id=? AND status='active'",
                              (task_id, session_id)).fetchone()
            if not task:
                raise ValueError("Only an active task in this session can be explicitly continued")
            existing = active_task(db, session_id, turn_id)
            if existing and existing["id"] != task_id:
                raise ValueError("This turn is already bound to another task")
            db.execute("INSERT OR REPLACE INTO bindings VALUES (?, ?, ?)", (session_id, turn_id, task_id))
            rows = db.execute("""SELECT commit_id FROM snapshots WHERE session_id=? AND turn_id=?
                AND task_id IS NULL AND phase='after-edit'""", (session_id, turn_id)).fetchall()
            for row in rows:
                db.execute("UPDATE snapshots SET task_id=? WHERE commit_id=?", (task_id, row["commit_id"]))
                write_note(repo, db, row["commit_id"])
            return dict(task)
    finally:
        db.close()


def finish_task(repo, project, session_id, turn_id, summary):
    validate_repo(repo, project)
    db = task_db(repo)
    try:
        with db:
            task = active_task(db, session_id, turn_id)
            if not task:
                raise ValueError("No active task registered for THIS session and turn; run begin or explicit bind")
            record_snapshot(repo, project, db, "finish", summary, session_id, turn_id, task=task)
            rows = db.execute("SELECT commit_id FROM snapshots WHERE task_id=? ORDER BY rowid", (task["id"],)).fetchall()
            if not rows:
                db.execute("DELETE FROM bindings WHERE task_id=?", (task["id"],))
                db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
                print("No actual prompt edits; empty task removed.")
                return 0
            # Commit metadata before tests, so a crash still retains evidence and pending status.
            db.execute("UPDATE tasks SET summary=? WHERE id=?", (summary, task["id"]))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", *OFFLINE_TESTS, "-p", "no:cacheprovider"], cwd=project)
        db.execute("BEGIN IMMEDIATE")
        with db:
            db.execute("UPDATE tasks SET status=?, end_commit=?, tests_exit=? WHERE id=?",
                ("completed" if result.returncode == 0 else "tests_failed", rows[-1]["commit_id"], result.returncode, task["id"]))
            for row in rows:
                write_note(repo, db, row["commit_id"])
        print(json.dumps({"task_id": task["id"], "tests_exit": result.returncode}, ensure_ascii=False))
        return result.returncode
    finally:
        db.close()


def restore(repo, project, ref, selected, apply):
    validate_repo(repo, project)
    commit, target = manifest(repo, project, ref)
    live = current_files(project)
    names = selected or sorted(live.keys() | target["files"].keys())
    bodies = {}
    for name in names:
        safe_path(project, name)
        if not managed(name) or name not in live.keys() | target["files"].keys():
            raise ValueError(f"Not a known prompt file: {name}")
        if name in target["files"]:
            body = git(repo, "show", f"{commit}:snapshot/{name}", binary=True)
            if hashlib.sha256(body).hexdigest() != target["files"][name]:
                raise ValueError(f"Snapshot hash mismatch: {name}")
            bodies[name] = body
    live_hashes = hashes(live)
    delta = changes({n: live_hashes[n] for n in names if n in live}, hashes(bodies))
    print(json.dumps({"restore_from": commit, "changes": delta, "apply": apply}, ensure_ascii=False, indent=2))
    if not apply:
        import difflib
        for name in delta:
            print("".join(difflib.unified_diff(
                live.get(name, b"").decode("utf-8").splitlines(keepends=True),
                bodies.get(name, b"").decode("utf-8").splitlines(keepends=True),
                fromfile=f"current/{name}", tofile=f"{commit[:12]}/{name}")))
        return
    if delta:
        snapshot(repo, project, "before-restore", f"Preserve current prompts before restoring {commit[:12]}")
        for name in delta:
            path = safe_path(project, name)
            if name in bodies:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bodies[name])
            else:
                path.unlink()
        snapshot(repo, project, "restored", f"Restore {commit[:12]} ({', '.join(delta)})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--repo", type=Path, help="Default: sibling <project>-PromptHistory")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "begin", "finish"):
        action = sub.add_parser(command)
        action.add_argument("--reason", required=True)
        if command != "init":
            action.add_argument("--session-id")
            action.add_argument("--turn-id")
    resume = sub.add_parser("bind")
    resume.add_argument("task_id")
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--turn-id", required=True)
    show = sub.add_parser("tasks")
    show.add_argument("--session-id", required=True)
    show.add_argument("--turn-id", required=True)
    sub.add_parser("check")
    sub.add_parser("list")
    compare = sub.add_parser("compare")
    compare.add_argument("before")
    compare.add_argument("after", nargs="?", default="HEAD")
    compare.add_argument("--file", action="append", default=[])
    undo = sub.add_parser("restore")
    undo.add_argument("ref")
    undo.add_argument("--file", action="append", default=[])
    undo.add_argument("--apply", action="store_true", help="Without this flag, only preview differences")
    branch = sub.add_parser("branch")
    branch.add_argument("name", help="Create and switch the BACKUP branch only; source files stay unchanged")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    repo = (args.repo or project.with_name(project.name + "-PromptHistory")).resolve()
    if args.command == "init":
        if repo == project or repo.is_relative_to(project) or project.is_relative_to(repo):
            raise ValueError("Backup repository must be separate from the source project")
        if repo.exists() and any(repo.iterdir()):
            validate_repo(repo, project)
            snapshot(repo, project, "init", args.reason)
        else:
            if not current_files(project):
                raise ValueError("No managed prompt files found")
            git(project, "rev-parse", "HEAD")
            repo.mkdir(parents=True, exist_ok=True)
            git(repo, "init", "--initial-branch=main")
            snapshot(repo, project, "init", args.reason, initial=True)
        return 0
    _, data = validate_repo(repo, project)
    if args.command in {"begin", "finish"} and (args.session_id or args.turn_id):
        if not args.session_id or not args.turn_id:
            raise ValueError("Both session-id and turn-id are required; never infer a latest task")
        if args.command == "begin":
            print(json.dumps(begin_task(repo, project, args.session_id, args.turn_id, args.reason), ensure_ascii=False))
            return 0
        return finish_task(repo, project, args.session_id, args.turn_id, args.reason)
    if args.command == "bind":
        print(json.dumps(bind_task(repo, project, args.task_id, args.session_id, args.turn_id), ensure_ascii=False))
        return 0
    if args.command == "tasks":
        db = task_db(repo)
        try:
            task = active_task(db, args.session_id, args.turn_id)
            rows = db.execute("SELECT * FROM snapshots WHERE session_id=? AND turn_id=? ORDER BY rowid",
                              (args.session_id, args.turn_id)).fetchall()
            print(json.dumps({"active_task": dict(task) if task else None,
                              "snapshots": [dict(row) for row in rows]}, ensure_ascii=False, indent=2))
            db.commit()
        finally:
            db.close()
        return 0
    if args.command in {"begin", "finish"}:
        snapshot(repo, project, args.command, args.reason)
        if args.command == "finish":
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", *OFFLINE_TESTS], cwd=project)
            print(f"Offline tests exit code: {result.returncode}; snapshot retained regardless of result.")
            return result.returncode
    elif args.command == "check":
        print(json.dumps({"backup_branch": git(repo, "branch", "--show-current"),
            "needs_backup": bool(delta := changes(data["files"], hashes(current_files(project)))),
            "changes": delta}, ensure_ascii=False, indent=2))
    elif args.command == "list":
        print(git(repo, "log", "--branches", "--notes=prompt-history", "--date=iso", "--format=%h %ad %d %s%n%N"))
    elif args.command == "compare":
        before, _ = manifest(repo, project, args.before)
        after, _ = manifest(repo, project, args.after)
        for name in args.file:
            safe_path(project, name)
            if not managed(name):
                raise ValueError(f"Not a managed prompt file: {name}")
        paths = [f"snapshot/{name}" for name in args.file] or ["snapshot"]
        print(git(repo, "diff", before, after, "--", *paths))
    elif args.command == "restore":
        restore(repo, project, args.ref, args.file, args.apply)
    elif args.command == "branch":
        git(repo, "check-ref-format", "--branch", args.name)
        snapshot(repo, project, "before-branch", f"Preserve prompts before experiment {args.name}")
        git(repo, "switch", "-c", args.name)
        print(f"Backup branch: {args.name}. Source project branch and files are unchanged.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Prompt history failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        sys.exit(1)
