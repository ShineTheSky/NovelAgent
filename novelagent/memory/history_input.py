"""Replace verified body snapshots in retain input, not in stored evidence."""

from copy import deepcopy
import json
import re


class HistoryBodyReferences:
    def __init__(self, history):
        self.history = history
        self.revisions = {}
        self.catalog = {}

    def reference(self, revision_id, path=""):
        if not revision_id:
            return None
        if revision_id not in self.revisions:
            self.revisions[revision_id] = self.history.get_revision(revision_id)
        revision = self.revisions[revision_id]
        if revision and path and revision["path"] != path.replace("\\", "/"):
            return None
        item = {"revision_id": revision_id, "status": "available" if revision else "missing",
                "path": revision["path"] if revision else path}
        if revision:
            item.update(previous_revision_id=revision["parent_revision_id"],
                        body_sha256=revision["body_sha256"], total_lines=len(revision["body_md"].splitlines()))
        self.catalog[revision_id] = item
        return {"revision_id": revision_id, "status": item["status"]}

    @staticmethod
    def revision_from_result(result):
        match = re.search(r"当前修订[:：]\s*([^\s\]]+)", str(result or ""))
        return match.group(1) if match else ""

    def read(self, payload):
        params = payload.get("params") or {}
        path = payload.get("path") or params.get("path", "")
        revision_id = payload.get("revision_id") or self.revision_from_result(payload.get("content"))
        ref = self.reference(revision_id, path)
        # A known-but-missing version remains a reference, never a current-file fallback.
        # Unversioned reads stay inline; stored evidence is not changed in either case.
        if ref:
            payload.pop("content", None)
            payload.update(revision_id=revision_id, body_reference=ref)
        return payload

    def write(self, payload):
        if payload.get("tool") != "Write" or payload.get("success") is not True:
            return payload
        params = payload.get("params") or {}
        revision_id = payload.get("revision_id") or self.revision_from_result(payload.get("result"))
        ref = self.reference(revision_id, params.get("path", ""))
        if ref:
            from novelagent.versioning import revision_manager
            content = params.get("content")
            # Existing snapshots must match; absent snapshots are explicitly marked missing.
            if isinstance(content, str) and (ref["status"] == "missing" or
                    revision_manager.parse(content).body == self.revisions[revision_id]["body_md"]):
                params.pop("content")
                payload.update(revision_id=revision_id, body_reference=ref)
        return payload

    def events(self, evidence):
        result = deepcopy(evidence)
        for event in result:
            if event["kind"] not in {"document_reference", "file_change_attempt"}:
                continue
            try:
                payload = json.loads(event["content"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            payload = self.read(payload) if event["kind"] == "document_reference" else self.write(payload)
            event["content"] = json.dumps(payload, ensure_ascii=False)
        return result

    def artifacts(self, artifacts):
        result = []
        for artifact in artifacts:
            self.reference(artifact["revision_id"], artifact["path"])
            self.reference(artifact.get("previous_revision_id"), artifact["path"])
            result.append({k: v for k, v in artifact.items() if k not in {"body", "diff"}})
        return result

    def display_events(self, evidence):
        """Hide body payloads in the UI only; unresolved source data stays stored."""
        result = self.events(evidence)
        for event in result:
            if event["kind"] not in {"document_reference", "file_change_attempt"}:
                continue
            try:
                payload = json.loads(event["content"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if event["kind"] == "file_change_attempt" and payload.get("tool") != "Write":
                continue
            params = payload.get("params") or {}
            revision_id = payload.get("revision_id") or self.revision_from_result(payload.get("result") or payload.get("content"))
            path = payload.get("path") or params.get("path", "")
            ref = self.reference(revision_id, path)
            payload.pop("content", None)
            params.pop("content", None)
            if "result" in payload:
                payload["result"] = "写入成功" if payload.get("success") else "写入未成功（原始结果保留在来源证据中）"
            payload["body_reference"] = ref or {"revision_id": revision_id, "status": "unresolved"}
            event["content"] = json.dumps(payload, ensure_ascii=False)
        return result

    def context(self, context):
        result = deepcopy(context)
        replies = {m.get("tool_call_id"): m for m in result if m["role"] in {"tool", "tool_result"}}
        for message in result:
            for call in message.get("tool_calls", []):
                function, reply = call["function"], replies.get(call["id"])
                if not reply or function["name"] not in {"Read", "Write"}:
                    continue
                try:
                    params = json.loads(function["arguments"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(params, dict):
                    continue
                if function["name"] == "Read":
                    payload = self.read({"params": params, "content": reply.get("content", "")})
                    if "content" not in payload:
                        reply["content"] = json.dumps(payload, ensure_ascii=False)
                elif not str(reply.get("content", "")).startswith("Error:"):
                    payload = self.write({"tool": "Write", "params": params, "success": True,
                                          "result": reply.get("content", "")})
                    if "body_reference" in payload:
                        function["arguments"] = json.dumps({**params, "body_reference": payload["body_reference"]}, ensure_ascii=False)
        return result
