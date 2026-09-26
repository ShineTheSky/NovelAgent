"""Project-scoped evidence bank between immutable Trace and derived memory.

Trace is an audit log.  The bank keeps only evidence that may participate in a
later memory decision, including artifact revisions that can be criticized in a
different session or Trace.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class MemoryBankStore:
    def __init__(self, workspace_dir: str, project_id: str):
        self.project = Path(workspace_dir) / project_id
        self.root = self.project / ".bank" / "items"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean_path(value: object) -> str:
        return str(value or "").replace("\\", "/").lstrip("./")

    @staticmethod
    def _memory_relevant_artifact(path: str) -> bool:
        normalized = path.casefold()
        return normalized == "outline.md" or normalized.startswith((
            "chapters/", "project/", "world/",
        ))

    @staticmethod
    def _stable_id(kind: str, *parts: object) -> str:
        key = ":".join(str(part or "") for part in parts)
        return f"bank_{uuid.uuid5(uuid.NAMESPACE_URL, f'novelagent:{kind}:{key}').hex}"

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for path in self.root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("bank_item_id"):
                items.append(item)
        return sorted(items, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def write(self, item: dict) -> dict:
        bank_item_id = str(item["bank_item_id"])
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{bank_item_id}.json"
        existing = None
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
        now = self._now()
        saved = {
            **(existing if isinstance(existing, dict) else {}),
            **item,
            "bank_item_id": bank_item_id,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
        }
        if isinstance(existing, dict):
            comparable_existing = {key: value for key, value in existing.items() if key != "updated_at"}
            comparable_saved = {key: value for key, value in saved.items() if key != "updated_at"}
            if comparable_existing == comparable_saved:
                return existing
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
        return saved

    @staticmethod
    def _run_metadata(events: list[dict]) -> dict[str, dict]:
        runs: dict[str, dict] = {}
        for event in events:
            payload = event.get("payload") or {}
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                continue
            meta = runs.setdefault(run_id, {})
            if str(event.get("event_type", "")).endswith("subagent_start"):
                meta.update({
                    "agent_preset": str(payload.get("preset") or ""),
                    "workflow": str(payload.get("workflow") or ""),
                    "task_prompt": str(payload.get("task_prompt") or payload.get("task_summary") or ""),
                })
        return runs

    def retain_artifact_revisions(self, events: list[dict]) -> list[dict]:
        """Persist meaningful artifact changes, not the surrounding Trace chatter."""
        existing = self.list()
        revision_to_item = {
            str((item.get("artifact") or {}).get("revision_id") or ""): item["bank_item_id"]
            for item in existing
            if item.get("item_type") == "artifact_revision"
            and (item.get("artifact") or {}).get("revision_id")
        }
        run_metadata = self._run_metadata(events)
        saved_items = []
        for event in events:
            payload = event.get("payload") or {}
            revisions = payload.get("revision_events")
            if not isinstance(revisions, list):
                continue
            run_id = str(payload.get("run_id") or "")
            meta = run_metadata.get(run_id, {})
            for index, raw in enumerate(revisions):
                if not isinstance(raw, dict):
                    continue
                path = self._clean_path(raw.get("path"))
                revision_id = str(raw.get("revision_id") or "")
                if not path or not revision_id or not self._memory_relevant_artifact(path):
                    continue
                trace_id = str(event.get("trace_id") or "")
                event_id = str(event.get("event_id") or "")
                item_id = self._stable_id(
                    "artifact_revision", path, revision_id,
                )
                parent_revision_id = str(
                    raw.get("parent_revision_id") or raw.get("source_revision_id") or ""
                )
                related = []
                if parent_revision_id in revision_to_item:
                    related.append(revision_to_item[parent_revision_id])
                actor = str(raw.get("updated_by") or event.get("actor") or meta.get("agent_preset") or "")
                before = str(raw.get("anchor_excerpt") or "")
                after = str(raw.get("replacement_excerpt") or "")
                task_prompt = str(meta.get("task_prompt") or "")
                agent_result = str(payload.get("result") or "")[:6000]
                content_parts = [f"{actor or 'Agent'} 修改了 {path}，生成修订 {revision_id}。"]
                if task_prompt:
                    content_parts.append(f"任务要求：{task_prompt}")
                if before:
                    content_parts.append(f"修改前片段：{before}")
                if after:
                    content_parts.append(f"修改后片段：{after}")
                item = self.write({
                    "bank_item_id": item_id,
                    "item_type": "artifact_revision",
                    "content": "\n".join(content_parts),
                    "actor": actor,
                    "agent_preset": str(meta.get("agent_preset") or payload.get("preset") or ""),
                    "workflow": str(meta.get("workflow") or payload.get("workflow") or ""),
                    "task_prompt": task_prompt,
                    "agent_result": agent_result,
                    "source_agent_trace_id": str(payload.get("agent_trace_id") or ""),
                    "artifact": {
                        "path": path,
                        "revision_id": revision_id,
                        "parent_revision_id": str(raw.get("parent_revision_id") or ""),
                        "source_revision_id": str(raw.get("source_revision_id") or ""),
                        "anchor_excerpt": before,
                        "anchor_sha256": str(raw.get("anchor_sha256") or ""),
                        "replacement_excerpt": after,
                    },
                    "source_trace_refs": [{
                        "trace_id": trace_id,
                        "event_id": event_id,
                        "turn": int(event.get("trace_turn") or 1),
                    }],
                    "related_bank_item_ids": related,
                })
                revision_to_item[revision_id] = item_id
                saved_items.append(item)
        return saved_items

    @staticmethod
    def _event_text(event: dict) -> str:
        payload = event.get("payload") or {}
        if event.get("event_type") == "user_answer":
            return json.dumps(payload.get("answers") or "", ensure_ascii=False)
        return str(payload.get("content") or payload.get("message") or "")

    @staticmethod
    def _feedback_like(text: str) -> bool:
        lowered = text.casefold()
        markers = (
            "不对", "问题", "不应该", "不要", "改成", "修改", "重写", "不满意", "不喜欢",
            "调整", "反馈", "我觉得", "为什么", "这里", "这段", "刚才", "写得", "应该",
            "太", "不够", "issue", "wrong",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _normalized_text(value: object) -> str:
        return re.sub(r"\s+", "", str(value or "")).casefold()

    def related(self, events: list[dict], messages: list[dict] | None = None, limit: int = 12) -> list[dict]:
        """Find cross-Trace evidence using revision, artifact and quoted-text anchors."""
        items = self.list()
        if not items:
            return []
        texts = [self._event_text(event) for event in events]
        texts.extend(str(message.get("content") or "") for message in (messages or []) if isinstance(message, dict))
        combined = "\n".join(text for text in texts if text)
        normalized = self._normalized_text(combined)
        paths: set[str] = set()
        revisions: set[str] = set()
        for event in events:
            payload = event.get("payload") or {}
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if params.get("path"):
                paths.add(self._clean_path(params["path"]))
            for revision in payload.get("revision_events") or []:
                if not isinstance(revision, dict):
                    continue
                if revision.get("path"):
                    paths.add(self._clean_path(revision["path"]))
                for key in ("revision_id", "parent_revision_id", "source_revision_id"):
                    if revision.get(key):
                        revisions.add(str(revision[key]))

        ranked = []
        feedback_like = self._feedback_like(combined)
        for recency, item in enumerate(items):
            artifact = item.get("artifact") or {}
            score = 0
            if revisions.intersection({
                str(artifact.get("revision_id") or ""),
                str(artifact.get("parent_revision_id") or ""),
                str(artifact.get("source_revision_id") or ""),
            }):
                score = max(score, 100)
            if artifact.get("path") and self._clean_path(artifact["path"]) in paths:
                score = max(score, 80)
            for key in ("anchor_excerpt", "replacement_excerpt"):
                anchor = self._normalized_text(artifact.get(key))
                if normalized and len(anchor) >= 8 and (anchor in normalized or normalized in anchor):
                    score = max(score, 70)
            source_text = self._normalized_text(item.get("source_text"))
            if normalized and len(source_text) >= 8 and (source_text in normalized or normalized in source_text):
                score = max(score, 70)
            # A user often says only "刚才那段不对".  Keep a small, bounded
            # recency fallback for artifact revisions so the model can reconnect
            # the criticism to the writing task without merging it automatically.
            if not score and feedback_like and item.get("item_type") == "artifact_revision" and recency < min(limit, 6):
                score = 10
            if score:
                ranked.append((score, str(item.get("updated_at", "")), item))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [item for _, _, item in ranked[:limit]]

    def retain_semantic_records(
        self, records: list[dict], events: list[dict], trace_id: str,
        related_bank_items: list[dict] | None = None,
    ) -> list[dict]:
        """Persist only extractor-approved candidates and attach their bank IDs."""
        event_by_id = {str(event.get("event_id") or ""): event for event in events}
        allowed_related = {
            str(item.get("bank_item_id")) for item in (related_bank_items or []) if item.get("bank_item_id")
        }
        saved = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            source_event_ids = [
                str(value) for value in record.get("source_event_ids", []) if str(value) in event_by_id
            ]
            claim = str(record.get("claim") or record.get("title") or "").strip()
            content = str(record.get("content") or claim).strip()
            if not source_event_ids or not claim or not content:
                continue
            operation_id = str(record.get("operation_id") or "")
            item_id = self._stable_id(
                "semantic_evidence", operation_id or trace_id,
                ",".join(source_event_ids), claim, index,
            )
            requested_related = [
                str(value) for value in record.get("source_bank_item_ids", [])
                if str(value) in allowed_related
            ]
            user_inputs = [
                self._event_text(event_by_id[event_id])
                for event_id in source_event_ids
                if event_by_id[event_id].get("event_type") in {"user_message", "user_answer"}
                and self._event_text(event_by_id[event_id])
            ]
            path = self._clean_path(record.get("artifact_path"))
            artifact = {
                "path": path,
                "revision_id": str(record.get("artifact_revision_id") or ""),
                "anchor_excerpt": str(record.get("anchor_excerpt") or ""),
                "anchor_sha256": str(record.get("anchor_sha256") or ""),
            }
            item = self.write({
                "bank_item_id": item_id,
                "item_type": "semantic_evidence",
                "layer_hint": str(record.get("layer") or ""),
                "category": str(record.get("category") or "project"),
                "domain": str(record.get("domain") or "overall"),
                "kind": str(record.get("kind") or ""),
                "signal": str(record.get("signal") or ""),
                "confidence": record.get("confidence", 0.5),
                "claim": claim,
                "content": content,
                "semantic": record.get("semantic") if isinstance(record.get("semantic"), dict) else {},
                "source_text": str(record.get("source_text") or ""),
                "user_requirements": str(record.get("user_requirements") or ""),
                "revision_direction": str(
                    record.get("revision_direction") or record.get("feedback_direction") or ""
                ),
                "user_inputs": user_inputs,
                "artifact": artifact,
                "source_trace_refs": [{
                    "trace_id": str(event_by_id[event_id].get("trace_id") or trace_id),
                    "event_id": event_id,
                    "turn": int(event_by_id[event_id].get("trace_turn") or 1),
                } for event_id in source_event_ids],
                "related_bank_item_ids": requested_related,
            })
            record["bank_item_ids"] = list(dict.fromkeys([
                *record.get("bank_item_ids", []), item_id, *requested_related,
            ]))
            saved.append(item)
        return saved

    @staticmethod
    def prompt_view(items: list[dict]) -> list[dict]:
        return [{
            "bank_item_id": item.get("bank_item_id", ""),
            "item_type": item.get("item_type", ""),
            "layer_hint": item.get("layer_hint", ""),
            "category": item.get("category", ""),
            "domain": item.get("domain", ""),
            "kind": item.get("kind", ""),
            "signal": item.get("signal", ""),
            "claim": item.get("claim", ""),
            "content": str(item.get("content", ""))[:6000],
            "actor": item.get("actor", ""),
            "agent_preset": item.get("agent_preset", ""),
            "workflow": item.get("workflow", ""),
            "task_prompt": str(item.get("task_prompt", ""))[:4000],
            "agent_result": str(item.get("agent_result", ""))[:3000],
            "source_agent_trace_id": item.get("source_agent_trace_id", ""),
            "artifact": item.get("artifact", {}),
            "semantic": item.get("semantic", {}),
            "source_text": str(item.get("source_text", ""))[:4000],
            "user_requirements": str(item.get("user_requirements", ""))[:2000],
            "revision_direction": str(item.get("revision_direction", ""))[:2000],
            "user_inputs": item.get("user_inputs", []),
            "source_trace_refs": item.get("source_trace_refs", []),
            "related_bank_item_ids": item.get("related_bank_item_ids", []),
        } for item in items]
