"""三层权限检查器"""

from novelagent.tools.base import ToolProtocol, ToolContext, PermissionResult
from novelagent.security.path_validator import PathValidator
from novelagent.security.bash_classifier import BashClassifier, BashRisk


class PermissionChecker:
    def __init__(self, working_dir: str):
        self.path_validator = PathValidator(working_dir)
        self.bash_classifier = BashClassifier()

    def check(self, tool: ToolProtocol, params: dict, context: ToolContext) -> PermissionResult:
        # 1a. checkRuleBasedPermissions — 静态规则
        block_result = self._check_static_rules(tool, params, context)
        if block_result == PermissionResult.BLOCK:
            return PermissionResult.BLOCK

        # 1b. estimateRisk — 风险评估
        risk_result = self._estimate_risk(tool, params, context)
        if risk_result == PermissionResult.BLOCK:
            return PermissionResult.BLOCK

        # 1c. tool.checkPermissions — 工具自主判断
        tool_result = tool.checkPermissions(params, context)
        if tool_result == PermissionResult.PASSTHROUGH:
            return risk_result  # 回退到 1b 结果
        return tool_result

    def _check_static_rules(self, tool: ToolProtocol, params: dict, context: ToolContext) -> PermissionResult:
        # 路径工具（Read/Write/Edit）：检查路径
        if tool.name in ("Read", "Write", "Edit"):
            path = params.get("path", "")
            if path and not self.path_validator.validate(path):
                return PermissionResult.BLOCK
        # Bash: 检查路径
        if tool.name == "Bash":
            # 路径检查留在 1b estimateRisk 中处理
            pass
        return PermissionResult.PASSTHROUGH

    def _estimate_risk(self, tool: ToolProtocol, params: dict, context: ToolContext) -> PermissionResult:
        if tool.name == "Bash":
            risk = self.bash_classifier.classify(params.get("command", ""))
            if risk == BashRisk.BLOCKED:
                return PermissionResult.BLOCK
            if risk == BashRisk.ASK:
                return PermissionResult.ASK
            return PermissionResult.ALLOW
        return PermissionResult.PASSTHROUGH
