import asyncio
from pathlib import Path

from novelagent.core.review_workflow import ReviewPolishWorkflow
from novelagent.core.session import ResponseChunk, Session


class _Runner:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def spawn_and_run(self, preset, *_args, **_kwargs):
        self.calls.append(preset)
        yield ResponseChunk(type="subagent_done", data=self.results[preset])


async def _collect(generator):
    return [chunk async for chunk in generator]


def _workflow(tmp_path: Path, results: dict):
    project = tmp_path / "project-a"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "content_1.1.1.md").write_text("# 第1章\n正文", encoding="utf-8")
    runner = _Runner(results)
    session = Session(session_id="session-a", project_id="project-a")
    return runner, ReviewPolishWorkflow(runner, str(tmp_path)), session


def test_empty_reviewer_stops_polisher_and_marks_empty_response(tmp_path):
    runner, workflow, session = _workflow(tmp_path, {
        "reviewer": {"result": "(子Agent未返回内容)", "empty_result": True},
    })

    chunks = asyncio.run(_collect(workflow.run(session, "chapters/content_1.1.1.md", "写作任务", "op-a")))

    assert runner.calls == ["reviewer"]
    assert chunks[-1].type == "error"
    assert chunks[-1].data["failure_kind"] == "empty_response"


def test_empty_polisher_marks_empty_response(tmp_path):
    runner, workflow, session = _workflow(tmp_path, {
        "reviewer": {"result": "未发现阻断问题。", "empty_result": False},
        "chapter_polisher": {"result": "(子Agent未返回内容)", "empty_result": True},
    })

    chunks = asyncio.run(_collect(workflow.run(session, "chapters/content_1.1.1.md", "写作任务", "op-a")))

    assert runner.calls == ["reviewer", "chapter_polisher"]
    assert chunks[-1].type == "error"
    assert chunks[-1].data["failure_kind"] == "empty_response"
