"""ToolRegistry — 全局工具注册表"""

from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext


class ToolNotFoundError(Exception): pass
class ToolAlreadyRegisteredError(Exception): pass


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolProtocol] = {}

    def register(self, tool: ToolProtocol):
        if tool.name in self._tools:
            raise ToolAlreadyRegisteredError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolProtocol:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' not found. Available: {list(self._tools.keys())}")
        return self._tools[name]

    def list_all(self) -> list[ToolProtocol]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict]:
        return [t.get_schema() for t in self._tools.values()]

    def get_tools_prompt(self) -> str:
        lines = ["## 可用工具"]
        for tool in self._tools.values():
            params_desc = self._format_parameters(tool.parameters)
            lines.append(f"\n### {tool.name}")
            lines.append(f"{tool.description}")
            if params_desc:
                lines.append(f"参数:\n{params_desc}")
        return "\n".join(lines)

    async def execute(self, name: str, params: dict, context: ToolContext) -> ToolResult:
        tool = self.get(name)
        if not tool.validate_params(params):
            required = tool.parameters.get("required", [])
            missing = [k for k in required if k not in params]
            return ToolResult(success=False, error=f"缺少必填参数: {missing}")
        return await tool.execute(params, context)

    def _format_parameters(self, params: dict) -> str:
        properties = params.get("properties", {})
        required = params.get("required", [])
        lines = []
        for name, prop in properties.items():
            req_mark = " (必填)" if name in required else ""
            lines.append(f"  - {name}: {prop.get('type', 'string')}{req_mark} — {prop.get('description', '')}")
        return "\n".join(lines)
