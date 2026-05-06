"""工具标准协议"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PermissionResult(Enum):
    ALLOW = "allow"              # 跳过确认，直接执行
    ASK = "ask"                  # 弹窗等待用户确认
    BLOCK = "block"              # 硬拒绝，无确认入口
    PASSTHROUGH = "passthrough"  # 回退到上一层（1b风险评估结果）


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str = ""

    def to_dict(self) -> dict:
        return {"success": self.success, "data": self.data, "error": self.error}


@dataclass
class ToolContext:
    session_id: str
    project_id: str
    working_dir: str
    accept_edits_mode: bool = False
    allow_rules: list[dict] = field(default_factory=list)
    previously_allowed: set[str] = field(default_factory=set)

    def is_previously_allowed(self, key: str) -> bool:
        return key in self.previously_allowed

    def mark_allowed(self, key: str):
        self.previously_allowed.add(key)


class ToolProtocol(ABC):
    """工具标准协议 — 所有工具必须继承此类"""
    name: str
    description: str
    parameters: dict  # JSON Schema

    @abstractmethod
    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        """执行工具"""
        ...

    def validate_params(self, params: dict) -> bool:
        """参数校验：检查必填字段"""
        required = self.parameters.get("required", [])
        for key in required:
            if key not in params:
                return False
        return True

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        """1c: 工具自主判断是否允许。默认回退到1b风险评估"""
        return PermissionResult.PASSTHROUGH

    def get_schema(self) -> dict:
        """生成工具JSON Schema"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
