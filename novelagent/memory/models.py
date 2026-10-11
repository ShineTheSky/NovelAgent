"""Versioned fact packages and learned knowledge; provenance is not model prose."""

from typing import Literal
from copy import deepcopy
import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator


KnowledgeDomain = Literal["content", "writing", "agent_work"]
KnowledgeCategory = Literal[
    "character", "setting", "background", "organization", "location", "item", "event",
    "outline", "drafting", "polishing", "reviewing", "tool_use", "task_dispatch",
    "context_management", "collaboration", "interaction_confirmation", "result_verification", "task_recovery",
]
CATEGORIES = {
    "content": {"character", "setting", "background", "organization", "location", "item", "event"},
    "writing": {"outline", "drafting", "polishing", "reviewing"},
    "agent_work": {"tool_use", "task_dispatch", "context_management", "collaboration",
                   "interaction_confirmation", "result_verification", "task_recovery"},
}
_STATEMENT_KEYS = {
    "facts": "statements", "fact_id": "statement_id", "fact_ids": "statement_ids",
    "fact_changes": "statement_changes", "previous_fact_ids": "previous_statement_ids",
    "current_fact_ids": "current_statement_ids", "previous_fact_id": "previous_statement_id",
    "current_fact_id": "current_statement_id", "source_fact_id": "source_statement_id",
    "target_fact_id": "target_statement_id",
}


def normalize_statement_keys(value):
    """Adapt structural names without altering IDs or opaque business attributes."""
    if isinstance(value, list):
        return [normalize_statement_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        name = _STATEMENT_KEYS.get(key, key)
        adapted = deepcopy(item) if key == "attributes" else normalize_statement_keys(item)
        if name in result and result[name] != adapted:
            raise ValueError(f"Conflicting legacy and current fields: {name}")
        result[name] = adapted
    return result


def legacy_statement(value):
    result = normalize_statement_keys(value)
    if isinstance(result, dict):
        domain = result.pop("domain", None)
        if "classifications" not in result and domain is not None:
            result["classifications"] = [{"domain": domain, "categories": []}]
    return result


def knowledge_payload(value):
    """Read old payloads in the new contract without rewriting stored evidence."""
    result = normalize_statement_keys(value)
    if not isinstance(result, dict):
        return result
    result.pop("category", None)
    result.pop("domain", None)
    domains = result.pop("knowledge_domains", [])
    if "statements" in result:
        result["statements"] = [legacy_statement(item) for item in result["statements"]]
    elif "classifications" not in result and domains:
        result["classifications"] = [{"domain": domain, "categories": []} for domain in dict.fromkeys(domains)]
    return result


def classification_union(statements):
    groups = {}
    for statement in statements:
        for group in statement.get("classifications", []):
            categories = groups.setdefault(group["domain"], [])
            categories.extend(c for c in group["categories"] if c not in categories)
    return [{"domain": domain, "categories": categories} for domain, categories in groups.items()]


def require_classifications(payload):
    groups = payload.get("classifications", [])
    if not groups or any(not group.get("categories") for group in groups):
        raise ValueError("New knowledge requires classifications with first-level categories")
    for group in groups:
        Classification.model_validate(group)


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def read_legacy_names(cls, value):
        return normalize_statement_keys(value)


class Classification(EvidenceModel):
    domain: KnowledgeDomain
    categories: list[KnowledgeCategory] = Field(default_factory=list, description="对应域的一级分类；旧数据待分类可为空")

    @model_validator(mode="after")
    def validate_categories(self):
        if set(self.categories) - CATEGORIES[self.domain]:
            raise ValueError(f"Invalid categories for {self.domain}: {self.categories}")
        self.categories = list(dict.fromkeys(self.categories))
        return self


class Entity(EvidenceModel):
    name: str = Field(min_length=1, description="本事实证据支持的稳定称呼；用户自身事实用 user，文档对象用真实项目相对路径")
    kind: str = Field(default="object", description="新实体使用 person/organization/place/object/event/mechanism/concept/theme/technique/goal/criterion/artifact/agent/user；已有身份保留旧类型")
    aliases: list[str] = Field(default_factory=list, description="证据确认的同一身份别名或等义名称，不用泛称、相关概念或上下位关系替代身份")
    entity_id: str = Field(default="", description="已有稳定实体ID；同批新实体可用 new:entity:1 等共享身份，不凭同名合并")
    ambiguous: bool = False
    new_identity: bool = False


class TimeAnchor(EvidenceModel):
    dimension: Literal["created", "observed", "occurred", "story", "valid", "chapter"]
    start: str = ""
    end: str = ""
    raw: str = ""
    precision: str = "unknown"

    @model_validator(mode="after")
    def preserve_unresolved_chapter(self):
        if self.dimension == "chapter":
            try:
                for value in (self.start, self.end):
                    if value:
                        time_key("chapter", value)
            except ValueError:
                # A volume/chapter label is not an invented section position.
                self.raw = " | ".join(dict.fromkeys(v for v in (self.raw, self.start, self.end) if v))
                self.start = self.end = ""
                self.precision = "unknown"
        return self


class MemoryStatement(EvidenceModel):
    statement_id: str = Field(default="", description="已有稳定 ID 不变；新事实为空，需在本包关系引用时用 new:1 等临时 ID")
    classifications: list[Classification] = Field(min_length=1)
    claim: str = Field(min_length=1)
    source_event_ids: list[str] = Field(min_length=1)
    modality: Literal["asserted", "written", "planned", "suggested", "inferred", "unknown"] = "unknown"
    entities: list[Entity] = Field(default_factory=list)
    times: list[TimeAnchor] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)
    status: Literal["active", "withdrawn"] = "active"

    @model_validator(mode="before")
    @classmethod
    def read_legacy_domain(cls, value):
        return legacy_statement(value)


