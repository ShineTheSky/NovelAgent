"""The file History survives writes, interrupted materialization and later feedback."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novelagent.history import DocumentHistoryStore, HistoryConflict
from novelagent.core.review_workflow import ReviewPolishWorkflow
from novelagent.core.session import ResponseChunk, Session
from novelagent.tools.base import ToolContext
from novelagent.tools.edit import EditTool
from novelagent.tools.read import ReadTool
from novelagent.tools.record_no_change import RecordNoChangeTool
from novelagent.tools.write import WriteTool
from novelagent.versioning import revision_manager


class DocumentHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.history = DocumentHistoryStore(self.project)
        self.context = ToolContext(
            session_id="session", project_id="project", working_dir=str(self.project),
            actor="writer", operation_id="operation",
            history_evidence={
                "user_input": "请写第一节。", "main_delegation": "写 chapters/content_1.1.1.md",
                "writer_thinking": "先确立人物动机。", "dependencies": {},
            },
        )

    async def _write(self, text, expected="new"):
        return await WriteTool().execute({
            "path": "chapters/content_1.1.1.md", "content": text,
            "expected_revision_id": expected,
        }, self.context)

    async def test_versions_dependencies_evidence_and_no_change(self):
        outline = self.project / "outlines" / "outline_1.0.0.md"
        outline.parent.mkdir(parents=True)
        dep = await revision_manager.commit(
            outline, "卷纲", "new", actor="writer", operation_id="outline",
            history_store=self.history,
        )
        read = await ReadTool().execute({"path": "outlines/outline_1.0.0.md"}, self.context)
        self.assertTrue(read.success)
        self.assertEqual(self.context.history_evidence["dependencies"]["outlines/outline_1.0.0.md"], dep.revision_id)

        first = await self._write("初稿")
        self.assertTrue(first.success, first.error)
        rev1 = self.context.revision_events[-1]["revision_id"]
        stored1 = self.history.get_revision(rev1)
        self.assertEqual(stored1["version_no"], 1)
        self.assertEqual(stored1["body_md"], "初稿")
        self.assertEqual(stored1["dependencies"]["outlines/outline_1.0.0.md"], dep.revision_id)
        self.assertEqual({row["kind"]: row["content"] for row in stored1["evidence"]}["main_delegation"],
                         "写 chapters/content_1.1.1.md")

        self.context.actor = "polisher"
        self.context.history_evidence.update({
            "reviewer_thinking": "人物动机不够清楚。", "reviewer_output": "补足动机",
            "reviewer_delegation": "按报告润色", "polisher_thinking": "增加动作证据。",
        })
        second = await self._write("润色稿", rev1)
        self.assertTrue(second.success, second.error)
        rev2 = self.context.revision_events[-1]["revision_id"]
        stored2 = self.history.get_revision(rev2)
        self.assertEqual((stored2["version_no"], stored2["parent_revision_id"]), (2, rev1))
        self.assertIn("reviewer_output", {row["kind"] for row in stored2["evidence"]})
        self.history.record_no_change(rev2, "这句需再检查", "确认原句已满足要求，无需修改")
        self.assertEqual(self.history.latest("chapters/content_1.1.1.md")["revision_id"], rev2)

        third = await EditTool().execute({
            "path": "chapters/content_1.1.1.md", "old_string": "润色", "new_string": "最终",
            "expected_revision_id": rev2,
        }, self.context)
        self.assertTrue(third.success, third.error)
        rev3 = self.context.revision_events[-1]["revision_id"]
        self.assertEqual(self.history.get_revision(rev3)["body_md"], "最终稿")
        self.assertEqual(self.history.get_revision(rev2)["body_md"], "润色稿")
        self.assertEqual(self.history.get_version("chapters/content_1.1.1.md", 2)["revision_id"], rev2)
        self.assertEqual([row["version_no"] for row in self.history.list_versions("chapters/content_1.1.1.md")], [1, 2, 3])

    async def test_recover_interrupted_write_and_preserve_external_edits(self):
        result = await self._write("第一版")
        self.assertTrue(result.success, result.error)
        path = self.project / "chapters" / "content_1.1.1.md"
        first_rendered = path.read_text(encoding="utf-8")
        first_id = self.context.revision_events[-1]["revision_id"]
        self.history.record_revision(
            path, "revision_after_crash", first_id, "第二版",
            revision_manager.render({"revision_id": "revision_after_crash"}, "第二版"),
            previous_body="第一版", previous_rendered=first_rendered,
        )
        self.history.recover_file(path)
        self.assertEqual(revision_manager.parse(path.read_text(encoding="utf-8")).body, "第二版")
        path.write_text("用户外部修改", encoding="utf-8")
        with self.assertRaises(HistoryConflict):
            self.history.recover_file(path)
        self.assertEqual(path.read_text(encoding="utf-8"), "用户外部修改")

    async def test_existing_file_is_imported_only_as_baseline(self):
        path = self.project / "chapters" / "content_1.1.1.md"
        path.parent.mkdir(parents=True)
        path.write_text("旧稿", encoding="utf-8")
        baseline = revision_manager.parse("旧稿").revision_id
        result = await self._write("新稿", baseline)
        self.assertTrue(result.success, result.error)
        new_id = self.context.revision_events[-1]["revision_id"]
        self.assertEqual(self.history.get_revision(baseline)["body_md"], "旧稿")
        self.assertEqual(self.history.get_revision(new_id)["parent_revision_id"], baseline)

    async def test_history_failure_never_reports_success(self):
        path = self.project / "chapters" / "content_1.1.1.md"
        with patch.object(DocumentHistoryStore, "record_revision", side_effect=OSError("history unavailable")):
            result = await self._write("不能写入")
        self.assertFalse(result.success)
        self.assertFalse(path.exists())
        self.assertFalse(self.context.revision_events)

    async def test_file_replacement_failure_is_recovered_from_committed_history(self):
        path = self.project / "chapters" / "content_1.1.1.md"
        with patch("novelagent.versioning.os.replace", side_effect=OSError("replace interrupted")):
            result = await self._write("提交后的正文")
        self.assertFalse(result.success)
        self.assertFalse(path.exists())
        latest = self.history.latest("chapters/content_1.1.1.md")
        self.assertEqual(latest["body_md"], "提交后的正文")
        self.history.recover_file(path)
        self.assertEqual(revision_manager.parse(path.read_text(encoding="utf-8")).body, "提交后的正文")

    async def test_expected_revision_conflict_preserves_latest(self):
        first = await self._write("第一版")
        self.assertTrue(first.success)
        result = await self._write("过期覆盖", "wrong-revision")
        self.assertFalse(result.success)
        latest = self.history.latest("chapters/content_1.1.1.md")
        self.assertEqual(latest["body_md"], "第一版")

    async def test_identical_write_does_not_create_revision(self):
        first = await self._write("同一正文")
        self.assertTrue(first.success)
        revision_id = self.context.revision_events[-1]["revision_id"]
        second = await self._write("同一正文", revision_id)
        self.assertTrue(second.success)
        self.assertEqual(len(self.context.revision_events), 1)
        self.assertEqual(len(self.history.list_versions("chapters/content_1.1.1.md")), 1)

    async def test_explicit_no_change_tool_attaches_to_current_revision(self):
        self.assertTrue((await self._write("正文")).success)
        revision_id = self.context.revision_events[-1]["revision_id"]
        result = await RecordNoChangeTool().execute({
            "path": "chapters/content_1.1.1.md", "expected_revision_id": revision_id,
            "opinion": "这段需要更克制吗？", "rationale": "无需修改：现有动作已经足够克制。",
        }, self.context)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(self.history.get_revision(revision_id)["no_change"]), 1)
        self.assertEqual(len(self.history.list_versions("chapters/content_1.1.1.md")), 1)

    async def test_quoted_feedback_finds_original_revision_without_trace(self):
        body = "他站在门口，犹豫了很久才开口。"
        result = await self._write(body)
        self.assertTrue(result.success)
        revision_id = self.context.revision_events[-1]["revision_id"]
        events = [{"event_type": "user_message", "payload": {
            "content": f"‘{body}’这段太直白，请改得克制一些。",
        }}]
        related = self.history.related_to_events(events)
        self.assertEqual([item["revision_id"] for item in related], [revision_id])

    async def test_review_evidence_is_forwarded_to_polish_version(self):
        self.assertTrue((await self._write("正文")).success)
        class Runner:
            def __init__(self):
                self.calls = []

            async def spawn_and_run(self, preset, task, *args, **kwargs):
                self.calls.append((preset, task, kwargs))
                if preset == "reviewer":
                    yield ResponseChunk(type="subagent_done", data={
                        "result": "人物动机不明", "history_thinking": "检查了动机链",
                        "history_user_input": "用户原话\n\n澄清后的完整选项",
                    })
                else:
                    yield ResponseChunk(type="subagent_done", data={"result": "已润色"})

        runner = Runner()
        workflow = ReviewPolishWorkflow(runner, str(self.project.parent))
        session = Session(session_id="session", project_id=self.project.name)
        chunks = [chunk async for chunk in workflow.run(
            session, "chapters/content_1.1.1.md", "写作原任务", "operation",
            history_evidence={"user_input": "用户原话"},
        )]
        self.assertEqual([call[0] for call in runner.calls], ["reviewer", "chapter_polisher"])
        evidence = runner.calls[1][2]["history_evidence"]
        self.assertEqual(evidence["reviewer_thinking"], "检查了动机链")
        self.assertEqual(evidence["reviewer_output"], "人物动机不明")
        self.assertEqual(evidence["reviewer_delegation"], runner.calls[1][1])
        self.assertIn("澄清后的完整选项", evidence["user_input"])
        self.assertEqual(chunks[-1].type, "subagent_done")


if __name__ == "__main__":
    unittest.main()
