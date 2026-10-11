"""Preview/install project-local Hooks; preserve other handlers and never bypass trust."""

import argparse
import json
from pathlib import Path
import sys


PROJECT = Path(__file__).resolve().parents[1]


def build_config(existing, project, python):
    config = json.loads(json.dumps(existing))
    events = config.setdefault("hooks", {})
    script = (project / "scripts/prompt_history_hook.py").as_posix()
    command = f'"{python.as_posix()}" "{script}"'
    # Explicit launcher works from both PowerShell and cmd; quoted paths alone do not.
    windows_python = python.as_posix().replace("'", "''")
    windows_script = script.replace("'", "''")
    windows_command = f'powershell.exe -NoProfile -NonInteractive -Command "& \'{windows_python}\' \'{windows_script}\'"'
    for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        groups = events.setdefault(event, [])
        # Installer idempotence: replace only our own handler, preserving other hooks.
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", [])
                              if script not in h.get("command", "") and script not in h.get("commandWindows", "")]
        groups[:] = [g for g in groups if g.get("hooks")]
        handler = {"type": "command", "command": command, "commandWindows": windows_command,
                   "timeout": 30}
        if event != "Stop":
            handler["additionalContextLimit"] = 600
        group = {"hooks": [handler]}
        if event in {"PreToolUse", "PostToolUse"}:
            group["matcher"] = "^(apply_patch|Edit|Write|Bash|exec_command)$"
        groups.append(group)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    directory = project / ".codex"
    if not directory.resolve().is_relative_to(project):
        raise ValueError("Project .codex directory escapes the project")
    path = directory / "hooks.json"
    if not path.resolve().is_relative_to(project):
        raise ValueError("hooks.json escapes the project")
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = build_config(existing, project, Path(sys.executable))
    print(json.dumps(config, ensure_ascii=False, indent=2))
    if args.apply:
        directory.mkdir(exist_ok=True)
        if path.exists() and config != existing:
            # Preserve original config before merging. Do not overwrite a prior recovery copy.
            import time
            original = directory / f"hooks.before-prompt-history-{time.time_ns()}.json"
            original.write_bytes(path.read_bytes())
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Installed {path}. Review/trust the new Hook definitions in Codex; trust was not modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