class StatementChange(EvidenceModel):
    kind: Literal["corrected", "split", "merged", "withdrawn"]
    previous_statement_ids: list[str] = Field(min_length=1)
    current_statement_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    source_event_ids: list[str] = Field(min_length=1)


class Relation(EvidenceModel):
    kind: Literal["entity", "semantic", "causal", "temporal"]
    predicate: str = Field(min_length=1)
    target_id: str = Field(default="", description="空表示本 Memory 包内关系，否则为真实已有记录 ID")
    target_version: int = Field(ge=1)
    source_statement_id: str = ""
    target_statement_id: str = ""
    confidence: float = Field(default=1, ge=0, le=1)
    inferred: bool = False
    domains: list[KnowledgeDomain] = Field(default_factory=list)
    scope: str = ""
    status: Literal["asserted", "hypothesis", "refuted"] = "asserted"
    source_event_ids: list[str] = Field(min_length=1)
    direction: Literal["directed", "undirected"] = "directed"

    @model_validator(mode="after")
    def normalize_direction(self):
        if (self.kind == "semantic" and self.predicate == "related") or (
            self.kind == "temporal" and self.predicate in {"same_section", "near_time"}
        ):
            self.direction = "undirected"
        elif self.kind == "causal":
            if self.direction != "directed":
                raise ValueError("Causal relations must be directed")
        return self


class GraphRelation(Relation):
    owner_id: str = Field(min_length=1)
    owner_version: int = Field(ge=1)


class ArtifactFragment(EvidenceModel):
    path: str
    revision_id: str
    previous_revision_id: str = ""
    source_event_id: str = ""
    before: str = ""
    after: str = ""
    verification: Literal["unverified", "event_verified", "version_verified", "model_trusted"] = "unverified"


class MemoryPackage(EvidenceModel):
    title: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    background: str
    requirement: str = Field(description="用户原需求（含 Ask 回答）或审阅 Agent 的修改需求及明确条件；不是主 Agent 派工或自行增加的约束")
    reason: str = Field(default="未知", description="业务原因及旧理解与新要求的有证据差异；不是系统故障或无依据的心理动机")
    interpretation: str = Field(default="未知", description="主 Agent 理解与传达、子 Agent 理解及其与原需求的有证据差异；保留增加的限制、扩展、遗漏或范围变化")
    handling: str = Field(default="未知", description="子 Agent 实际处理方式与依据；区分计划、完成报告、成功写入和版本核验")
    result: str = Field(default="未知", description="业务结果；区分 Agent 完成报告和实际版本核验，不从无证据推出未执行")
    unresolved: str = Field(default="", description="证据明确的未落实业务要求；无明确未完成项则为空，证据缺口不是未完成工作")
    completion: Literal["complete", "partial", "no_change", "unknown"] = Field(
        default="unknown", description="业务完成状态，不继承 History 执行状态；partial 须有未落实要求，unknown 表示落实结果无证据")
    classifications: list[Classification] = Field(default_factory=list, description="程序从有效 statements 汇总，不独立判断")
    artifact_paths: list[str] = Field(default_factory=list)
    source_text: str = ""
    fragments: list[ArtifactFragment] = Field(default_factory=list)
    statements: list[MemoryStatement] = Field(min_length=1)
    statement_changes: list[StatementChange] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def read_legacy_package(cls, value):
        return knowledge_payload(value)

    @model_validator(mode="after")
    def aggregate_classifications(self):
        self.classifications = [Classification.model_validate(group) for group in classification_union(
            [statement.model_dump() for statement in self.statements if statement.status == "active"])]
        return self


