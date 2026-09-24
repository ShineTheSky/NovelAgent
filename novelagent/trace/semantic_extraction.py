"""Scenario-aware semantic extraction rules for Trace analysis.

The Trace analyzer still owns evidence binding and memory lifecycle decisions.
This module only decides which semantic facets matter for the current window and
normalizes the graph-ready metadata returned by the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractionProfile:
    name: str
    mission: str
    fields: tuple[str, ...]
    prompt: str


PROFILES = {
    "user_directive": ExtractionProfile(
        name="user_directive",
        mission="提取用户明确表达、纠正或确认的长期偏好、约束、目标和工作方式。",
        fields=("directive_type", "target", "desired", "prohibited", "conditions", "priority", "duration"),
        prompt="""应提取：用户明确说出的偏好、禁忌、质量标准、纠正、目标、持续有效的工作流程；一句话里有多个可独立变化的要求时拆成多条。
不得提取：礼貌确认、一次性命令、仅为当前调用提供的参数、Agent 自己推测出的偏好、没有被用户确认的建议。
模态：把“必须/以后都要”标为 confirmed；“可以试试/也许”标为 proposed；问题句和待用户选择项标为 open，绝不能改写为既定偏好。
实体：事实关于用户时包含 user 实体；target 指向写作维度、工具、Agent 或工件，而不是把“喜欢”“不要”等动词当实体。
正确例：输入“以后打斗必须写清距离，不要只堆招式”→ what="用户要求打斗场景明确人物距离并避免只罗列招式"，attributes.target="打斗场景"。
错误例：输入“继续写下一章”→ 不生成长期记录。""",
    ),
    "story_knowledge": ExtractionProfile(
        name="story_knowledge",
        mission="提取小说世界中可复用的人物、关系、地点、设定、状态变化、事件与因果。",
        fields=("canon_status", "subject_type", "state_before", "state_after", "story_time", "causes", "effects"),
        prompt="""应提取：人物身份/性格/能力/关系，地点与组织，世界规则，已确认的情节事件，角色状态变化及来源明确的因果。
不得提取：写作讨论本身、尚未选定的脑暴方案、只在当前句子中成立的修辞、模型补齐的世界观细节。
Canon：正文或用户明确确认的设定用 confirmed/canon；大纲计划或候选方案用 proposed/planned；被新设定替换时保留旧事实并建立 supersedes，不能静默覆盖。
共指：同一窗口同时出现“他/林深”时写成林深；没有明确证据时不得把相似姓名、称号或代词合并为同一人物。
时间：when 优先记录故事内发生时间；“三天后”等相对故事时间无法锚定时保留原表达并设置 temporal.precision=relative，不能用 Trace 的现实日期替代。
正确例：输入“林深原本回避冲突，但母亲失踪后开始主动调查”→ 分成状态事实与事件事实，并由调查事实通过 caused_by/target_index 指向母亲失踪事实。
错误例：输入“也许让林深其实是卧底？”→ assertion_status=proposed，不能写成“林深是卧底”。""",
    ),
    "text_feedback": ExtractionProfile(
        name="text_feedback",
        mission="提取用户针对具体正文或大纲片段的评价，以及可迁移的修改要求。",
        fields=("problem", "desired_effect", "forbidden_expression", "revision_strategy", "transfer_scope"),
        prompt="""应提取：被评价的文本锚点、用户指出的问题、希望达到的阅读效果、明确禁止项、可执行修改方向及适用范围。
不得提取：被引用正文中的虚构内容本身（除非用户同时确认它是设定）、RAG 参考片段、模型自行补出的写法、只适用于一个错字的意见。
原文保护：anchor_excerpt 和用户原始反馈由程序保存；semantic.what 只写用户要求，不复述或改写整段正文。
迁移性：只对当前锚点的意见写 attributes.transfer_scope="anchor_only"；用户明确概括为通用写法时才写 scene/chapter/project。
正确例：输入“这句太直白，用握杯子的动作表现压力”→ problem="情绪解释过直"，desired_effect="克制地表现压力"，revision_strategy="以动作替代解释"。
错误例：看到参考资料采用短句，就把“用户偏好短句”写入记忆。""",
    ),
    "review_finding": ExtractionProfile(
        name="review_finding",
        mission="从 reviewer 结果中提取可重复检查的底层写作缺陷，不保存一次性错字或肯定项。",
        fields=("symptom", "evidence", "trigger", "diagnostic_rule", "remediation", "severity"),
        prompt="""应提取：reviewer 明确指出、未来章节可以再次检测的底层缺陷；同时记录触发条件、原报告证据、诊断规则和修复原则。
