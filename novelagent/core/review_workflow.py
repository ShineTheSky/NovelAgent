"""Program-owned chapter review and polish pipeline.

The coordinator does not relay the novel body.  A completed writer revision
causes this module to construct the identical leading artifact block for the
reviewer and polisher, then it runs the two specialist agents in order.
"""

import re
from pathlib import Path

from novelagent.core.session import ResponseChunk
from novelagent.trace.file_lifecycle import FileLifecycleStore


_CHAPTER_PATH = re.compile(r"^chapters/content_(\d+)\.(\d+)\.(\d+)\.md$", re.IGNORECASE)


class ReviewPolishWorkflow:
    def __init__(self, runner, working_dir: str):
        self.runner = runner
        self.working_dir = Path(working_dir)

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
        return path.read_text(encoding="utf-8") if path.is_file() else "（该文件尚不存在）"

    def _reference_feedback_context(self, project_id: str, chapter_path: str, body: str) -> str:
        records = FileLifecycleStore(str(self.working_dir), project_id).text_feedback_for_artifact(chapter_path, body)
        if not records:
            return ""
        lines = ["## 当前正文的用户修改反馈（按需遵守）"]
        for record in records:
            lines.append(f"### {record.get('title', record.get('claim', '文本反馈'))}\n{record.get('content', '')}")
        lines.append("若反馈互相矛盾、引用已无法定位，或修改方向仍不明确，先用 AskUserQuestion 澄清；否则据此直接审阅或润色。")
        return "\n\n".join(lines)

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
        memory = self._read_if_exists(project_dir, ".memory/memory.md")
        feedback = self._reference_feedback_context(project_id, normalized, body)
        extra = f"## 项目记忆（按需遵守）\n{memory}"
        return context, f"{extra}\n\n{feedback}" if feedback else extra

    def build_context(self, project_id: str, chapter_path: str) -> tuple[str, str]:
        """Expose the same deterministic context order to manual review/polish calls."""
        return self._artifact_context(project_id, chapter_path)

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
            return "", self._read_if_exists(project_dir, ".memory/memory.md")
        previous_path = f"chapters/content_{int(volume)}.{int(chapter)}.{prior}.md"
        previous = project_dir / previous_path
        if not previous.is_file():
            return "", self._read_if_exists(project_dir, ".memory/memory.md")
        volume_outline = f"outlines/outline_{int(volume)}.0.0.md"
        chapter_outline = f"outlines/outline_{int(volume)}.{int(chapter)}.0.md"
        context = "\n\n".join([
            "## 固定工件上下文（禁止重新读取或改写此区块）",
            f"### 前序最新正文：{previous_path}\n{previous.read_text(encoding='utf-8')}",
            f"### 对应卷纲：{volume_outline}\n{self._read_if_exists(project_dir, volume_outline)}",
            f"### 对应章纲：{chapter_outline}\n{self._read_if_exists(project_dir, chapter_outline)}",
        ])
        return context, f"## 项目记忆（按需遵守）\n{self._read_if_exists(project_dir, '.memory/memory.md')}"

    async def run(self, parent_session, chapter_path: str, writer_task: str, operation_id: str, parent_run_id: str = ""):
        artifact_context, memory_context = self._artifact_context(parent_session.project_id, chapter_path)
        reviewer_task = (
            f"审阅 `{chapter_path}`。这是写作 Agent 刚提交的版本；只输出可执行的结构化审阅报告，"
            "不要写文件。请按“问题、证据、严重度、建议”列出；没有问题也明确说明。\n\n"
            f"## 本轮写作需求（用于判断是否满足用户目标）\n{writer_task}"
        )
        reviewer_result = ""
        reviewer_completed = False
        reviewer_empty = False
        async for chunk in self.runner.spawn_and_run(
            "reviewer", reviewer_task, parent_session, False,
            extra_context=memory_context, artifact_context=artifact_context,
            actor="reviewer", operation_id=f"{operation_id}:review",
            parent_run_id=parent_run_id, workflow="auto_review",
            context_as_user_message=True,
        ):
            if chunk.type == "subagent_done":
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

        # Re-read after review so the second specialist receives the actual latest header/body.
        artifact_context, memory_context = self._artifact_context(parent_session.project_id, chapter_path)
        polisher_task = (
            f"根据以下审阅报告润色 `{chapter_path}`。必须只修改该章节；使用固定正文中的当前修订作为 "
            "expected_revision_id。完成写入后简述修改内容。\n\n"
            f"## 审阅报告\n{reviewer_result}\n\n## 原写作任务（仅作意图参考）\n{writer_task}"
        )
        polisher_result = ""
        polisher_completed = False
        polisher_empty = False
        async for chunk in self.runner.spawn_and_run(
            "chapter_polisher", polisher_task, parent_session, False,
            extra_context=memory_context, artifact_context=artifact_context,
            actor="polisher", operation_id=f"{operation_id}:polish",
            parent_run_id=parent_run_id, workflow="auto_polish",
        ):
            if chunk.type == "subagent_done":
                polisher_result = chunk.data.get("result", "")
                polisher_empty = bool(chunk.data.get("empty_result")) or not polisher_result.strip()
                polisher_completed = not polisher_empty and not polisher_result.startswith("Error:")
            yield ResponseChunk(type=chunk.type, data={**chunk.data, "workflow": "auto_polish"})

        if not polisher_completed:
            yield ResponseChunk(type="error", data={
                "message": "自动润色未返回结果；章节保留在润色前版本或已写入版本，需人工核查。",
                "workflow": "auto_polish",
                "failure_kind": "empty_response" if polisher_empty else "subagent_failure",
            })
