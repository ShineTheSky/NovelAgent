"""Codex project Hooks: exact turn binding, quiet snapshots, one end-of-turn reminder."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import sys

import prompt_history as history


def resolved_path(value):
    path = Path(value).resolve()
    if sys.platform == "win32":
        name = str(path)
        if name.startswith("\\\\?\\UNC\\"):
            path = Path("\\\\" + name[8:])
        elif re.match(r"^\\\\\?\\[A-Za-z]:\\", name):
            path = Path(name[4:])
    return path


def diagnostic(event, status, cwd=None):
    # stderr is separate from the Hook JSON response; never log prompt/tool bodies.
    fields = {key: event.get(key) for key in ("hook_event_name", "session_id", "turn_id", "cwd")}
    fields.update(component="prompt_history_hook", status=status)
    if cwd is not None:
        fields["normalized_cwd"] = str(cwd)
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr)


def context(event, message):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def identity(event):
    session, turn = event.get("session_id"), event.get("turn_id")
    if not isinstance(session, str) or not session or not isinstance(turn, str) or not turn:
        raise ValueError("Hook needs explicit session_id AND turn_id; old-task fallback is forbidden")
    return session, turn


def patch_touches_prompts(command, project, cwd=None):
    project = resolved_path(project)
    cwd = resolved_path(cwd or project)
    paths = re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", command, re.MULTILINE)
    for name in paths:
        path = resolved_path(cwd / Path(name.strip().replace("\\", "/")))
        if not path.is_relative_to(project):
            continue
        name = path.relative_to(project).as_posix()
        if history.managed(name):
            history.safe_path(project, name)
            return True
    return False


def handle(event, project=history.PROJECT, repo=None):
    project = resolved_path(project)
    repo = resolved_path(repo or project.with_name(project.name + "-PromptHistory"))
    cwd = resolved_path(event.get("cwd", ""))
    if not cwd.is_relative_to(project):
        diagnostic(event, "outside_project", cwd)
        return {}
    diagnostic(event, "accepted", cwd)
    name = event.get("hook_event_name")
    if name not in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}:
        return {}
    session, turn = identity(event)
    flags = f"--session-id {session} --turn-id {turn}"
    if name == "UserPromptSubmit":
        # No task, user text, or persistent state is created for ordinary conversation.
        return context(name, "Prompt backup context: " + flags +
            ". Only if editing managed prompts, run prompt_history.py begin with these EXACT IDs and --reason. "
            "Do not reuse a previous turn's task; continue it only through explicit bind.")
    tool = event.get("tool_name", "")
    command = event.get("tool_input", {}).get("command", "") if isinstance(event.get("tool_input"), dict) else ""
    if name in {"PreToolUse", "PostToolUse"}:
        # Shell commands are checked by hashes: do not try to parse arbitrary shell writes.
        if tool in {"apply_patch", "Edit", "Write"}:
            if not patch_touches_prompts(command, project, cwd):
                return {}
        elif tool not in {"Bash", "exec_command"}:
            return {}
    history.validate_repo(repo, project)
    db = history.task_db(repo)
    try:
        with db:
            task = history.active_task(db, session, turn)
            if name in {"PreToolUse", "PostToolUse"}:
                phase = "before-edit" if name == "PreToolUse" else "after-edit"
                # Drift already present before the edit is not attributed to the new task.
                assigned = task if phase == "after-edit" else None
                with redirect_stdout(io.StringIO()):
                    history.record_snapshot(repo, project, db, phase,
                        task["reason"] if assigned else f"Automatic prompt snapshot ({phase})",
                        session, turn, event.get("tool_use_id", ""), assigned)
                return {}
            # Stop is a reminder, NOT proof that a multi-turn requirement has completed.
            _, latest = history.manifest(repo, project)
            if history.changes(latest["files"], history.hashes(history.current_files(project))):
                with redirect_stdout(io.StringIO()):
                    history.record_snapshot(repo, project, db, "after-edit", "End-of-turn prompt backup",
                                            session, turn, task=task)
            pending = db.execute("""SELECT commit_id, task_id FROM snapshots WHERE session_id=? AND turn_id=?
                AND phase IN ('after-edit', 'finish') ORDER BY rowid""", (session, turn)).fetchall()
            missing = any(row["task_id"] is None for row in pending)
            unfinished = task and any(row["task_id"] == task["id"] for row in pending)
            if not (missing or unfinished) or event.get("stop_hook_active"):
                return {}
            inserted = db.execute("INSERT OR IGNORE INTO reminders VALUES (?, ?)", (session, turn)).rowcount
            if not inserted:
                return {}
            reason = ("Prompt changes were backed up. Complete their business description once, not per file. "
                f"Use scripts/prompt_history.py with {flags}. "
                "If unassigned, begin --reason with this turn's actual request; it adopts ONLY this turn's snapshots. "
                "If this requirement is complete, finish --reason with actual changes and run offline checks. "
                "If work is interrupted or continuing, leave the task active and report its ID; do not invent completion. "
                "Never adopt a previous turn/session automatically. Snapshots: " +
                ", ".join(row["commit_id"][:12] for row in pending))
            return {"decision": "block", "reason": reason}
    finally:
        db.close()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    event = {}
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Hook input must be a JSON object")
        result = handle(event)
    except Exception as exc:
        diagnostic(event if isinstance(event, dict) else {}, "failed")
        message = f"Prompt backup Hook failed: {exc}. Do not assume prompts were backed up."
        if isinstance(event, dict) and event.get("hook_event_name") == "PreToolUse":
            result = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason": message}}
        else:
            result = {"systemMessage": message}
    else:
        diagnostic(event, "completed")
    print(json.dumps(result, ensure_ascii=False))