不得提取：肯定项、一次性错字、只与当前情节取舍有关的建议、polisher 的修改说明、没有证据的风格判断。
强度：单次 reviewer 发现只能是 weak Insight；只有不同 Trace 或不同正文修订重复出现同一底层问题时才能 support。
粒度：把“节奏不好”改写为可检查表现，例如“连续三轮对白没有动作、视线或空间变化”；不要保存空泛标签。
正确例：报告“三轮对白中人物位置没有变化”→ diagnostic_rule="连续对白时检查是否存在动作或空间支点"。
错误例：报告“第 23 行少了句号”→ 不生成可复用 review_finding。""",
    ),
    "agent_behavior": ExtractionProfile(
        name="agent_behavior",
        mission="提取 Agent 或工具在特定条件下的异常行为、用户期望与修正方式。",
        fields=("trigger", "observed_behavior", "expected_behavior", "component", "impact", "correction"),
        prompt="""应提取：用户指出的 Agent/工具异常，失败发生条件，实际行为、期望行为、影响与可复用修正规则。
不得提取：正常工具调用流水、临时网络错误、没有归因依据的猜测、Agent 自己声称“已学会”的内容。
分类：用户对一次行为的质疑通常是 weak Insight；用户明确要求以后始终遵循的流程规则才是 strong Memory。
归因：区分用户事实和 Agent 经历。用户说“不要在信息充足时 AskUserQuestion”是用户规则；“本轮错误调用了 AskUserQuestion”是 Agent 行为事实。
正确例：输入“信息已经够了，为什么还问我？”→ trigger="已有足够上下文"，observed_behavior="仍调用 AskUserQuestion"，expected_behavior="直接继续执行"。
错误例：Trace 中出现一次 Read 调用→ 不生成 Agent 记忆。""",
    ),
    "project_decision": ExtractionProfile(
        name="project_decision",
        mission="提取已经作出的项目决定、采用理由、受影响工件与尚未解决的问题。",
        fields=("decision", "alternatives", "rationale", "affected_artifacts", "status", "open_questions"),
        prompt="""应提取：明确作出的设计/剧情/实现决定，选择理由，被否决或保留的备选项，受影响工件和仍待确认的问题。
不得提取：仅因 Write/Edit 已执行就推断出的决定、普通文件保存、未被接受的 Agent 建议、实现过程中的低层步骤。
状态：已经确认或实施写 confirmed；尚在讨论写 proposed；依赖用户选择写 open。工具成功只能证明文件发生变化，不能证明用户接受其设计理由。
正确例：输入“保留双时间线，因为需要让两条线索在第三章汇合”→ decision="保留双时间线"，rationale="两条线索将在第三章汇合"。
错误例：Agent 写入 outline.md→ 自动推断“用户决定采用该大纲”。""",
    ),
    "general_fact": ExtractionProfile(
        name="general_fact",
        mission="仅提取六个月后仍值得召回、且不属于其他场景的稳定事实。",
        fields=("fact_type", "scope"),
        prompt="""应提取：长期仍有用的身份、关系、背景、重要事件、稳定状态、计划、专业知识和项目上下文。
