from novelagent.trace.semantic_extraction import (
    PROFILES,
    build_semantic_extraction_section,
    normalize_analysis_semantics,
    normalize_record_semantic,
    select_extraction_profiles,
)


def _names(profiles):
    return [profile.name for profile in profiles]


def test_mixed_window_enables_review_text_and_story_profiles():
    profiles = select_extraction_profiles([
        {
            "type": "workflow_subagent_done",
            "preset": "reviewer",
            "workflow": "auto_review",
            "result": "这段正文的连续对白缺少动作支点，人物关系也没有推进。",
        },
        {
            "type": "tool_call",
            "tool": "Edit",
            "params": {"path": "chapters/content_1.1.1.md"},
        },
    ])

    assert _names(profiles) == [
        "story_knowledge", "text_feedback", "review_finding", "project_decision",
    ]


def test_user_answer_and_setting_checkpoint_enable_two_independent_missions():
    profiles = select_extraction_profiles(
        [{"type": "user_answer", "answers": {"性格": "改成恐惧回避型"}}],
        [{"event_id": "checkpoint-1", "reason": "确认角色初始性格设定"}],
    )

    assert _names(profiles) == ["user_directive", "story_knowledge", "project_decision"]


def test_prompt_exposes_common_fact_shape_and_only_selected_attributes():
    profiles = select_extraction_profiles([
        {"type": "user_message", "content": "以后不要用直白解释，动作要有留白。"},
    ])

    prompt = build_semantic_extraction_section(profiles)

    assert 'scenario="user_directive"' in prompt
    assert 'scenario="text_feedback"' in prompt
    assert '"entities"' in prompt
    assert '"relations"' in prompt
    assert "共指" in prompt
    assert "Trace timestamp" in prompt
    assert "时间先后不等于因果" in prompt
    assert "previous_logic" in prompt
    assert "只有 direction_change" in prompt
    assert "这不是方向变更" in prompt
    assert "diagnostic_rule" not in prompt


def test_each_profile_has_its_own_extraction_rules_and_example():
    expected = {
        "user_directive": "一次性命令",
        "story_knowledge": "林深是卧底",
        "text_feedback": "anchor_only",
        "review_finding": "第 23 行少了句号",
        "agent_behavior": "已有足够上下文",
        "project_decision": "保留双时间线",
        "general_fact": "六个月后",
    }

    for name, marker in expected.items():
        prompt = build_semantic_extraction_section([PROFILES[name]])
        assert f'scenario="{name}"' in prompt
        assert marker in prompt


def test_semantic_normalization_keeps_only_profile_fields_and_known_relations():
    profiles = select_extraction_profiles([
        {"type": "user_answer", "answers": {"要求": "动作场景写清人物距离"}},
    ])
    record = normalize_record_semantic({
        "category": "user",
        "content": "动作场景必须写清人物距离。",
        "semantic": {
            "scenario": "not-a-profile",
            "what": "用户要求动作场景写清人物距离。",
            "who": "用户",
            "entities": [
                {"name": "动作场景", "type": "unknown", "role": "适用对象"},
                "空间关系",
            ],
            "relations": [
                {"type": "applies_to", "target": "动作场景", "description": "明确限定"},
                {"type": "invented", "target": "无效"},
            ],
            "attributes": {
                "target": "动作场景",
                "desired": "人物距离清楚",
                "diagnostic_rule": "不属于 user_directive",
            },
        },
    }, profiles)

    assert record["extraction_profile"] == "user_directive"
    assert record["semantic"]["who"] == ["用户"]
    assert record["semantic"]["entities"][0]["type"] == "concept"
    assert record["semantic"]["relations"] == [{
        "type": "applies_to", "target": "动作场景", "description": "明确限定",
    }]
    assert record["semantic"]["attributes"] == {
        "target": "动作场景", "desired": "人物距离清楚",
    }


def test_user_directive_keeps_reason_and_old_vs_user_logic():
    record = normalize_record_semantic({
        "category": "user",
        "content": "不是让全书都加快，我只是不满意追逐段拖沓。",
        "semantic": {
            "scenario": "user_directive",
            "why": "追逐段阅读节奏拖沓",
            "attributes": {
                "trigger": "用户审阅追逐段",
                "change_type": "correction",
                "previous_logic": "全书整体加快",
                "user_logic": "只加快追逐段，日常段保持慢节奏",
                "logic_difference": "适用范围从全书缩小为追逐段",
                "dissatisfaction": "追逐段拖沓",
                "diagnostic_rule": "不属于 user_directive",
            },
        },
    }, [PROFILES["user_directive"]])

    assert record["semantic"]["why"] == "追逐段阅读节奏拖沓"
    assert record["semantic"]["attributes"] == {
        "trigger": "用户审阅追逐段",
        "change_type": "correction",
        "previous_logic": "全书整体加快",
        "user_logic": "只加快追逐段，日常段保持慢节奏",
        "logic_difference": "适用范围从全书缩小为追逐段",
        "dissatisfaction": "追逐段拖沓",
    }


def test_missing_semantic_payload_gets_a_minimal_graph_ready_fact():
    profiles = select_extraction_profiles([
        {"type": "user_message", "content": "我习惯先写大纲再写正文。"},
    ])

    record = normalize_record_semantic({
        "category": "user",
        "claim": "先写大纲再写正文",
        "content": "用户习惯先写大纲再写正文。",
    }, profiles)

    assert record["extraction_profile"] == "user_directive"
    assert record["semantic"] == {
        "scenario": "user_directive",
        "assertion_status": "",
        "perspective": "world",
        "fact_kind": "directive",
        "what": "用户习惯先写大纲再写正文。",
        "who": [], "when": "", "where": "", "why": "",
        "temporal": {
            "kind": "unknown", "start": "", "end": "",
            "precision": "unknown", "source_text": "",
        },
        "entities": [], "relations": [], "attributes": {},
    }


def test_causal_target_index_can_only_point_to_an_earlier_record():
    profiles = [PROFILES["story_knowledge"]]
    data = normalize_analysis_semantics({"records": [
        {
            "content": "母亲失踪。",
            "semantic": {
                "scenario": "story_knowledge",
                "relations": [{"type": "causes", "target_index": 1}],
            },
        },
        {
            "content": "林深开始调查。",
            "semantic": {
                "scenario": "story_knowledge",
                "relations": [{"type": "caused_by", "target_index": 0}],
            },
        },
    ]}, profiles)

    assert data["records"][0]["semantic"]["relations"] == []
    assert data["records"][1]["semantic"]["relations"] == [{
        "type": "caused_by", "target": "", "description": "", "target_index": 0,
    }]


def test_agent_behavior_defaults_to_agent_experience_event():
    record = normalize_record_semantic({
        "category": "agent",
        "content": "上下文充分时仍调用了 AskUserQuestion。",
        "semantic": {"scenario": "agent_behavior"},
    }, [PROFILES["agent_behavior"]])

    assert record["semantic"]["perspective"] == "agent_experience"
    assert record["semantic"]["fact_kind"] == "event"
