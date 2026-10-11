"""Independent temporary Git repositories only; no API or production writes."""

import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("prompt_history", Path(__file__).parents[1] / "scripts/prompt_history.py")
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def test_embedded_memory_correction_prompts_are_managed():
    assert history.managed("novelagent/memory/consolidation.py")


@pytest.fixture
def repos(tmp_path):
    project, backup = tmp_path / "source", tmp_path / "backup"
    project.mkdir()
    history.git(project, "init", "--initial-branch=main")
    prompt = project / history.FILES[0]
    prompt.parent.mkdir(parents=True)
    prompt.write_bytes(b'PROMPT = "old"\r\n')
    (project / "unrelated.txt").write_text("leave alone", encoding="utf-8")
    history.git(project, "add", ".")
    history.git(project, "commit", "-m", "baseline")
    args = ["--project", str(project), "--repo", str(backup)]
    history.main([*args, "init", "--reason", "baseline"])
    return project, backup, prompt, args


def test_snapshot_detects_divergence_is_idempotent_and_does_not_commit_source(repos, capsys):
    project, backup, prompt, args = repos
    source_head = history.git(project, "rev-parse", "HEAD")
    first = history.git(backup, "rev-parse", "HEAD")
    capsys.readouterr()
    history.main([*args, "check"])
    assert json.loads(capsys.readouterr().out)["needs_backup"] is False
    prompt.write_bytes(b'PROMPT = "new"\n')
    history.main([*args, "check"])
    assert json.loads(capsys.readouterr().out)["needs_backup"] is True
    history.main([*args, "begin", "--reason", "capture previous manual edit"])
    second = history.git(backup, "rev-parse", "HEAD")
    assert first != second
    assert history.git(backup, "show", f"HEAD:snapshot/{history.FILES[0]}", binary=True) == prompt.read_bytes()
    history.main([*args, "begin", "--reason", "unchanged"])
    assert history.git(backup, "rev-parse", "HEAD") == second
    assert history.git(project, "rev-parse", "HEAD") == source_head
    assert not (backup / "snapshot/unrelated.txt").exists()


def test_restore_previews_preserves_current_and_restores_only_selected_file(repos, capsys):
    project, backup, prompt, args = repos
    old = history.git(backup, "rev-parse", "HEAD")
    prompt.write_bytes(b'PROMPT = "unsaved experiment"\n')
    other = project / "prompts/other.j2"
    other.parent.mkdir()
    other.write_text("keep this", encoding="utf-8")
    history.main([*args, "restore", old, "--file", history.FILES[0]])
    assert b"unsaved experiment" in prompt.read_bytes()
    assert "unsaved experiment" in capsys.readouterr().out
    history.main([*args, "restore", old, "--file", history.FILES[0], "--apply"])
    assert prompt.read_bytes() == b'PROMPT = "old"\r\n'
    assert b"unsaved experiment" in history.git(backup, "show", f"HEAD~1:snapshot/{history.FILES[0]}", binary=True)
    assert other.read_text(encoding="utf-8") == "keep this"
    assert (project / "unrelated.txt").read_text(encoding="utf-8") == "leave alone"


def test_snapshots_and_restores_additions_and_removals(repos):
    project, backup, prompt, args = repos
    original = history.git(backup, "rev-parse", "HEAD")
    extra = project / "prompts/new.j2"
    extra.parent.mkdir()
    extra.write_text("new", encoding="utf-8")
    history.main([*args, "begin", "--reason", "new template"])
    added = history.git(backup, "rev-parse", "HEAD")
    history.main([*args, "restore", original, "--apply"])
    assert not extra.exists()
    history.main([*args, "restore", added, "--apply"])
    assert extra.read_text(encoding="utf-8") == "new"
    extra.unlink()
    history.main([*args, "begin", "--reason", "deleted template"])
    assert "prompts/new.j2" not in history.manifest(backup, project)[1]["files"]


def test_experiment_branch_does_not_switch_source_and_can_compare(repos, capsys):
    project, backup, prompt, args = repos
    history.main([*args, "branch", "codex/experiment"])
    assert history.git(backup, "branch", "--show-current") == "codex/experiment"
    assert history.git(project, "branch", "--show-current") == "main"
    prompt.write_bytes(b'PROMPT = "alternative"\n')
    history.main([*args, "begin", "--reason", "alternative"])
    capsys.readouterr()
    history.main([*args, "compare", "main", "HEAD", "--file", history.FILES[0]])
    assert '+PROMPT = "alternative"' in capsys.readouterr().out
    assert b'"old"' in history.git(backup, "show", f"main:snapshot/{history.FILES[0]}", binary=True)


def test_finish_snapshots_even_when_offline_tests_fail(repos, monkeypatch):
    project, backup, prompt, args = repos
    prompt.write_bytes(b'PROMPT = "broken"\n')
    original_run = history.subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] == history.sys.executable:
            calls.append(command)
            return history.subprocess.CompletedProcess(command, 1)
        return original_run(command, **kwargs)

    monkeypatch.setattr(history.subprocess, "run", run)
    assert history.main([*args, "finish", "--reason", "failed experiment"]) == 1
    assert history.git(backup, "show", f"HEAD:snapshot/{history.FILES[0]}", binary=True) == prompt.read_bytes()
    assert calls == [[history.sys.executable, "-m", "pytest", "-q", *history.OFFLINE_TESTS]]
    assert not any("scripts/test_trace_prompt.py" in call for call in calls)


def test_refuses_dirty_backup_other_project_and_unmanaged_restore(repos):
    project, backup, prompt, args = repos
    with pytest.raises(ValueError, match="known prompt"):
        history.main([*args, "restore", "HEAD", "--file", "unrelated.txt", "--apply"])
    with pytest.raises(ValueError, match="Unsafe"):
        history.main([*args, "restore", "HEAD", "--file", "../outside.py", "--apply"])
    (backup / "unexpected.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(ValueError, match="unsaved changes"):
        history.main([*args, "begin", "--reason", "must not overwrite"])
    assert prompt.read_bytes() == b'PROMPT = "old"\r\n'
    with pytest.raises(ValueError, match="another project"):
        history.manifest(backup, project.parent)


def test_rejects_nested_backup_and_non_repository_directory(repos, tmp_path):
    project, backup, prompt, args = repos
    with pytest.raises(ValueError, match="separate"):
        history.main(["--project", str(project), "--repo", str(project / "backup"),
                      "init", "--reason", "invalid"])
    existing = tmp_path / "occupied"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(history.subprocess.CalledProcessError):
        history.main(["--project", str(project), "--repo", str(existing), "init", "--reason", "invalid"])
    assert not (existing / ".git").exists()


def test_snapshot_does_not_copy_symlink_outside_project(repos, tmp_path):
    project, backup, prompt, args = repos
    outside = tmp_path / "private.j2"
    outside.write_text("secret", encoding="utf-8")
    link = project / "prompts/outside.j2"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation requires privileges on this Windows host")
    with pytest.raises(ValueError, match="escapes"):
        history.main([*args, "begin", "--reason", "must not copy outside data"])