不得提取：寒暄、感谢、填充语、过程播报、重复事实、一次性命令，以及没有持久价值的琐碎信息。
选择性：先问“六个月后召回它是否仍会改变回答或行动？”答案是否定时跳过；质量优先于数量。
合并与拆分：描述同一主张的限定词合并为一条；可以独立变真或变假的主张拆开。
正确例：“项目使用 FastAPI，部署环境禁止联网”→ 两条独立事实。
错误例：“好的，我知道了”→ 不生成事实。""",
    ),
}

PROFILE_ORDER = tuple(PROFILES)

_TEXT_FEEDBACK_TERMS = (
    "正文", "原文", "描写", "台词", "对白", "文风", "节奏", "叙事", "视角", "改写", "润色",
    "直白", "不够生动", "留白", "句子", "段落",
)
_STORY_TERMS = (
    "设定", "人物", "角色", "关系", "世界观", "剧情", "情节", "大纲", "时间线", "伏笔", "结局",
    "地点", "阵营", "能力", "身份", "性格",
)
_AGENT_TERMS = (
    "agent", "工具调用", "为什么调用", "askuserquestion", "trace", "子代理", "权限询问", "调度异常",
    "工具失败", "agent 异常",
)
_PROJECT_PATH_MARKERS = ("outline", "project/", "characters/", "world/")
_TEXT_PATH_MARKERS = ("chapters/", "content_", "outline")


def select_extraction_profiles(event_view: list[dict], checkpoint_reasons: list[dict] | None = None) -> list[ExtractionProfile]:
    """Select every applicable profile; a mixed Trace window is not forced into one label."""
    selected: set[str] = set()
    texts: list[str] = []
    paths: list[str] = []

    for event in event_view:
        event_type = str(event.get("type") or event.get("event_type") or "")
        if event_type in {"user_message", "user_answer", "question_ask"}:
            selected.add("user_directive")
        if event.get("preset") == "reviewer" or event.get("workflow") == "auto_review":
            selected.add("review_finding")
        if event_type == "error" or event.get("success") is False or event.get("error"):
            selected.add("agent_behavior")

        for key in ("content", "result", "error"):
            value = event.get(key)
            if value:
                texts.append(str(value))
        answers = event.get("answers")
        if answers:
            texts.append(str(answers))
        params = event.get("params")
        if isinstance(params, dict):
            if params.get("reason"):
                texts.append(str(params["reason"]))
            if params.get("path"):
                paths.append(str(params["path"]).replace("\\", "/").lower())
            if event.get("tool") in {"Write", "Edit"}:
                selected.add("project_decision")

    for reason in checkpoint_reasons or []:
        if reason.get("reason"):
            texts.append(str(reason["reason"]))

    combined = "\n".join(texts).casefold()
    if any(term.casefold() in combined for term in _TEXT_FEEDBACK_TERMS) or any(
        marker in path for marker in _TEXT_PATH_MARKERS for path in paths
    ):
        selected.add("text_feedback")
    if any(term.casefold() in combined for term in _STORY_TERMS) or any(
        marker in path for marker in _PROJECT_PATH_MARKERS for path in paths
    ):
        selected.add("story_knowledge")
    if any(term in combined for term in _AGENT_TERMS):
        selected.add("agent_behavior")
    if checkpoint_reasons:
        selected.add("project_decision")
    if selected == {"user_directive"}:
        selected.add("general_fact")
    if not selected:
        selected.add("general_fact")

    return [PROFILES[name] for name in PROFILE_ORDER if name in selected]


def build_semantic_extraction_section(profiles: list[ExtractionProfile]) -> str:
    """Build the variable mission block while keeping one stable semantic fact shape."""
    profile_lines = []
    for profile in profiles:
        profile_lines.append(
            f'### scenario="{profile.name}"\n'
            f'MISSION：{profile.mission}\n'
            f'attributes 只填写这些键：{", ".join(profile.fields)}。\n'
            f'{profile.prompt}'
        )
    names = "|".join(profile.name for profile in profiles)
    return f"""

══════════════════════════════════════════════════════════════════════════
场景化语义提取
══════════════════════════════════════════════════════════════════════════

本窗口允许的 scenario 为 [{names}]；同一窗口可以输出多种 scenario，但每条 record 只能选择一个。

