import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer
from novelagent.trace.bash_case_analyzer import BashCaseAnalyzer


def test_case_annotation_is_saved_and_loaded_by_semantic_family(tmp_path):
    analyzer = BashCaseAnalyzer(None, str(tmp_path / "workspace"))
    payload = {
        "id": "bash_case_1",
        "created_at": "2026-09-14T00:00:00+00:00",
        "command_family": "ls",
    }
    case_path = Path(analyzer.directory) / "2026-09-14" / "bash_case_1.json"
    case_path.parent.mkdir(parents=True)
    case_path.write_text(json.dumps(payload), encoding="utf-8")

    analyzer._update_case(payload, {
        "purpose": "检查章节版本和关键词是否已写入。",
        "semantic_family": "chapter_content_verification",
        "suggested_tool": "Read",
        "reason": "只读取章节内容。",
        "confidence": 0.92,
    })

    stored = json.loads(case_path.read_text(encoding="utf-8"))
    assert stored["purpose_analysis"]["suggested_tool"] == "Read"
    assert analyzer._load_cases("chapter_content_verification") == [stored]


def test_semantic_family_is_safe_for_aggregation_file_names():
    assert BashCaseAnalyzer._semantic_family("Chapter Check / v2", "ls") == "chapter_check_v2"
    assert BashCaseAnalyzer._semantic_family("", "python3") == "python3"


def test_bad_and_bash_case_analysis_share_one_llm_position(tmp_path):
    class FakeLLM:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tag") == ":bash-case-annotation":
                content = json.dumps({
                    "purpose": "读取文件", "semantic_family": "file_read",
                    "suggested_tool": "Read", "reason": "专用读取", "confidence": 0.9,
                }, ensure_ascii=False)
            else:
                content = json.dumps({
                    "summary": "共同原因", "root_causes": [], "recommendations": [],
                    "needs_more_evidence": True,
                }, ensure_ascii=False)
            yield SimpleNamespace(type="text_delta", content=content)

    async def scenario():
        llm = FakeLLM()
        bash = BashCaseAnalyzer(llm, str(tmp_path / "workspace"))
        annotation = await bash._annotate_case({
            "id": "bash-1", "command": "cat outline.md",
            "session_id": "session-a", "project_id": "project-a",
        })
        assert annotation["semantic_family"] == "file_read"

        bad = BadCaseAnalyzer(llm, str(tmp_path / "workspace"))
        await bad._analyze("test", [{"id": "case-1", "error": "boom"}])
        assert {call["position"] for call in llm.calls} == {"case_analysis"}

    asyncio.run(scenario())
