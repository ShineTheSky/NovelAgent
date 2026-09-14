import json
from pathlib import Path

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