def exclude_dispatch_entities(payload: dict) -> dict:
    """Normalize new writes only; dispatch evidence is not an entity-retrieval link."""
    for statement in payload["statements"]:
        if any(group["domain"] == "agent_work" and "task_dispatch" in group["categories"]
               for group in statement["classifications"]):
            statement["entities"] = []
    return payload


class FragmentSelection(EvidenceModel):
    source_event_id: str = Field(min_length=1, description="给定 History 中对应的成功修改或版本读取事件；路径和版本号由程序补填")
    before: str = ""
    after: str = ""


class SourceEventQuote(EvidenceModel):
    event_id: str = Field(min_length=1, description="与本包 source_event_ids 或 fragments.source_event_id 对应的完整事件 ID")
    excerpt: str = Field(min_length=1, pattern=r"\S", description="该事件中直接支持本包结论的连续原文；程序允许90%字符相似度，不是摘要，不固定取开头，不引用 ID 或字段名")


class MemoryExtractionPackage(MemoryPackage):
    fragments: list[FragmentSelection] = Field(default_factory=list)
    source_event_quotes: list[SourceEventQuote] = Field(description="每个被引用的事件至少提供一段相关原文；程序校验并唯一定位纠错，不写入正式 Memory")


class SourceMemory(EvidenceModel):
    memory_id: str
    version: int = Field(ge=1)
    statement_ids: list[str] = Field(default_factory=list, description="仅给定 Memory 版本中的真实事实 ID；空列表表示该版本全部事实")
    role: Literal["support", "against", "context"] = "support"


class Solution(EvidenceModel):
    solution_id: str = ""
    explanation: str = Field(min_length=1)
    method: str = ""
    conditions: str = ""
    roles: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    content_types: list[Literal["chapter", "volume_outline", "chapter_outline", "character", "world", "other"]] = Field(default_factory=list)
    score: float = Field(ge=0, le=1)
    status: Literal["active", "rejected"] = "active"
    human_selected: bool = False


class Insight(EvidenceModel):
    title: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    kind: Literal["rule", "fact", "method", "standard"]
    purpose: str
    conditions: str = ""
    exceptions: str = ""
    method: str = ""
    check: str = ""
    classifications: list[Classification] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    content_types: list[Literal["chapter", "volume_outline", "chapter_outline", "character", "world", "other"]] = Field(default_factory=list)
    times: list[TimeAnchor] = Field(default_factory=list)
    sources: list[SourceMemory] = Field(min_length=1)
    solutions: list[Solution] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    authority: Literal["human", "reviewer", "agent"] = "agent"
    human_confirmed: bool = False
    status: Literal["active", "disputed", "withdrawn"] = "active"

    @model_validator(mode="before")
    @classmethod
    def read_legacy_package(cls, value):
        return knowledge_payload(value)

    @model_validator(mode="after")
    def merge_source_statements(self):
        sources = {}
        for source in self.sources:
            key = (source.memory_id, source.version, source.role)
            if key not in sources:
                sources[key] = source.model_copy(deep=True)
            elif not sources[key].statement_ids or not source.statement_ids:
                sources[key].statement_ids = []  # Empty means all statements, not none.
            else:
                sources[key].statement_ids = list(dict.fromkeys([*sources[key].statement_ids, *source.statement_ids]))
        self.sources = list(sources.values())
        return self


