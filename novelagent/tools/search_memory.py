"""Explicit fact-package recall and immutable version reads, never maturity events."""

import json

from novelagent.history import DocumentHistoryStore
from novelagent.tools.base import PermissionResult, ToolProtocol, ToolResult


class SearchMemoryTool(ToolProtocol):
    name = "SearchMemory"
    description = "证据不足或需要确认事实时，融合检索 Memory 事实包；或用 memory_id/version 精确读取来源版本。不是默认 Insight 召回。"
    parameters = {"type": "object", "properties": {
        "query": {"type": "string"}, "memory_id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
        "anyOf": [{"required": ["query"]}, {"required": ["memory_id"]}]}

    def __init__(self, pipeline):
        self.pipeline = pipeline

    async def execute(self, params, context):
        store = self.pipeline.store(context.project_id)
        if params.get("memory_id"):
            record = store.get(params["memory_id"], params.get("version"))
            if not record or record["layer"] != "memory":
                return ToolResult(success=False, error="Memory 或版本不存在")
            records = [record]
        elif params.get("query"):
            result = await self.pipeline.retrieval(context.project_id).recall(params["query"], purpose="memory_evidence",
                limit=max(1, min(int(params.get("limit", 4)), 8)))
            if result["status"] in {"failed", "timeout"}:
                await self.pipeline.capture_failure(context.project_id, {"session_id": context.session_id,
                    "trace_ids": [context.source_trace_id], "history_id": ""}, result.get("error"), "evidence_recall")
                return ToolResult(success=False, error=result.get("error", "Memory recall failed"))
            records = [r["record"] for r in result["items"]]
        else:
            return ToolResult(success=False, error="需要 query 或 memory_id")
        return ToolResult(success=True, data=json.dumps([store.view(r) for r in records], ensure_ascii=False))

    def checkPermissions(self, params, context):
        return PermissionResult.ALLOW


class ReadHistoryTool(ToolProtocol):
    name = "ReadHistory"
    description = "按 revision_id 分页读取本项目不可变版本正文（不附带思考，默认前200行）；版本缺失明确返回 missing，不读当前文件代替。或按 history_id 读取完整证据单元。只读，不累计成熟度。"
    parameters = {"type": "object", "properties": {
        "history_id": {"type": "string"}, "revision_id": {"type": "string"},
        "offset": {"type": "integer", "minimum": 1, "description": "正文起始行，默认1"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "正文行数，默认200，最多2000"}},
        "anyOf": [{"required": ["history_id"]}, {"required": ["revision_id"]}]}

    def __init__(self, pipeline):
        self.pipeline = pipeline

    async def execute(self, params, context):
        history = DocumentHistoryStore(self.pipeline.store(context.project_id).project_dir)
        if any(key in params and (not isinstance(params[key], str) or not params[key].strip())
               for key in ("history_id", "revision_id")):
            return ToolResult(success=False, error="history_id 和 revision_id 必须为非空字符串")
        if params.get("history_id") and params.get("revision_id"):
            return ToolResult(success=False, error="history_id 和 revision_id 只能指定一个")
        if params.get("history_id"):
            data = history.get_unit(params["history_id"])
        elif params.get("revision_id"):
            offset, limit = params.get("offset", 1), params.get("limit", 200)
            if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 2000:
                return ToolResult(success=False, error="offset 必须为正整数；limit 必须为1到2000的整数")
            revision = history.get_revision(params["revision_id"])
            if revision is None:
                return ToolResult(success=False, error="正文版本缺失", data=json.dumps({
                    "status": "missing", "revision_id": params["revision_id"],
                }, ensure_ascii=False))
            lines = revision["body_md"].splitlines(keepends=True)
            end = min(offset - 1 + limit, len(lines))
            data = {"status": "available", "revision_id": revision["revision_id"],
                    "path": revision["path"], "version_no": revision["version_no"],
                    "previous_revision_id": revision["parent_revision_id"],
                    "body_sha256": revision["body_sha256"], "offset": offset,
                    "total_lines": len(lines), "body": "".join(lines[offset - 1:end]),
                    "next_offset": end + 1 if end < len(lines) else None}
        else:
            return ToolResult(success=False, error="需要 history_id 或 revision_id")
        return ToolResult(success=bool(data), data=json.dumps(data, ensure_ascii=False) if data else None,
                          error="" if data else "History 或正文版本不存在")

    def checkPermissions(self, params, context):
        return PermissionResult.ALLOW