每条 records 必须额外包含 semantic：
{{
  "scenario": "从 [{names}] 中选择一个",
  "assertion_status": "confirmed|proposed|open；只按来源语气判断",
  "perspective": "world|agent_experience；用户/项目/小说事实属于 world，只有 Agent 或工具实际经历属于 agent_experience",
  "fact_kind": "event|state|directive|finding|decision",
  "what": "独立、原子、可验证的事实；不要把多个主张塞进一条",
  "who": ["涉及的人物、用户、Agent 或工件"],
  "when": "故事时间、有效期或发生时间；原文未说明则为空字符串",
  "temporal": {{"kind":"story|real|revision|validity|unknown","start":"ISO 时间或原始相对时间","end":"范围终点","precision":"exact|day|month|year|relative|unknown","source_text":"来源中的时间表达"}},
  "where": "地点或适用工件；原文未说明则为空字符串",
  "why": "明确出现的原因、目的或影响；不得脑补",
  "entities": [{{"name":"实体规范名称","type":"character|location|organization|artifact|concept|user|agent","role":"该事实中的作用"}}],
  "relations": [{{"type":"causes|caused_by|enables|prevents|supports|contradicts|supersedes|applies_to|part_of","target_index":0,"target":"已有记录 id 或无法用索引表示的明确对象","description":"关系依据"}}],
  "attributes": {{"仅使用当前 scenario 声明的字段":"没有证据的字段省略"}}
}}

共同规则（参考 Hindsight retain extraction）：
- 语言：所有事实使用来源文本的语言和文字，不翻译；名称、路径、标识符、代码和引文保持原样。
- 选择性：只保留未来会改变写作、审阅、规划或 Agent 行为的显著事实；寒暄、感谢、过程播报、普通工具调用和重复信息跳过。
- 粒度：what 用一到两句话写成可独立理解的原子事实，带上主语、对象、适用条件和必要限定。同一主张的修饰信息合并；能独立变真或变假的主张拆开。
- 共指：当称谓和名字在来源中明确指向同一对象时，使用规范名称并在 role 保留关系；证据不足时不合并人物、别名或代词。
- 模态：问题、建议、候选方案和未决选择不得改写为事实；分别使用 proposed 或 open。只有来源明确确认或已经发生的内容使用 confirmed。
- 视角：用户偏好、纠正、规则、小说内容和项目决定即使出现在 Agent 对话中也属于 world；只有 Agent/工具实际执行、观察或失败的行为属于 agent_experience。
- 类型：有明确发生或变化时间的是 event；持续属性是 state；用户规则是 directive；审阅缺陷是 finding；已作出的选择是 decision。
- 时间：Trace timestamp 是“被提及时间”，不是故事发生时间。现实相对时间只有在来源事件时间足以锚定时才转成绝对时间；故事相对时间不能用现实日期换算。只知道年月时保留完整月份范围，只知道年份时保留完整年份范围，禁止伪造精确日期。
- 实体：提取能连接其他事实的人物、地点、组织、工件、重要对象和抽象概念；不要把普通动词、评价词或 user/agent 之外的空泛代词当实体。
- 因果：causes/caused_by/enables/prevents 每条事实最多两个，只在来源明确表达因果或同一窗口可直接验证时记录。时间先后不等于因果；指向本批更早 records 时使用零基 target_index，且不得指向当前或后续记录。
- 无信息字段使用空字符串、空数组或空对象，不写“未知”“N/A”，也不得借用另一事实的值补齐。

