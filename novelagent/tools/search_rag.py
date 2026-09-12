"""Shared reference-library search tool for the chapter polisher."""

from novelagent.tools.base import PermissionResult, ToolContext, ToolProtocol, ToolResult


class SearchRagTool(ToolProtocol):
    name = "SearchRag"
    description = "检索全局参考资料库，返回与当前润色或扩写目标相关的片段。结果仅可借鉴表现手法，禁止复用原句、人物或情节。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "结合当前剧情、润色目标和所需表现手法编写的检索词"},
            "limit": {"type": "integer", "description": "返回片段数量，1 到 5，默认 3"},
        },
        "required": ["query"],
    }

    def __init__(self, rag_store):
        self.rag_store = rag_store

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        limit = max(1, min(int(params.get("limit", 3)), 5))
        results = await self.rag_store.search(params["query"], limit)
        if not results:
            return ToolResult(success=True, data="(资料库中没有相关片段)")
        lines = ["[参考资料检索结果]"]
        for index, result in enumerate(results, 1):
            lines.append(f"\n### 片段 {index}｜{result['title']}\n{result['content']}")
        return ToolResult(success=True, data="\n".join(lines))

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW
