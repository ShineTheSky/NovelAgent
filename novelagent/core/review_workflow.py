"""Program-owned chapter review and polish pipeline.

The reviewer forks the writer's finished model messages and receives its phase
instructions at the end. Missing latest artifacts are appended, not replayed.
The polisher continues the completed review messages. Memory extraction continues
the completed polish messages after their History has been durably finalized.
"""

import re
import json
from pathlib import Path

from novelagent.core.session import ResponseChunk
from novelagent.trace.file_lifecycle import FileLifecycleStore
from novelagent.history import DocumentHistoryStore
from novelagent.versioning import RevisionManager
from novelagent.trace.agent_run import AgentTraceTaskManager


_CHAPTER_PATH = re.compile(r"^chapters/content_(\d+)\.(\d+)\.(\d+)\.md$", re.IGNORECASE)


class ReviewPolishWorkflow:
    def __init__(self, runner, working_dir: str, history_analyzer=None, background_tasks=None):
        self.runner = runner
        self.working_dir = Path(working_dir)
        self.history_analyzer = history_analyzer
        self.background_tasks = background_tasks or getattr(runner, "trace_tasks", None) or AgentTraceTaskManager()

    async def _analyze_review(self, parent_session, review_history_id: str, source_trace_id: str):
        try:
            await self.history_analyzer.analyze(parent_session.project_id, review_history_id)
        except Exception as exc:
            await self._capture_failure(parent_session, review_history_id, source_trace_id,
                                        exc, "history_analysis_failure")
            raise

    async def _capture_failure(self, session, review_id: str, trace_id: str, error: str, kind: str):
        if review_id and kind != "history_analysis_failure":
            DocumentHistoryStore(self.working_dir / session.project_id).set_analysis_status(review_id, "deferred", error)
        recorder = getattr(self.runner, "bad_cases", None)
        if recorder:
            await recorder.capture(
                source_trace_id=trace_id or review_id,
                session_id=session.session_id, project_id=session.project_id,
                failure_kind=kind, actor="history:reviewer", error=error,
                tool="HistoryAnalysis", params={"history_id": review_id},
            )

    @staticmethod
    def is_chapter(path: str) -> bool:
        return bool(_CHAPTER_PATH.match(path.replace("\\", "/")))

    @staticmethod
    def find_chapter_path(task: str, attachments: list[dict] | None = None) -> str | None:
        for attachment in attachments or []:
            if isinstance(attachment, dict) and ReviewPolishWorkflow.is_chapter(attachment.get("path", "")):
                return attachment["path"].replace("\\", "/")
        match = re.search(r"chapters/content_(\d+)\.(\d+)\.(\d+)\.md", task, re.IGNORECASE)
        if match:
            return "chapters/content_{}.{}.{}.md".format(*match.groups())
        numbered = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", task)
        return f"chapters/content_{'.'.join(numbered.groups())}.md" if numbered else None

    def _read_if_exists(self, project_dir: Path, relative_path: str) -> str:
        path = project_dir / relative_path
        if path.suffix.lower() == ".md" and ".memory" not in path.parts:
            DocumentHistoryStore(project_dir).recover_file(path)
        if not path.is_file():
            return "（该文件尚不存在）"
        if path.suffix.lower() == ".md" and ".memory" not in path.parts:
            DocumentHistoryStore(project_dir).ensure_baseline(path)
        return path.read_text(encoding="utf-8")

    def _reference_feedback_context(self, project_id: str, chapter_path: str, body: str) -> str:
        records = FileLifecycleStore(str(self.working_dir), project_id).text_feedback_for_artifact(chapter_path, body)
        if not records:
            return ""
        lines = ["## 当前正文的用户修改反馈（按需遵守）"]
        for record in records:
            lines.append(f"### {record.get('title', record.get('claim', '文本反馈'))}\n{record.get('content', '')}")
        lines.append("若反馈互相矛盾、引用已无法定位，或修改方向仍不明确，先用 AskUserQuestion 澄清；否则据此直接审阅或润色。")
        return "\n\n".join(lines)

    def _memory_context(self, project_id: str) -> str:
        knowledge = getattr(self.runner, "knowledge", None)
        if knowledge:
            return knowledge.index(project_id)
        project_dir = self.working_dir / project_id
        store = FileLifecycleStore(str(self.working_dir), project_id)
        parts = [f"## 项目记忆（按需遵守）\n{self._read_if_exists(project_dir, '.memory/memory.md')}"]
        recurring_issues = store.review_issue_context()
        if recurring_issues:
            parts.append(recurring_issues)
        return "\n\n".join(parts)

    def _artifact_context(self, project_id: str, chapter_path: str) -> tuple[str, str]:
        normalized = chapter_path.replace("\\", "/")
        match = _CHAPTER_PATH.match(normalized)
        if not match:
            raise ValueError(f"不是章节正文路径: {chapter_path}")
        volume, chapter, _ = match.groups()
        project_dir = self.working_dir / project_id
        volume_outline = f"outlines/outline_{int(volume)}.0.0.md"
        chapter_outline = f"outlines/outline_{int(volume)}.{int(chapter)}.0.md"
        body = self._read_if_exists(project_dir, normalized)
        context = "\n\n".join([
            "## 固定工件上下文（禁止重新读取或改写此区块）",
            f"### 最新版本正文：{normalized}\n{body}",
            f"### 对应卷纲：{volume_outline}\n{self._read_if_exists(project_dir, volume_outline)}",
            f"### 对应章纲：{chapter_outline}\n{self._read_if_exists(project_dir, chapter_outline)}",
        ])
        feedback = self._reference_feedback_context(project_id, normalized, body)
        extra = self._memory_context(project_id)
        return context, f"{extra}\n\n{feedback}" if feedback else extra

    def build_context(self, project_id: str, chapter_path: str) -> tuple[str, str]:
        """Expose the same deterministic context order to manual review/polish calls."""
        return self._artifact_context(project_id, chapter_path)

    @staticmethod
    def _missing_context(context: str, fork_context: list[dict]) -> str:
        """Keep the fork prefix intact; append only artifact blocks absent from it."""
        contents = [str(message.get("content") or "") for message in fork_context]
        for message in fork_context:
            for call in message.get("tool_calls", []):
                try:
                    args = json.loads(call.get("function", {}).get("arguments", "{}"))
                except (ValueError, TypeError):
                    continue
                if isinstance(args, dict) and isinstance(args.get("content"), str):
                    contents.append(args["content"])
        blocks = re.split(r"(?=^### )", context, flags=re.MULTILINE)
        missing = []
        for block in blocks:
            heading, separator, body = block.partition("\n")
            info = RevisionManager.parse(body)
            if separator and not info.legacy and info.body.strip() and any(info.body.strip() in content for content in contents):
                missing.append(f"{heading}\n当前修订：{info.revision_id}（正文已在继承的上下文中，不重复附带）")
                continue
            if separator and body.strip() and not any(body.strip() in content for content in contents):
                missing.append(block.strip())
        return "\n\n".join(missing)

    def dependency_versions(self, project_id: str, chapter_path: str) -> dict[str, str]:
        match = _CHAPTER_PATH.match(chapter_path.replace("\\", "/"))
        if not match:
            return {}
        volume, chapter, _ = map(int, match.groups())
        paths = (f"outlines/outline_{volume}.0.0.md", f"outlines/outline_{volume}.{chapter}.0.md")
        result = {}
        history = DocumentHistoryStore(self.working_dir / project_id)
        for relative in paths:
            path = self.working_dir / project_id / relative
            history.recover_file(path)
            if path.is_file():
                result[relative] = history.ensure_baseline(path)
        return result

    def build_writer_context(self, project_id: str, chapter_path: str) -> tuple[str, str]:
        """Give the writer the current target, or its most recent sibling when creating one."""
        project_dir = self.working_dir / project_id
        normalized = chapter_path.replace("\\", "/")
        match = _CHAPTER_PATH.match(normalized)
        if not match:
            raise ValueError(f"不是章节正文路径: {chapter_path}")
        volume, chapter, section = match.groups()
        target = project_dir / normalized
        if target.is_file():
            return self._artifact_context(project_id, normalized)

        prior = int(section) - 1
        if prior <= 0:
            return "", self._memory_context(project_id)
        previous_path = f"chapters/content_{int(volume)}.{int(chapter)}.{prior}.md"
        previous = project_dir / previous_path
        if not previous.is_file():
            return "", self._memory_context(project_id)
        volume_outline = f"outlines/outline_{int(volume)}.0.0.md"
        chapter_outline = f"outlines/outline_{int(volume)}.{int(chapter)}.0.md"
        context = "\n\n".join([
            "## 固定工件上下文（禁止重新读取或改写此区块）",
            f"### 前序最新正文：{previous_path}\n{self._read_if_exists(project_dir, previous_path)}",
            f"### 对应卷纲：{volume_outline}\n{self._read_if_exists(project_dir, volume_outline)}",
            f"### 对应章纲：{chapter_outline}\n{self._read_if_exists(project_dir, chapter_outline)}",
        ])
        return context, self._memory_context(project_id)

    async def run(self, parent_session, chapter_path: str, writer_task: str, operation_id: str,
                  parent_run_id: str = "", source_trace_id: str = "",
                  history_evidence: dict | None = None, fork_context: list[dict] | None = None):
        artifact_context, memory_context = self._artifact_context(parent_session.project_id, chapter_path)
        dependencies = self.dependency_versions(parent_session.project_id, chapter_path)
        reviewer_task = (
            f"审阅 `{chapter_path}`。这是写作 Agent 刚提交的版本；只输出可执行的结构化审阅报告，"
            "不要写文件。必须分别输出 `## 用户需求验收` 和 `## 常规写作检查`，"
            "分别检查明确需求落实和其他常规问题；问题需附证据、严重度、建议，没有问题也明确说明。"
        )
        if not fork_context:
            reviewer_task += f"\n\n## 本轮写作需求（主 Agent 任务传递）\n{writer_task}"
        elif not any(writer_task in str(m.get("content") or "") for m in fork_context):
            reviewer_task += f"\n\n## 本轮写作需求（主 Agent 任务传递）\n{writer_task}"
        raw_requirement = str((history_evidence or {}).get("user_input") or "")
        if raw_requirement and (not fork_context or not any(raw_requirement in str(m.get("content") or "") for m in fork_context)):
            reviewer_task += f"\n\n## 用户需求原文与澄清回答\n{raw_requirement}"
        reviewer_artifact = self._missing_context(artifact_context, fork_context) if fork_context else artifact_context
        reviewer_memory = self._missing_context(memory_context, fork_context) if fork_context else memory_context
        reviewer_result = ""
        reviewer_completed = False
        reviewer_empty = False
        reviewer_thinking = ""
        review_history_id = ""
        reviewer_user_input = str((history_evidence or {}).get("user_input") or "")
        reviewer_user_answers = list((history_evidence or {}).get("user_answers") or [])
        requirement_event_ids = list((history_evidence or {}).get("requirement_event_ids") or [])
        reviewer_context = []
        async for chunk in self.runner.spawn_and_run(
            "reviewer", reviewer_task, parent_session, False,
            extra_context=reviewer_memory, artifact_context=reviewer_artifact,
            actor="reviewer", operation_id=f"{operation_id}:review",
            parent_run_id=parent_run_id, workflow="auto_review",
            context_as_user_message=True,
            fork_context=fork_context,
            context_snapshot=reviewer_context,
            source_trace_id=source_trace_id,
            history_evidence={**(history_evidence or {}), "dependencies": dependencies,
                              "target_path": chapter_path},
        ):
            if chunk.type == "subagent_done":
                reviewer_thinking = chunk.data.get("history_thinking", "")
                review_history_id = chunk.data.get("review_history_id", "")
                reviewer_user_input = chunk.data.get("history_user_input", reviewer_user_input)
                reviewer_user_answers = chunk.data.get("history_user_answers", reviewer_user_answers)
                requirement_event_ids = chunk.data.get("requirement_event_ids", requirement_event_ids)
                reviewer_result = chunk.data.get("result", "")
                reviewer_empty = bool(chunk.data.get("empty_result")) or not reviewer_result.strip()
                reviewer_completed = not reviewer_empty and not reviewer_result.startswith("Error:")
            yield ResponseChunk(type=chunk.type, data={**chunk.data, "workflow": "auto_review"})

        if not reviewer_completed:
            yield ResponseChunk(type="error", data={
                "message": "自动审阅未完成，已停止自动润色；原章节保持在写作版本。",
                "workflow": "auto_review",
                "failure_kind": "empty_response" if reviewer_empty else "subagent_failure",
            })
            return

        if not reviewer_context:
            await self._capture_failure(parent_session, review_history_id, source_trace_id,
                                        "审阅完整上下文缺失", "history_context_failure")
            yield ResponseChunk(type="error", data={
                "message": "审阅完整上下文缺失，已停止自动润色，不能以报告替代上下文。",
                "workflow": "auto_review", "failure_kind": "missing_context",
            })
            return

        # Re-read after review so the second specialist receives the actual latest header/body.
        artifact_context, memory_context = self._artifact_context(parent_session.project_id, chapter_path)
        dependencies = self.dependency_versions(parent_session.project_id, chapter_path)
        polisher_task = (
            f"审阅阶段已完成，现在落实以上审阅思考与最终报告，润色 `{chapter_path}`。"
            "不要重新生成审阅报告，也不要继续原写作任务。必须只修改该章节；"
            "使用下方最新工件的当前修订作为 expected_revision_id。"
            "完成写入后简述修改内容；确实无需修改时，以‘无需修改：’开头说明理由。"
        )
        polisher_artifact = self._missing_context(artifact_context, reviewer_context)
        polisher_memory = self._missing_context(memory_context, reviewer_context)
        polisher_result = ""
        polisher_completed = False
        polisher_empty = False
        polisher_context = []
        polish_request_id = ""
        history_error = ""
        async for chunk in self.runner.spawn_and_run(
            "chapter_polisher", polisher_task, parent_session, False,
            extra_context=polisher_memory, artifact_context=polisher_artifact,
            actor="polisher", operation_id=f"{operation_id}:polish",
            parent_run_id=parent_run_id, workflow="auto_polish",
            context_as_user_message=True, fork_context=reviewer_context,
            context_snapshot=polisher_context,
            source_trace_id=source_trace_id,
            history_evidence={
                **(history_evidence or {}), "main_delegation": writer_task,
                "user_input": reviewer_user_input,
                "user_answers": reviewer_user_answers,
                "requirement_event_ids": requirement_event_ids,
                "reviewer_thinking": reviewer_thinking,
                "reviewer_output": reviewer_result,
                "reviewer_delegation": polisher_task,
                "review_history_id": review_history_id,
                "dependencies": dependencies,
                "target_path": chapter_path,
            },
        ):
            if chunk.type == "subagent_done":
                polish_request_id = chunk.data.get("history_request_id", "")
                polisher_result = chunk.data.get("result", "")
                polisher_empty = bool(chunk.data.get("empty_result")) or not polisher_result.strip()
                polisher_completed = not polisher_empty and not polisher_result.startswith("Error:")
                if polisher_completed and review_history_id:
                    store = DocumentHistoryStore(self.working_dir / parent_session.project_id)
                    try:
                        store.complete_review_workflow(review_history_id, polish_request_id, polisher_context)
                    except ValueError as exc:
                        history_error = str(exc)
                        await self._capture_failure(parent_session, review_history_id, source_trace_id,
                                                    history_error, "history_completion_failure")
                    if not history_error and self.history_analyzer:
                        self.background_tasks.submit(
                            self._analyze_review(parent_session, review_history_id, source_trace_id),
                            label=f"history-review:{review_history_id}",
                        )
                chunk.data["history_complete"] = polisher_completed and not history_error
            yield ResponseChunk(type=chunk.type, data={**chunk.data, "workflow": "auto_polish"})

        if not polisher_completed:
            yield ResponseChunk(type="error", data={
                "message": "自动润色未返回结果；章节保留在润色前版本或已写入版本，需人工核查。",
                "workflow": "auto_polish",
                "failure_kind": "empty_response" if polisher_empty else "subagent_failure",
            })
            return

        if history_error:
            yield ResponseChunk(type="error", data={
                "message": f"润色结果缺少完整 History 证据，未触发审阅记忆提取：{history_error}",
                "workflow": "auto_polish", "failure_kind": "incomplete_history",
            })
            return
