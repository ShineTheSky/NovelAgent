"""Versioned fact packages and learned knowledge; provenance is not model prose."""

from typing import Literal
import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class Fact(EvidenceModel):
    fact_id: str = Field(default="", description="已有稳定 ID 不变；新事实为空，需在本包关系引用时用 new:1 等临时 ID")
    domain: Literal["content", "writing"]
    claim: str = Field(min_length=1)
    source_event_ids: list[str] = Field(min_length=1)
    modality: Literal["asserted", "written", "planned", "suggested", "inferred", "unknown"] = "unknown"
    entities: list[Entity] = Field(default_factory=list)
    times: list[TimeAnchor] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)
    status: Literal["active", "withdrawn"] = "active"


class FactChange(EvidenceModel):
    kind: Literal["corrected", "split", "merged", "withdrawn"]
    previous_fact_ids: list[str] = Field(min_length=1)
    current_fact_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    source_event_ids: list[str] = Field(min_length=1)


class Relation(EvidenceModel):
    kind: Literal["entity", "semantic", "causal", "temporal"]
    predicate: str = Field(min_length=1)
    target_id: str = Field(default="", description="空表示本 Memory 包内关系，否则为真实已有记录 ID")
    target_version: int = Field(ge=1)
    source_fact_id: str = ""
    target_fact_id: str = ""
    confidence: float = Field(default=1, ge=0, le=1)
    inferred: bool = False
    domains: list[Literal["content", "writing"]] = Field(default_factory=list)
    scope: str = ""
    status: Literal["asserted", "hypothesis", "refuted"] = "asserted"
    source_event_ids: list[str] = Field(min_length=1)


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
    requirement: str
    reason: str = Field(default="未知", description="业务原因及旧理解与新要求的有证据差异；不是系统故障或无依据的心理动机")
    interpretation: str = "未知"
    handling: str = "未知"
    result: str = Field(default="未知", description="业务结果；区分 Agent 完成报告和实际版本核验，不从无证据推出未执行")
    unresolved: str = Field(default="", description="证据明确的未落实业务要求；无明确未完成项则为空，证据缺口不是未完成工作")
    completion: Literal["complete", "partial", "no_change", "unknown"] = Field(
        default="unknown", description="业务完成状态，不继承 History 执行状态；partial 须有未落实要求，unknown 表示落实结果无证据")
    category: Literal["user", "project", "reference", "agent"] = "project"
    domain: Literal["writing", "outline", "overall"] = "overall"
    artifact_paths: list[str] = Field(default_factory=list)
    source_text: str = ""
    fragments: list[ArtifactFragment] = Field(default_factory=list)
    facts: list[Fact] = Field(min_length=1)
    fact_changes: list[FactChange] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


class FragmentSelection(EvidenceModel):
    source_event_id: str = Field(min_length=1, description="给定 History 中对应的成功修改或版本读取事件；路径和版本号由程序补填")
    before: str = ""
    after: str = ""


class MemoryExtractionPackage(MemoryPackage):
    fragments: list[FragmentSelection] = Field(default_factory=list)


class SourceMemory(EvidenceModel):
    memory_id: str
    version: int = Field(ge=1)
    fact_ids: list[str] = Field(default_factory=list, description="仅给定 Memory 版本中的真实事实 ID；空列表表示该版本全部事实")
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
    domain: Literal["writing", "outline", "overall"] = "overall"
    knowledge_domains: list[Literal["content", "writing"]] = Field(default_factory=list)
    category: Literal["user", "project", "reference", "agent"] = "project"
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

    @model_validator(mode="after")
    def merge_source_facts(self):
        sources = {}
        for source in self.sources:
            key = (source.memory_id, source.version, source.role)
            if key not in sources:
                sources[key] = source.model_copy(deep=True)
            elif not sources[key].fact_ids or not source.fact_ids:
                sources[key].fact_ids = []  # Empty means all facts, not no facts.
            else:
                sources[key].fact_ids = list(dict.fromkeys([*sources[key].fact_ids, *source.fact_ids]))
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
    if layer == "memory":
        fields = ("title", "direction", "background", "requirement", "reason", "handling", "result", "source_text")
        return "\n".join([*(str(payload.get(k) or "") for k in fields),
                           *(f["claim"] for f in payload.get("facts", []) if f.get("status", "active") == "active"), *payload.get("artifact_paths", []),
                           *(e["name"] for f in payload.get("facts", []) if f.get("status", "active") == "active" for e in f.get("entities", []))])
    return "\n".join(str(payload.get(k) or "") for k in
                     ("title", "claim", "purpose", "conditions", "exceptions", "method", "check"))


def render_knowledge(record: dict, role="", paths=(), content_type="") -> str:
    payload = record["payload"]
    if record["layer"] == "memory":
        labels = {"background": "必要前情", "requirement": "需求", "reason": "原因", "interpretation": "理解",
                  "handling": "处理", "result": "结果", "unresolved": "未完成范围"}
        body = "\n\n".join(f"### {label}\n{payload[key]}" for key, label in labels.items())
        body += "\n\n### 事实\n" + "\n".join(f"- [{f['modality']}{'/已撤回' if f.get('status') == 'withdrawn' else ''}] {f['claim']}" for f in payload["facts"])
        if payload["source_text"]:
            body += "\n\n### 用户提供的原文\n" + payload["source_text"]
        for fragment in payload["fragments"]:
            body += (f"\n\n### {fragment['path']}：{fragment['previous_revision_id']} → {fragment['revision_id']}"
                     f"\n修改前：\n{fragment['before']}\n修改后：\n{fragment['after']}")
        return body
    solution = chosen_solution(payload, role, paths, content_type)
    return "\n".join(filter(None, [payload["claim"], f"目的：{payload['purpose']}",
        "文件范围："+", ".join(payload["artifact_paths"]) if payload["artifact_paths"] else "",
        "执行角色："+", ".join(payload["roles"]) if payload["roles"] else "",
        "正文类型："+", ".join(payload["content_types"]) if payload["content_types"] else "",
        "适用时间："+str(payload["times"]) if payload["times"] else "",
        f"来源权威：{payload['authority']}；人类确认：{payload['human_confirmed']}",
        f"条件：{payload['conditions']}", f"例外：{payload['exceptions']}",
        f"执行：{solution['method'] if solution else payload['method']}",
        f"检查：{payload['check']}", f"解释：{solution['explanation']}" if solution else ""]))
