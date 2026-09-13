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
        note = (
            f"用户于 {item['manual_downgraded_at']} 将本条从 {layer} 降级。"
            "该信息可能不符合用户当前偏好或需要重新验证；后续相似反馈只能作为低幅证据，"
            "除非用户明确重新确认，否则不能直接恢复高优先级。"
        )
        content = str(item.get("content") or item.get("claim") or "").strip()
        item["content"] = content if note in content else f"{content}\n\n## 用户降级备注\n{note}"
        result = self._move(item, "memory" if layer == "pattern" else "evidence", reason="manual")
        self.rebuild_indexes()
        return result