def chosen_solution(payload: dict, role="", paths=(), content_type="") -> dict | None:
    active = [s for s in payload.get("solutions", []) if s.get("status") == "active"
              and (not s.get("roles") or not role or role in s["roles"])
              and (not s.get("artifact_paths") or bool(set(paths) & set(s["artifact_paths"])))
              and (not s.get("content_types") or content_type in s["content_types"])]
    return max(active, key=lambda s: (bool(s.get("human_selected")), s["score"], s["solution_id"]), default=None)


def time_key(dimension: str, value: str) -> str:
    if dimension == "chapter":
        parts = value.split(".")
        if len(parts) != 3 or any(not p.isdigit() or int(p) > 999999 for p in parts):
            raise ValueError("Chapter time must be volume.chapter.section")
        return ".".join(f"{int(p):06d}" for p in parts)
    if dimension == "story" and re.fullmatch(r"story:\d+", value):
        return f"story:{int(value[6:]):012d}"
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def retrieval_text(layer: str, payload: dict) -> str:
    payload = knowledge_payload(payload)
    if layer == "memory":
        fields = ("title", "direction", "background", "requirement", "reason", "handling", "result", "source_text")
        return "\n".join([*(str(payload.get(k) or "") for k in fields),
                           *(f["claim"] for f in payload.get("statements", []) if f.get("status", "active") == "active"), *payload.get("artifact_paths", []),
                           *(e["name"] for f in payload.get("statements", []) if f.get("status", "active") == "active" for e in f.get("entities", []))])
    return "\n".join(str(payload.get(k) or "") for k in
                     ("title", "claim", "purpose", "conditions", "exceptions", "method", "check"))


def render_knowledge(record: dict, role="", paths=(), content_type="") -> str:
    payload = knowledge_payload(record["payload"])
    domains = {"content": "内容", "writing": "写作", "agent_work": "Agent 工作"}
    categories = {"character": "人物", "setting": "设定", "background": "背景", "organization": "组织",
        "location": "地点", "item": "物品", "event": "事件", "outline": "大纲规划", "drafting": "正文创作",
        "polishing": "润色修改", "reviewing": "审阅检查", "tool_use": "工具使用", "task_dispatch": "任务调度",
        "context_management": "上下文管理", "collaboration": "协作交接", "interaction_confirmation": "交互确认",
        "result_verification": "结果核验", "task_recovery": "任务恢复"}
    classification = "；".join(f"{domains[c['domain']]}：" + ("、".join(categories[k] for k in c['categories']) or "待分类")
                              for c in payload.get("classifications", []))
    if record["layer"] == "memory":
        labels = {"background": "必要前情", "requirement": "需求", "reason": "原因", "interpretation": "理解",
                  "handling": "处理", "result": "结果", "unresolved": "未完成范围"}
        body = "\n\n".join(f"### {label}\n{payload[key]}" for key, label in labels.items())
        body += "\n\n### 事实\n" + "\n".join(f"- [{f['modality']}{'/已撤回' if f.get('status') == 'withdrawn' else ''}] {f['claim']}" for f in payload["statements"])
        if payload["source_text"]:
            body += "\n\n### 用户提供的原文\n" + payload["source_text"]
        for fragment in payload["fragments"]:
            body += (f"\n\n### {fragment['path']}：{fragment['previous_revision_id']} → {fragment['revision_id']}"
                     f"\n修改前：\n{fragment['before']}\n修改后：\n{fragment['after']}")
        return (f"知识分类：{classification}\n\n" if classification else "") + body
    solution = chosen_solution(payload, role, paths, content_type)
    return "\n".join(filter(None, [payload["claim"], f"目的：{payload['purpose']}",
        f"知识分类：{classification}" if classification else "",
        "文件范围："+", ".join(payload["artifact_paths"]) if payload["artifact_paths"] else "",
        "执行角色："+", ".join(payload["roles"]) if payload["roles"] else "",
        "正文类型："+", ".join(payload["content_types"]) if payload["content_types"] else "",
        "适用时间："+str(payload["times"]) if payload["times"] else "",
        f"来源权威：{payload['authority']}；人类确认：{payload['human_confirmed']}",
        f"条件：{payload['conditions']}", f"例外：{payload['exceptions']}",
        f"执行：{solution['method'] if solution else payload['method']}",
        f"检查：{payload['check']}", f"解释：{solution['explanation']}" if solution else ""]))
