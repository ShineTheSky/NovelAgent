"""File-backed derived records for immutable SQLite traces."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
import yaml

LAYERS = {"evidence": ".evidence", "memory": ".memory", "pattern": ".pattern"}
CATEGORIES = {"user", "project", "reference", "agent"}
DOMAINS = {"writing", "outline", "overall"}
EVIDENCE_INDEX_LIMIT = 1000
MEMORY_INDEX_LIMIT = 200
PATTERN_PROMOTION_WEIGHT = 200
MAX_RECORD_WEIGHT = 300


class FileLifecycleStore:
    def __init__(self, workspace_dir: str, project_id: str):
        self.project = Path(workspace_dir) / project_id

    def _root(self, layer: str, category: str = "project") -> Path:
        root = self.project / LAYERS[layer]
        return root / category

    def _base(self, layer: str) -> Path:
        return self.project / LAYERS[layer]

    def ensure(self):
        for layer in LAYERS:
            for category in CATEGORIES:
                self._root(layer, category).mkdir(parents=True, exist_ok=True)

    def list(self, layer: str) -> list[dict]:
        self.ensure(); records = []
        for path in self._base(layer).rglob("*.md"):
            if path.name in {"memory.md", "memory_archive.md", "project_rules.md", "evidence.md", "pattern.md"}: continue
            try:
                text = path.read_text(encoding="utf-8")
                _, header, body = text.split("---", 2)
                item = yaml.safe_load(header) or {}
                if item.get("id"):
                    item.setdefault("title", str(item.get("claim") or body.strip().split("\n", 1)[0]))
                    item.update(layer=layer, content=body.strip(), file_path=str(path.relative_to(self.project)).replace("\\", "/"))
                    records.append(item)
            except (OSError, ValueError, yaml.YAMLError):
                continue
        return sorted(records, key=lambda item: str(item.get("updated", "")), reverse=True)

    @staticmethod
    def _rank(item: dict) -> tuple[str, float]:
        date = str(item.get("updated") or item.get("created") or "")[:10]
        try:
            weight = float(item.get("weight", 0))
        except (TypeError, ValueError):
            weight = 0.0
        return date, weight

    def _write_index(self, layer: str, title: str, limit: int, records: list[dict]) -> None:
        selected = sorted(records, key=self._rank, reverse=True)[:limit]
        lines = [f"# {title}", "", f"> 系统生成索引：{len(selected)} / {len(records)} 条。", "",
                 "| # | 分类 | 范围 | 权重 | 更新时间 | 摘要 | 路径 |",
                 "|---|---|---|---:|---|---|---|"]
        for index, item in enumerate(selected, start=1):
            lines.append(
                f"| {index} | {item.get('category', '-')} | {item.get('domain', '-')} | "
                f"{item.get('weight', 0)} | {str(item.get('updated', ''))[:10]} | "
                f"{item.get('title', item.get('claim', '')).replace('|', ' ')} | {item.get('file_path', '')} |"
            )
        if not selected:
            lines.append("| - | - | - | - | - | 暂无记录 | - |")
        (self._base(layer) / f"{layer}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _move(self, item: dict, target_layer: str, *, reason: str | None = None) -> dict:
        path = self.project / item["file_path"]
        path.unlink(missing_ok=True)
        source_layer = item.get("layer")
        if source_layer == "memory" and target_layer == "evidence":
            item["origin_memory_id"] = item["id"]
            if reason:
                item["demotion_reason"] = reason
            item["id"] = f"evi_{uuid.uuid4().hex}"
        elif source_layer == "evidence" and target_layer == "memory":
            item["origin_evidence_id"] = item["id"]
            item["id"] = f"mem_{uuid.uuid4().hex}"
        return self.write(target_layer, item)

    def enforce_memory_capacity(self) -> list[dict]:
        """Demote least-active novel memories; Agent records are unbounded."""
        demoted = []
        records = self.list("memory")
        active = [item for item in records if item.get("category") != "agent"]
        for item in sorted(active, key=self._rank, reverse=True)[MEMORY_INDEX_LIMIT:]:
            demoted.append(self._move(item, "evidence", reason="capacity"))
        return demoted

    def rebuild_indexes(self) -> None:
        self.enforce_memory_capacity()
        evidence = self.list("evidence")
        memory = self.list("memory")
        patterns = self.list("pattern")
        self._write_index("evidence", "证据索引", EVIDENCE_INDEX_LIMIT,
                          [item for item in evidence if item.get("category") != "agent"])
        self._write_index("memory", "记忆索引", MEMORY_INDEX_LIMIT,
                          [item for item in memory if item.get("category") != "agent"])
        self._write_index("pattern", "模式索引", EVIDENCE_INDEX_LIMIT,
                          [item for item in patterns if item.get("category") != "agent"])

    def get(self, layer: str, record_id: str) -> dict | None:
        return next((item for item in self.list(layer) if item["id"] == record_id), None)

    @staticmethod
    def is_text_feedback(item: dict) -> bool:
        return item.get("category") == "reference" and item.get("kind") == "text_feedback"

    @staticmethod
    def is_review_issue(item: dict) -> bool:
        return item.get("domain") == "writing" and item.get("kind") == "review_issue"

    def review_issue_context(self, limit: int = 12) -> str:
        """Render recurring reviewer findings as actionable writing context."""
        records = [
            item for layer in ("pattern", "memory") for item in self.list(layer)
            if self.is_review_issue(item)
        ]
        selected = sorted(records, key=self._rank, reverse=True)[:limit]
        if not selected:
            return ""
        lines = ["## 高频写作错误（写作、审阅和润色时检查）"]
        for item in selected:
            lines.append(
                f"### {item.get('title', item.get('claim', '写作问题'))}"
                f"（累计 {item.get('support_count', 1)} 次）\n"
                f"{item.get('content', item.get('claim', ''))}"
            )
        return "\n\n".join(lines)

    @staticmethod
    def can_auto_promote(item: dict) -> bool:
        return item.get("promotion_status", "auto") != "manual_review"

    @staticmethod
    def confirm_auto_promotion(item: dict) -> dict:
        confirmed = dict(item)
        confirmed["promotion_status"] = "auto"
        confirmed["explicitly_reconfirmed_at"] = datetime.now(timezone.utc).isoformat()
        return confirmed

    def cancel_manual_review(self, layer: str, record_id: str) -> dict | None:
        item = self.get(layer, record_id)
        if not item:
            return None
        if item.get("promotion_status") != "manual_review":
            return item
        item = self.confirm_auto_promotion(item)
        item["manual_review_cancelled_at"] = item["explicitly_reconfirmed_at"]
        item.pop("downgrade_reason", None)
        item.pop("downgraded_from", None)
        note_marker = "\n\n## 用户降级备注\n"
        item["content"] = str(item.get("content") or "").partition(note_marker)[0].rstrip()
        result = self.write(layer, item)
        self.rebuild_indexes()
        return result

    def promote_memory_to_pattern(self, memory: dict, trace_ids: list[str]) -> dict:
        """Create one Pattern per source Memory, even if semantic matching missed it."""
        memory_id = str(memory["id"])
        existing = next(
            (item for item in self.list("pattern") if memory_id in item.get("memory_ids", [])),
            None,
        )
        if existing:
            existing["trace_ids"] = list(dict.fromkeys([*existing.get("trace_ids", []), *trace_ids]))
            existing["weight"] = max(float(existing.get("weight", 0)), float(memory.get("weight", 0)))
            existing["support_count"] = max(
                int(existing.get("support_count", 1)), int(memory.get("support_count", 1)),
            )
            return self.write("pattern", existing)
        return self.write("pattern", {
            "title": memory.get("title", memory["claim"]),
            "claim": memory["claim"],
            "content": memory.get("content", memory["claim"]),
            "category": memory["category"],
            "domain": memory["domain"],
            "kind": memory.get("kind", ""),
            "memory_ids": [memory_id],
            "trace_ids": trace_ids,
            "weight": memory["weight"],
            "support_count": memory["support_count"],
        })

    def merge_text_feedback(self, existing: dict, incoming: dict, trace_id: str,
                            source_event_ids: list[str], user_inputs: list[dict]) -> dict:



        """Refresh one reference-memory interpretation without treating repeats as votes."""
        merged = dict(existing)
        history = list(merged.get("feedback_history") or [])
        if not history and (merged.get("source_event_ids") or merged.get("trace_id")):
            history.append({
                "trace_id": str(merged.get("trace_id") or ""),
                "source_event_ids": list(merged.get("source_event_ids") or []),
                "feedback_direction": str(merged.get("feedback_direction") or ""),
                "relation": "new",
            })
        entry = {
            "trace_id": trace_id,
            "source_event_ids": list(dict.fromkeys(source_event_ids)),
            "feedback_direction": str(incoming.get("feedback_direction") or "").strip(),
            "relation": str(incoming.get("feedback_relation") or "").strip(),
        }
        if not any(
            row.get("trace_id") == entry["trace_id"] and row.get("source_event_ids") == entry["source_event_ids"]
            for row in history if isinstance(row, dict)
        ):
            history.append(entry)
        merged["feedback_history"] = history
        merged["feedback_count"] = len(history)
        inputs = list(merged.get("user_inputs") or [])
        known_input_ids = {str(row.get("event_id") or "") for row in inputs if isinstance(row, dict)}
        inputs.extend(row for row in user_inputs if str(row.get("event_id") or "") not in known_input_ids)
        merged["user_inputs"] = inputs
        merged["trace_id"] = trace_id
        merged["trace_ids"] = list(dict.fromkeys([*merged.get("trace_ids", []), trace_id]))
        merged["source_event_ids"] = list(dict.fromkeys([*merged.get("source_event_ids", []), *source_event_ids]))
        anchors = list(merged.get("anchor_examples") or [])
        if not anchors and merged.get("anchor_excerpt"):
            anchors.append({
                "artifact_path": str(merged.get("artifact_path") or "").replace("\\", "/").lstrip("./"),
                "artifact_revision_id": str(merged.get("artifact_revision_id") or ""),
                "anchor_excerpt": str(merged.get("anchor_excerpt") or "")[:700],
                "anchor_sha256": str(merged.get("anchor_sha256") or ""),
            })
        anchor = {
            "artifact_path": str(incoming.get("artifact_path") or "").replace("\\", "/").lstrip("./"),
            "artifact_revision_id": str(incoming.get("artifact_revision_id") or ""),
            "anchor_excerpt": str(incoming.get("anchor_excerpt") or "")[:700],
            "anchor_sha256": str(incoming.get("anchor_sha256") or ""),
        }
        if anchor["anchor_excerpt"] and not any(
            row.get("anchor_sha256") == anchor["anchor_sha256"] and row.get("artifact_path") == anchor["artifact_path"]
            for row in anchors if isinstance(row, dict)
        ):
            anchors.append(anchor)
        merged["anchor_examples"] = anchors
        for key in ("feedback_direction", "artifact_path", "artifact_revision_id", "anchor_excerpt", "anchor_sha256"):
            value = incoming.get(key)
            if value:
                merged[key] = value
        # Trace remains the immutable full evidence source.  Repeated feedback
        # refreshes the user's requirements and revision direction without
        # treating the same text anchor as another promotion vote.
        user_requirements = str(
            incoming.get("user_requirements")
            or merged.get("user_requirements")
            or ""
        ).strip()
        revision_direction = str(
            incoming.get("revision_direction")
            or incoming.get("feedback_direction")
            or incoming.get("feedback_analysis")
            or incoming.get("content")
            or merged.get("revision_direction")
            or merged.get("feedback_direction")
            or merged.get("feedback_analysis")
            or merged.get("content")
            or ""
        ).strip()
        merged["user_requirements"] = user_requirements
        merged["revision_direction"] = revision_direction
        merged.pop("feedback_analysis", None)
        merged["content"] = self.render_text_feedback_content(user_requirements, revision_direction, inputs)
        if incoming.get("title"):
            merged["title"] = str(incoming["title"])
            merged["claim"] = str(incoming.get("claim") or incoming["title"])
        return merged

    @staticmethod
    def render_text_feedback_content(user_requirements: str, revision_direction: str,
                                     user_inputs: list[dict]) -> str:
        lines = [
            "## 用户要求",
            user_requirements or "（尚待进一步提炼）",
            "",
            "## 修改方向",
            revision_direction or "（尚待进一步判断）",
            "",
            "## 用户原始反馈",
        ]
        if not user_inputs:
            lines.append("（未能定位原始用户输入；可按 trace_id 回查。）")
        for row in user_inputs:
            if not isinstance(row, dict):
                continue
            trace_id = row.get("trace_id", "")
            event_id = row.get("event_id", "")
            lines.extend([f"### Trace {trace_id} / Event {event_id}", str(row.get("content") or "")])
        return "\n".join(lines).strip()


    def text_feedback_for_artifact(self, relative_path: str, body: str, limit: int = 5) -> list[dict]:
        """Find normal reference memories whose quoted text belongs to this artifact."""
        normalized = relative_path.replace("\\", "/").lstrip("./")
        matches = []
        for item in self.list("memory"):
            if not self.is_text_feedback(item):
                continue
            anchors = [item, *[row for row in item.get("anchor_examples", []) if isinstance(row, dict)]]
            quoted = [str(anchor.get("anchor_excerpt") or "").strip() for anchor in anchors]
            same_path = str(item.get("artifact_path") or "").replace("\\", "/").lstrip("./") == normalized
            same_path = same_path or any(
                str(anchor.get("artifact_path") or "").replace("\\", "/").lstrip("./") == normalized
                for anchor in anchors
            )
            if same_path or any(quote and quote in body for quote in quoted):
                matches.append(item)
        return sorted(matches, key=self._rank, reverse=True)[:limit]

    def write(self, layer: str, item: dict) -> dict:
        self.ensure(); category = str(item.get("category", "project"))
        item["category"] = category if category in CATEGORIES else "project"
        item["domain"] = item.get("domain") if item.get("domain") in DOMAINS else "overall"
        item["title"] = str(item.get("title") or item.get("claim") or item.get("content") or "").strip().split("\n", 1)[0]
        item["claim"] = str(item.get("claim") or item["title"]).strip()
        item.setdefault("id", f"{layer[:3]}_{uuid.uuid4().hex}")
        item.setdefault("created", datetime.now(timezone.utc).isoformat())
        item["updated"] = datetime.now(timezone.utc).isoformat()
        body = str(item.get("content") or item.get("claim") or "").strip()
        payload = {key: value for key, value in item.items() if key not in {"content", "layer", "file_path"}}
        path = self._root(layer, item["category"]) / f"{item['id']}.md"
        path.write_text("---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body + "\n", encoding="utf-8")
        return {**payload, "layer": layer, "content": body, "file_path": str(path.relative_to(self.project)).replace("\\", "/")}

    def downgrade(self, layer: str, record_id: str) -> dict | None:
        item = self.get(layer, record_id)
        if not item: return None
        item["weight"] = max(0, float(item.get("weight", 0)) - 30)
        count = int(item.get("manual_downgrade_count", 0)) + 1
        item["manual_downgrade_count"] = count
        item["manual_downgraded_at"] = datetime.now(timezone.utc).isoformat()
        item["downgraded_from"] = layer
        item["promotion_status"] = "manual_review"
        item["downgrade_reason"] = "用户认为该记录目前不应保持原有等级，需保留不同意见并重新验证。"
        note = (
            f"用户于 {item['manual_downgraded_at']} 将本条从 {layer} 降级。"
            "后续证据可以继续追加，但不得触发自动晋级；只有用户明确重新确认后，"
            "才能恢复自动晋级。"
        )
        content = str(item.get("content") or item.get("claim") or "").strip()
        item["content"] = content if note in content else f"{content}\n\n## 用户降级备注\n{note}"
        result = self._move(item, "memory" if layer == "pattern" else "evidence", reason="manual")
        self.rebuild_indexes()
        return result
