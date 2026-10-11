# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Overview

NovelAgent2 is an AI-powered novel writing agent that orchestrates specialized sub-agents (chapter writer, reviewer, character designer, outliner) through a ReAct loop. The backend is Python/FastAPI, the frontend is React/TypeScript/Vite/Tailwind.

## Core Tradeoff
These guidelines bias toward **caution over speed**. For trivial tasks, use judgment.

## Prompt backup workflow (mandatory for Codex prompt edits)

Prompt backups live in the independent sibling repository `../NovelAgent2-PromptHistory`.
The source project's normal Git workflow and runtime prompt loading stay unchanged.

- Only when editing `novelagent/memory/extraction_prompts.py`, `novelagent/trace/semantic_extraction.py`,
  `config/subagent_presets.yaml`, or `prompts/**/*.j2`, register ONE task before the first edit:
  `python scripts/prompt_history.py begin --session-id <session> --turn-id <turn> --reason "<requested change>"`.
  Use the exact IDs injected by the UserPromptSubmit Hook; never infer them from the latest stored task.
  If Hook context is unavailable, request Hook activation or use an explicit ungrouped backup command;
  do not invent a session/turn binding. Ordinary conversation must not create prompt tasks.
- If the backup repository is missing, run `python scripts/prompt_history.py init --reason "Initial prompt baseline"` first.
  Do not edit prompts if backup fails; report the problem and resolve it first.
- Trusted project Hooks silently snapshot before/after edits. They group multiple files and tool calls under
  the task bound to THIS session and turn only. Without registration, after-edit snapshots remain unassigned.
  `begin` can adopt only unassigned after-edit snapshots from this exact session/turn; pre-edit drift is a baseline.
- When the requirement is complete (including unsuccessful experiments), run:
  `python scripts/prompt_history.py finish --session-id <session> --turn-id <turn> --reason "<actual changes and why>"`.
  It saves changes before offline tests and adds a shared business summary/test result using Git notes,
  without rewriting snapshot commit IDs. Failed tests do not discard snapshots. Empty tasks are removed.
- If interrupted or awaiting input, leave the task active, report its ID, and do not pretend it is complete.
  To continue in a later turn, explicitly run `bind <task-id> --session-id <session> --turn-id <new-turn>`.
  Closed tasks and other sessions' tasks must never be reused. A turn end does not itself close a requirement.
- The Stop Hook reminds at most once per turn if prompt edits lack grouping or a completed summary.
  Respond to that reminder once for the whole requirement, not once per file. Do not create a separate LLM call.
- Use the `novelagent` virtual environment. Backup and offline checks must not call models, start the backend,
  write production memories, push repositories, or commit the source project automatically.
- `check` reports whether backup is needed; `list` and `compare <before> [after]` inspect saved versions.
  `tasks --session-id <session> --turn-id <turn>` shows exact-turn grouping and snapshot IDs.
  `restore <ref> [--file <relative path>]` only previews; use `--apply` only when the user requests restoration.
  Applied restoration first preserves current files and only writes the managed files shown in the preview.
- `branch codex/<experiment>` preserves current prompts and creates/switches a branch in the BACKUP repository only.
  It does not switch the source project or change its files. Experiments still use begin/finish.
- Python/YAML sources are backed up as whole files, including non-prompt fields. Always inspect restore diffs;
  do not overwrite unrelated code/config changes. The backup is not a filesystem watcher or a hard editing gate.
- Install project-local Hook definitions with `python scripts/install_prompt_history_hooks.py --apply`,
  then review/trust them in Codex. Never bypass trust automatically or change global Codex settings.
- If changing prompts embedded in another file, extend the script's managed file list and offline tests first.
  Real-model regression tests require separate user authorization; do not run `scripts/test_trace_prompt.py` automatically.

## 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

Test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---
These guidelines are working if:
- Fewer unnecessary changes in diffs
- Fewer rewrites due to overcomplication
- Clarifying questions come before implementation rather than after mistakes

## Architecture (7-layer backend)

```
web/ (React SSE) ←→ novelagent/server/ (FastAPI + SSE)
                          │
               novelagent/core/agent_loop.py  (ReAct loop engine)
              /           |            \              \
     tools/        security/        context/        memory/         llm/
     (layer4)      (layer5)         (layer6)        (layer7)    (client)
```

### Layer 4 — Tools (`novelagent/tools/`)

All tools extend `ToolProtocol` (`base.py`): `name`, `description`, `parameters` (JSON Schema), `execute()`, `checkPermissions()`. Registered in `ToolRegistry` (`registry.py`).

- **Read/Write/Edit/Glob/Grep** — File system tools. Write uses a 7-step permission decision (protected dirs, allow_rules, acceptEdits, etc.).
- **TextStats/CheckContent** — Read-only text statistics and batched content acceptance checks.
- **Bash** — Shell execution with 3-tier risk classification: ALLOW (read-only), ASK (redirects/git/rm/mv), BLOCKED (sudo, package managers, networking, destructive git). On Windows uses Git Bash if available.
- **SubAgent** — Spawns sub-agents defined in `config/subagent_presets.yaml`. Returns `subagent_spawn` result; actual execution delegated to `SubAgentRunner`.
- **AskUserQuestion/CreateTraceCheckpoint/SearchRag** — Interactive clarification, explicit Trace checkpoints, and shared reference-library retrieval.