以下是本窗口实际启用的场景提示词：
{chr(10).join(profile_lines)}
""".strip()


_ENTITY_TYPES = {"character", "location", "organization", "artifact", "concept", "user", "agent"}
_RELATION_TYPES = {
    "causes", "caused_by", "enables", "prevents", "supports", "contradicts", "supersedes",
    "applies_to", "part_of",
}


def normalize_record_semantic(
    record: dict[str, Any], profiles: list[ExtractionProfile], record_index: int | None = None,
) -> dict[str, Any]:
    """Normalize model-authored semantic metadata without inventing missing facts."""
    item = dict(record)
    raw = item.get("semantic")
    if not isinstance(raw, dict):
        raw = {}

    allowed_scenarios = {profile.name for profile in profiles}
    scenario = str(raw.get("scenario") or "").strip()
    if scenario not in allowed_scenarios:
        scenario = _fallback_scenario(item, allowed_scenarios)

    semantic: dict[str, Any] = {
        "scenario": scenario,
        "assertion_status": _assertion_status(raw.get("assertion_status")),
        "perspective": _perspective(raw.get("perspective"), scenario),
        "fact_kind": _fact_kind(raw.get("fact_kind"), scenario),
        "what": str(raw.get("what") or item.get("content") or item.get("claim") or "").strip(),
        "who": _string_list(raw.get("who")),
        "when": str(raw.get("when") or "").strip(),
        "temporal": _normalize_temporal(raw.get("temporal")),
        "where": str(raw.get("where") or "").strip(),
        "why": str(raw.get("why") or "").strip(),
    }

    entities = []
    for entity in raw.get("entities") or []:
        if isinstance(entity, str):
            name, entity_type, role = entity.strip(), "concept", ""
        elif isinstance(entity, dict):
            name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("type") or "concept").strip()
            role = str(entity.get("role") or "").strip()
        else:
            continue
        if name:
            entities.append({
                "name": name,
                "type": entity_type if entity_type in _ENTITY_TYPES else "concept",
                "role": role,
            })
    semantic["entities"] = entities

    relations = []
    for relation in raw.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        relation_type = str(relation.get("type") or "").strip()
        target = str(relation.get("target") or "").strip()
        try:
            target_index = int(relation["target_index"]) if "target_index" in relation else None
        except (TypeError, ValueError):
            target_index = None
        if target_index is not None and (
            target_index < 0 or (record_index is not None and target_index >= record_index)
        ):
            target_index = None
        if relation_type in _RELATION_TYPES and (target or target_index is not None):
            normalized_relation = {
                "type": relation_type,
                "target": target,
                "description": str(relation.get("description") or "").strip(),
            }
            if target_index is not None:
                normalized_relation["target_index"] = target_index
            relations.append(normalized_relation)
    semantic["relations"] = relations

    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    allowed_fields = set(PROFILES[scenario].fields) if scenario in PROFILES else set()
    semantic["attributes"] = {
        str(key): value for key, value in attributes.items() if str(key) in allowed_fields
    }
    item["semantic"] = semantic
    item["extraction_profile"] = scenario
    return item


def normalize_analysis_semantics(data: dict[str, Any], profiles: list[ExtractionProfile]) -> dict[str, Any]:
    normalized = dict(data)
    normalized["records"] = [
        normalize_record_semantic(record, profiles, index)
        for index, record in enumerate(data.get("records", []))
        if isinstance(record, dict)
    ]
    return normalized


def _fallback_scenario(record: dict[str, Any], allowed: set[str]) -> str:
    preferred = []
    if record.get("kind") == "text_feedback":
        preferred.append("text_feedback")
    if record.get("kind") == "review_issue":
        preferred.append("review_finding")
    if record.get("category") == "agent":
        preferred.append("agent_behavior")
    if record.get("category") == "user":
        preferred.append("user_directive")
    preferred.extend(PROFILE_ORDER)
    return next((name for name in preferred if name in allowed), "general_fact")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _assertion_status(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in {"confirmed", "proposed", "open"} else ""


def _perspective(value: Any, scenario: str) -> str:
    perspective = str(value or "").strip()
    if perspective in {"world", "agent_experience"}:
        return perspective
    return "agent_experience" if scenario == "agent_behavior" else "world"


def _fact_kind(value: Any, scenario: str) -> str:
    kind = str(value or "").strip()
    if kind in {"event", "state", "directive", "finding", "decision"}:
        return kind
    return {
        "user_directive": "directive",
        "review_finding": "finding",
        "project_decision": "decision",
        "agent_behavior": "event",
    }.get(scenario, "state")


def _normalize_temporal(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    kind = str(raw.get("kind") or "unknown").strip()
    precision = str(raw.get("precision") or "unknown").strip()
    return {
        "kind": kind if kind in {"story", "real", "revision", "validity", "unknown"} else "unknown",
        "start": str(raw.get("start") or "").strip(),
        "end": str(raw.get("end") or "").strip(),
        "precision": precision if precision in {"exact", "day", "month", "year", "relative", "unknown"} else "unknown",
        "source_text": str(raw.get("source_text") or "").strip(),
    }
