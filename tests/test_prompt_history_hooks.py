"""Actual temporary Git/SQLite, synthetic Codex events, no model calls."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prompt_history as history
import prompt_history_hook as hook
import install_prompt_history_hooks as installer


@pytest.fixture
def prompt_repo(tmp_path):
    project, backup = tmp_path / "project", tmp_path / "backup"
    project.mkdir()
    history.git(project, "init", "--initial-branch=main")
    first = project / history.FILES[0]
    first.parent.mkdir(parents=True)
    first.write_text('PROMPT = "baseline"\n', encoding="utf-8")
    second = project / "prompts/base.j2"
    second.parent.mkdir()
    second.write_text("baseline template", encoding="utf-8")
    history.git(project, "add", ".")
    history.git(project, "commit", "-m", "source baseline")
    history.main(["--project", str(project), "--repo", str(backup), "init", "--reason", "test baseline"])
    return project, backup, first, second


def event(project, name, turn="t1", session="s1", tool="apply_patch", path=history.FILES[0]):
    return {"cwd": str(project), "session_id": session, "turn_id": turn,
            "hook_event_name": name, "tool_use_id": "call-" + turn, "tool_name": tool,
            "tool_input": {"command": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch"}}


def run_hook(fixture, name, **kwargs):
    project, backup, *_ = fixture
    return hook.handle(event(project, name, **kwargs), project, backup)


def edit(fixture, file, value, **kwargs):
    project, backup, *_ = fixture
    path = file.relative_to(project).as_posix()
    assert run_hook(fixture, "PreToolUse", path=path, **kwargs) == {}
    file.write_text(value, encoding="utf-8")
    assert run_hook(fixture, "PostToolUse", path=path, **kwargs) == {}
    return history.git(backup, "rev-parse", "HEAD")


def task_rows(backup, table="tasks"):
    db = history.task_db(backup)
    try:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        db.rollback()
        db.close()


def fake_tests(monkeypatch, exit_code=0):
    original = history.subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] == sys.executable:
            calls.append(command)
            return history.subprocess.CompletedProcess(command, exit_code)
        return original(command, **kwargs)

    monkeypatch.setattr(history.subprocess, "run", run)
    return calls


def test_ordinary_turn_provides_identity_without_creating_tasks_or_copying_user_text(prompt_repo):
    project, backup, *_ = prompt_repo
    data = event(project, "UserPromptSubmit")
    data["prompt"] = "private ordinary conversation"
    reply = hook.handle(data, project, backup)
    assert "--session-id s1 --turn-id t1" in reply["hookSpecificOutput"]["additionalContext"]
    assert "private" not in json.dumps(reply)
    assert not (backup / ".git/prompt_history.sqlite").exists()
    assert run_hook(prompt_repo, "PreToolUse", path="unrelated.py") == {}
    assert not (backup / ".git/prompt_history.sqlite").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended paths")
@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("registered", [False, True])
def test_windows_cwd_forms_preserve_identity_snapshots_and_reminder(prompt_repo, extended, subdirectory, registered):
    project, backup, first, _ = prompt_repo
    source_head = history.git(project, "rev-parse", "HEAD")
    cwd = first.parent if subdirectory else project
    prefix = "\\\\?\\" if extended else ""
    data = event(project, "UserPromptSubmit")
    data["cwd"] = prefix + str(cwd)
    reply = hook.handle(data, project, backup)
    assert "--session-id s1 --turn-id t1" in reply["hookSpecificOutput"]["additionalContext"]
    assert not (backup / ".git/prompt_history.sqlite").exists()
    task = history.begin_task(backup, project, "s1", "t1", "Windows path task") if registered else None
    relative = first.name if subdirectory else history.FILES[0]
    data["tool_input"]["command"] = "*** Update File: " + relative
    data["hook_event_name"] = "PreToolUse"
    assert hook.handle(data, project, backup) == {}
    first.write_text("windows path edit", encoding="utf-8")
    data["hook_event_name"] = "PostToolUse"
    assert hook.handle(data, project, backup) == {}
    assert history.git(backup, "show", f"HEAD:snapshot/{history.FILES[0]}") == "windows path edit"
    assert task_rows(backup, "snapshots")[0]["task_id"] == (task["id"] if task else None)
    data["hook_event_name"] = "Stop"
    assert hook.handle(data, project, backup)["decision"] == "block"
    assert hook.handle(data, project, backup) == {}
    assert history.git(project, "rev-parse", "HEAD") == source_head


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended paths")
def test_extended_patch_path_and_project_paths_are_normalized(prompt_repo):
    project, backup, first, _ = prompt_repo
    prefix = "\\\\?\\"
    assert hook.patch_touches_prompts("*** Update File: " + prefix + str(first), project)
    data = event(project, "UserPromptSubmit")
    data["cwd"] = prefix + str(project)
    reply = hook.handle(data, Path(prefix + str(project)), Path(prefix + str(backup)))
    assert reply["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    data["cwd"] = prefix + str(project.parent / (project.name + "-other"))
    assert hook.handle(data, project, backup) == {}
    assert not (backup / ".git/prompt_history.sqlite").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended paths")
def test_extended_unc_path_normalization_without_network_access(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self: self)
    assert hook.resolved_path(r"\\?\UNC\server\share\project") == Path(r"\\server\share\project")


def test_multifile_edits_group_into_one_task_and_finish_adds_notes_without_rewriting_commits(prompt_repo, monkeypatch):
    project, backup, first, second = prompt_repo
    source_head = history.git(project, "rev-parse", "HEAD")
    task = history.begin_task(backup, project, "s1", "t1", "Clarify requirement sources")
    one = edit(prompt_repo, first, 'PROMPT = "new boundary"')
    two = edit(prompt_repo, second, "updated template")
    assert one != two
    assert {row["task_id"] for row in task_rows(backup, "snapshots")} == {task["id"]}
    calls = fake_tests(monkeypatch)
    assert history.finish_task(backup, project, "s1", "t1", "Both prompts updated consistently") == 0
    assert len(calls) == 1 and calls[0][3:] == ["-q", *history.OFFLINE_TESTS, "-p", "no:cacheprovider"]
    assert history.git(backup, "rev-parse", "HEAD") == two
    for commit in (one, two):
        note = json.loads(history.git(backup, "notes", "--ref=prompt-history", "show", commit))
        assert note["task"]["summary"] == "Both prompts updated consistently"
        assert note["task"]["tests_exit"] == 0
    assert run_hook(prompt_repo, "Stop") == {}
    assert history.git(project, "rev-parse", "HEAD") == source_head


def test_missing_registration_keeps_unassigned_then_adopts_only_exact_turn(prompt_repo):
    project, backup, first, second = prompt_repo
    first_commit = edit(prompt_repo, first, "first turn")
    second_commit = edit(prompt_repo, second, "second turn", turn="t2")
    assert all(row["task_id"] is None for row in task_rows(backup, "snapshots"))
    task = history.begin_task(backup, project, "s1", "t2", "Only second request")
    records = {row["commit_id"]: row for row in task_rows(backup, "snapshots")}
    assert records[first_commit]["task_id"] is None
    assert records[second_commit]["task_id"] == task["id"]
    assert task["start_commit"] == first_commit


def test_stale_active_task_not_used_in_new_turn_or_other_session(prompt_repo):
    project, backup, first, second = prompt_repo
    old = history.begin_task(backup, project, "s1", "t1", "Old request")
    edit(prompt_repo, first, "new turn", turn="t2")
    edit(prompt_repo, second, "different session", turn="t1", session="s2")
    assert all(row["task_id"] is None for row in task_rows(backup, "snapshots"))
    with pytest.raises(ValueError, match="THIS session and turn"):
        history.finish_task(backup, project, "s1", "t2", "must not close old request")
    with pytest.raises(ValueError, match="this session"):
        history.bind_task(backup, project, old["id"], "s2", "t1")


def test_explicit_resume_across_turns_and_closed_task_cannot_rebind(prompt_repo, monkeypatch):
    project, backup, first, second = prompt_repo
    task = history.begin_task(backup, project, "s1", "t1", "Continue one requirement")
    edit(prompt_repo, first, "first part")
    history.bind_task(backup, project, task["id"], "s1", "t2")
    edit(prompt_repo, second, "continued", turn="t2")
    assert {row["task_id"] for row in task_rows(backup, "snapshots")} == {task["id"]}
    fake_tests(monkeypatch)
    history.finish_task(backup, project, "s1", "t2", "complete")
    with pytest.raises(ValueError, match="active task"):
        history.bind_task(backup, project, task["id"], "s1", "t3")


def test_stop_reminds_once_not_per_edit_and_does_not_close_task(prompt_repo):
    project, backup, first, second = prompt_repo
    task = history.begin_task(backup, project, "s1", "t1", "unfinished requirement")
    edit(prompt_repo, first, "a")
    edit(prompt_repo, second, "b")
    reply = run_hook(prompt_repo, "Stop")
    assert reply["decision"] == "block" and "once, not per file" in reply["reason"]
    assert run_hook(prompt_repo, "Stop") == {}
    assert task_rows(backup)[0]["status"] == "active"
    next_stop = event(project, "Stop", turn="t2")
    next_stop["stop_hook_active"] = True
    assert hook.handle(next_stop, project, backup) == {}


def test_stop_snapshots_shell_drift_even_if_post_event_was_missed(prompt_repo):
    project, backup, first, second = prompt_repo
    first.write_text("missed edit", encoding="utf-8")
    assert run_hook(prompt_repo, "Stop")["decision"] == "block"
    assert history.git(backup, "show", f"HEAD:snapshot/{history.FILES[0]}") == "missed edit"
    assert task_rows(backup, "snapshots")[0]["task_id"] is None


def test_preexisting_drift_not_attributed_to_task_and_shell_hash_checks(prompt_repo):
    project, backup, first, second = prompt_repo
    history.begin_task(backup, project, "s1", "t1", "Current task")
    first.write_text("older manual change", encoding="utf-8")
    assert run_hook(prompt_repo, "PreToolUse", tool="Bash") == {}
    assert task_rows(backup, "snapshots")[0]["task_id"] is None
    second.write_text("shell write", encoding="utf-8")
    assert run_hook(prompt_repo, "PostToolUse", tool="Bash") == {}
    records = task_rows(backup, "snapshots")
    assert records[1]["task_id"] is not None


def test_no_change_creates_no_snapshot_and_empty_task_removed(prompt_repo):
    project, backup, *_ = prompt_repo
    initial = history.git(backup, "rev-parse", "HEAD")
    run_hook(prompt_repo, "PreToolUse")
    run_hook(prompt_repo, "PostToolUse")
    assert run_hook(prompt_repo, "Stop") == {}
    assert not task_rows(backup) and not task_rows(backup, "snapshots")
    history.begin_task(backup, project, "s1", "t1", "never edited")
    assert history.finish_task(backup, project, "s1", "t1", "no change") == 0
    assert task_rows(backup) == []
    assert history.git(backup, "rev-parse", "HEAD") == initial


def test_failure_results_kept_and_begin_idempotent_but_different_request_rejected(prompt_repo, monkeypatch):
    project, backup, first, second = prompt_repo
    task = history.begin_task(backup, project, "s1", "t1", "one request")
    assert history.begin_task(backup, project, "s1", "t1", "one request")["id"] == task["id"]
    with pytest.raises(ValueError, match="different active task"):
        history.begin_task(backup, project, "s1", "t1", "different request")
    commit = edit(prompt_repo, first, "failed candidate")
    fake_tests(monkeypatch, 1)
    assert history.finish_task(backup, project, "s1", "t1", "candidate failed tests") == 1
    assert task_rows(backup)[0]["status"] == "tests_failed"
    assert history.git(backup, "rev-parse", "HEAD") == commit


def test_identity_missing_refuses_latest_fallback_and_other_project_is_ignored(prompt_repo):
    project, backup, *_ = prompt_repo
    data = event(project, "PreToolUse")
    del data["turn_id"]
    with pytest.raises(ValueError, match="old-task fallback"):
        hook.handle(data, project, backup)
    data["cwd"] = str(project.parent)
    assert hook.handle(data, project, backup) == {}


def test_installer_merges_idempotently_and_preserves_other_hooks():
    project = Path("D:/example/project")
    existing = {"description": "existing", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "other-hook"}]}]}}
    first = installer.build_config(existing, project, Path("D:/python.exe"))
    assert existing["hooks"]["Stop"][0]["hooks"][0]["command"] == "other-hook"
    assert len(first["hooks"]["Stop"]) == 2
    assert installer.build_config(first, project, Path("D:/python.exe")) == first
    assert first["hooks"]["PostToolUse"][0]["hooks"][0]["additionalContextLimit"] == 600
    assert "additionalContextLimit" not in first["hooks"]["Stop"][-1]["hooks"][0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command shells")
@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh.exe", "cmd.exe"])
@pytest.mark.parametrize("extended", [False, True])
def test_installed_windows_command_launches_hook_through_shell(tmp_path, shell, extended):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    project = tmp_path / "project with spaces and 'quote'"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ("prompt_history_hook.py", "prompt_history.py"):
        shutil.copyfile(Path(hook.__file__).parent / name, scripts / name)
    config = installer.build_config({}, project, Path(sys.executable))
    command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["commandWindows"]
    data = event(project, "UserPromptSubmit")
    if extended:
        data["cwd"] = "\\\\?\\" + str(project)
    data["prompt"] = "private ordinary conversation"
    # cmd parses its own raw command tail; Python's argv escaping is not cmd quoting.
    invocation = (f'"{executable}" /d /s /c "{command}"' if shell == "cmd.exe" else
                  [executable, "-NoProfile", "-NonInteractive", "-Command", command])
    result = subprocess.run(invocation, input=json.dumps(data).encode(),
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    reply = json.loads(result.stdout.decode("utf-8"))
    assert "--session-id s1 --turn-id t1" in reply["hookSpecificOutput"]["additionalContext"]
    diagnostics = [json.loads(line) for line in result.stderr.decode("utf-8").splitlines()]
    assert [row["status"] for row in diagnostics] == ["accepted", "completed"]
    assert "private ordinary conversation" not in result.stderr.decode("utf-8")
    assert not project.with_name(project.name + "-PromptHistory").exists()


@pytest.mark.parametrize("extended", [False, True])
def test_real_hook_entry_accepts_utf8_and_outputs_only_valid_json(extended):
    if extended and sys.platform != "win32":
        pytest.skip("Windows extended paths")
    project = Path(__file__).parents[1]
    data = event(project, "UserPromptSubmit")
    if extended:
        data["cwd"] = "\\\\?\\" + str(project.resolve())
    data["prompt"] = "普通对话，不修改提示词"
    reply = subprocess.run([sys.executable, str(project / "scripts/prompt_history_hook.py")],
        input=json.dumps(data, ensure_ascii=False).encode("utf-8"), capture_output=True, check=True)
    parsed = json.loads(reply.stdout.decode("utf-8"))
    assert parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "普通对话" not in reply.stdout.decode("utf-8")
    diagnostics = [json.loads(line) for line in reply.stderr.decode("utf-8").splitlines()]
    assert [row["status"] for row in diagnostics] == ["accepted", "completed"]
    assert diagnostics[0]["normalized_cwd"] == str(project.resolve())
    assert "普通对话" not in reply.stderr.decode("utf-8")


def test_outside_project_reports_skip_without_backup(prompt_repo, capsys):
    project, backup, *_ = prompt_repo
    data = event(project, "UserPromptSubmit")
    data["cwd"] = str(project.parent)
    assert hook.handle(data, project, backup) == {}
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["status"] == "outside_project"
    assert not (backup / ".git/prompt_history.sqlite").exists()


def test_real_hook_entry_denies_missing_identity_without_using_stale_state():
    project = Path(__file__).parents[1]
    data = event(project, "PreToolUse")
    del data["turn_id"]
    reply = subprocess.run([sys.executable, str(project / "scripts/prompt_history_hook.py")],
        input=json.dumps(data).encode("utf-8"), capture_output=True, check=True)
    parsed = json.loads(reply.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert parsed["permissionDecision"] == "deny"
    assert "old-task fallback" in parsed["permissionDecisionReason"]


def test_patch_paths_normalized_from_session_directory(prompt_repo):
    project, backup, first, second = prompt_repo
    assert hook.patch_touches_prompts("*** Update File: ./" + history.FILES[0], project)
    assert hook.patch_touches_prompts("*** Update File: extraction_prompts.py", project, first.parent)
    assert hook.patch_touches_prompts("*** Add File: " + str(second), project)
    assert not hook.patch_touches_prompts("*** Update File: ../outside.py", project)