### Layer 5 — Security (`novelagent/security/`)

Three-tier permission check in `PermissionChecker.check()`:
1. **1a** Static rules (`_check_static_rules`) — path validation for Read/Write/Edit
2. **1b** Risk estimation (`_estimate_risk`) — BashClassifier for Bash commands
3. **1c** Tool self-judgment (`tool.checkPermissions()`) — returns ALLOW/ASK/BLOCK/PASSTHROUGH

`PathValidator` enforces working-directory sandbox + system path blacklist. `BashClassifier` has separate ALLOW/ASK/BLOCKED command lists.

### Layer 6 — Context (`novelagent/context/`)

- `ContextBuilder.build()` assembles system prompt (Jinja2 from `prompts/`) + messages + memory injection
- `PromptManager` renders `prompts/base_system.j2` with includes from `partials/` and `dynamic/`
- `MessageManager` wraps a `Message` dataclass list with serialization
- `TokenCounter` uses tiktoken (`cl100k_base`); `needs_compression()` triggers at 80% of limit
- `build_cumulative_compression()` composes stored Memory Summaries with uncovered turns before continuing in a linked Trace

### Layer 7 — Memory and Trace (`novelagent/memory/`, `novelagent/trace/`)

- `MemoryManager` coordinates project/global memory initialization, relevant-memory prefetch, and `memory.md` index rebuilding
- `FileStore` reads/writes `.memory/` records; `IndexManager` maintains the top-200 active memory index
- `Prefetcher` makes a low-latency LLM call (configured by `memory_prefetch`) to select relevant memory files
- `TraceStore` persists immutable execution evidence in SQLite; background analysis derives `.insight/`, `.memory/`, and `.pattern/` records
- `normalized_trajectory.py` validates normalized trajectory records and binds each derived record to source Trace events
- The former `AutoMemory` path has been removed; Trace analysis now owns automatic knowledge extraction and promotion

### Core — Agent Loop (`novelagent/core/agent_loop.py`)

`AgentLoop.run()` flow:
1. Kick off async memory prefetch
2. Build context (system prompt + history + current user message)
3. Wait for prefetch (configured timeout, currently 1.5s), inject results
4. Before the ReAct loop, compose cumulative compression when the context reaches the configured threshold
5. ReAct loop: LLM call → parse tool use → security check → execute tool → append results → detect loops → repeat
6. Persist linked Trace events and schedule background Trace analysis without blocking the user response

Permission ASK flow: `PermissionResult.ASK` → yield `permission_ask` chunk → `await permission_event.wait()` → check `permission_granted`. The frontend POSTs to `/api/sessions/{id}/permission-response` to unblock.

SubAgent streaming: `SubAgentRunner.spawn_and_run()` runs a mini loop with its own tool set, yields chunks with `source: "subagent"` to the frontend, final result as `subagent_done`.

### LLM Client (`novelagent/llm/`)

`LLMClient` supports both Anthropic and OpenAI-compatible APIs (DeepSeek, MiniMax, OpenRouter, Azure, self-hosted). Provider routing via `LLMConfigLoader` reads `config/llm_config.yaml`; current positions are `main_loop`, `sub_agent`, `memory_prefetch`, `memory_summary_fallback`, `session_title`, and `case_analysis`. Sub-agents can further override settings via `sub_agent.overrides`.

### Storage (`novelagent/storage/`)

SQLite via aiosqlite. Two tables: `projects` (project_id, name, genre, word_count) and `sessions` (session_id, project_id, title, messages_json, token_count). Messages stored as JSON.

### Web Frontend (`web/`)

React 19 + TypeScript + Vite + Tailwind CSS 4. Key components:
- `App.tsx` — Main layout (sidebar + chat)
- `MessageBubble.tsx` — Markdown rendering (react-markdown + remark-gfm)
- `ToolCard.tsx` / `ToolResultCard.tsx` — Tool call/result display
- `PermissionDialog.tsx` — ASK permission modal
- `TokenBar.tsx` — Token usage indicator
- `useChat.ts` hook manages all state (projects, sessions, messages, SSE)

SSE event types: `text_delta`, `thinking`, `tool_call`, `tool_result`, `permission_ask`, `done`, `error`, `subagent_done`.

## Configuration

- `config/config.yaml` — Working dir, security rules, session/agent settings
- `config/llm_config.yaml` — Providers + position-specific model configs
- `config/subagent_presets.yaml` — Sub-agent definitions (system prompt, tools, max_turns, inherit_history)
- `.env` — API keys (ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, MINIMAX_API_KEY, etc.)
- `prompts/base_system.j2` — Main agent system prompt (Jinja2)

## Project structure conventions

- Project files live under `workspace/{project_id}/` with subdirs: `chapters/`, `project/characters/`, `world/`, `outline.md`
- Memory files under `workspace/{project_id}/.memory/` with type subdirs: `user/`, `feedback/`, `reference/`, `summary/`
- Each project gets a `project.yaml` with metadata

## Key patterns

- Tools execute relative to `ToolContext.working_dir` (resolved as `workspace/{project_id}`)
- Session messages are persisted to SQLite at the end of each ReAct turn
- `accept_edits` mode allows Write/Edit tools to skip ASK permission
- Previously-allowed operations are remembered per-session (hash-based keys)
- Loop detection: 3 consecutive identical tool call sets → abort
