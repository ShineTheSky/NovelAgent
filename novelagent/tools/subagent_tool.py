"""SubAgent工具"""

import yaml
from pathlib import Path
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class SubAgentTool(ToolProtocol):
    name = "SubAgent"
    description = "创建并运行一个子Agent执行特定任务。可用预设: chapter_writer(撰写章节)、chapter_polisher(润色章节)、reviewer(评审章节)、character_designer(设计角色)、outliner(规划大纲)。"

    parameters = {
        "type": "object",
        "properties": {
            "preset": {"type": "string", "description": "预设名称。可用: chapter_writer, chapter_polisher, reviewer, character_designer, outliner, memory_extractor"},
            "task": {"type": "string", "description": "分配给子Agent的任务描述"},
            "inherit_history": {"type": "boolean", "description": "覆盖预设的inherit_history设置"},
            "attachments": {
                "type": "array",
                "description": "提供给子Agent的参考文件内容列表，必须将你认为subagent会用到的文件以及路径传入，不能假设subagent可以自己获取。每个元素含 path(文件路径) 和 content(文件内容)。子Agent应优先使用附件，不需要再读取这些文件。",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "content": {"type": "string", "description": "文件内容"},
                    },
                },
            },
            "show_result": {"type": "boolean", "description": "是否将子Agent完整返回结果直接展示到客户端对话区"},
        },
        "required": ["preset", "task"],
    }

    def __init__(self, presets_path: str = "config/subagent_presets.yaml"):
        self.presets_path = presets_path
        self._presets_cache = None

    def _load_presets(self) -> dict:
        if self._presets_cache is None:
            with open(self.presets_path, encoding="utf-8") as f:
                self._presets_cache = yaml.safe_load(f)
            print(f"[subagent] 加载预设: {list(self._presets_cache.get('presets', {}).keys())}", flush=True)
        return self._presets_cache

    def get_preset(self, preset_name: str) -> dict | None:
        presets = self._load_presets()
        return presets.get("presets", {}).get(preset_name)

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        preset_name = params["preset"]
        preset = self.get_preset(preset_name)
        if preset is None:
            available = list(self._load_presets().get("presets", {}).keys())
            return ToolResult(success=False, error=f"预设 '{preset_name}' 不存在。可用: {available}")

        # 返回预设配置和任务，由 Agent Loop 层的 subagent.py 完成实际创建和执行
        return ToolResult(success=True, data={
            "type": "subagent_spawn",
            "preset": preset_name,
            "task": params["task"],
            "config": preset,
            "inherit_history": params.get("inherit_history", preset.get("inherit_history", True)),
        })

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW  # 子Agent创建无需确认
