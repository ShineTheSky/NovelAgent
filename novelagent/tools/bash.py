"""Bash工具"""

import os
import sys
import shutil
import subprocess
import hashlib
from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class BashTool(ToolProtocol):
    name = "Bash"
    description = "在项目工作目录下执行Shell命令。用于字数统计、目录浏览(ls/find)、文本处理等。所有命令执行前经过安全层校验。"

    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的Shell命令"},
            "working_dir": {"type": "string", "description": "工作目录，默认项目根目录"},
        },
        "required": ["command"],
    }

    # 风险分类
    ALLOW_COMMANDS = ["wc", "cat", "ls", "find", "head", "tail", "sort", "uniq", "grep", "echo", "printf", "cut", "tr"]
    ASK_COMMANDS = ["sed", "awk", "tee", "rm", "mv", "git"]
    BLOCKED_COMMANDS = [
        "sudo", "shutdown", "reboot", "halt", "dd", "mkfs", "fdisk", "format",
        "kill", "pkill", "taskkill", "chmod", "chown", "apt", "apt-get", "yum",
        "dnf", "pacman", "pip", "pip3", "npm", "brew", "gcc", "g++", "make",
        "eval", "exec", "source", "nc", "ncat", "netcat", "socat", "tcpdump",
        "nmap", "telnet", "su", "runas", "doas", "pkexec", "systemctl", "service",
    ]

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        command = params["command"]
        cwd = os.path.abspath(params.get("working_dir", context.working_dir))

        try:
            # Windows: use Git Bash if available, otherwise fall back to cmd
            if sys.platform == 'win32':
                bash_path = shutil.which('bash') or r'E:\Program Files\Git\bin\bash.exe'
                if os.path.exists(bash_path):
                    result = subprocess.run(
                        [bash_path, '-c', command],
                        capture_output=True, text=True,
                        timeout=30, cwd=cwd, encoding='utf-8', errors='replace'
                    )
                else:
                    result = subprocess.run(
                        command, shell=True, capture_output=True, text=True,
                        timeout=30, cwd=cwd, encoding='utf-8', errors='replace'
                    )
            else:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=30, cwd=cwd, encoding='utf-8', errors='replace'
                )
            output = (result.stdout or '') + (result.stderr or '')
            # 输出截断
            max_size = 10240
            if len(output) > max_size:
                output = output[:max_size] + f"\n[输出截断，超过{max_size}字节]"
            return ToolResult(success=(result.returncode == 0), data=output.strip() or "(无输出)")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Bash命令超时（30s）")

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        command = params.get("command", "").strip()
        if not command:
            return PermissionResult.BLOCK

        base_cmd = command.split()[0].lower()

        # 检查管道中的命令
        all_cmds = []
        for part in command.replace("|", " ").split():
            c = part.strip().lower()
            if c and not c.startswith("-") and not c.startswith(">"):
                all_cmds.append(c)

        # BLOCKED 优先检查
        for cmd in all_cmds:
            if cmd in [b.lower() for b in self.BLOCKED_COMMANDS]:
                return PermissionResult.BLOCK
            # 完整匹配检查（如 "git push --force"）
            if any(command.lower().startswith(b) for b in ["git push --force", "git reset --hard", "git clean -fd"]):
                return PermissionResult.BLOCK

        # 会话内已批准 → ALLOW
        cmd_hash = hashlib.md5(command.encode()).hexdigest()[:8]
        if context.is_previously_allowed(f"bash:{cmd_hash}"):
            return PermissionResult.ALLOW

        # ASK: 含重定向符
        if ">" in command:
            return PermissionResult.ASK

        # ASK: sed/awk/tee/rm/mv/git
        for cmd in all_cmds:
            if cmd in [a.lower() for a in self.ASK_COMMANDS]:
                return PermissionResult.ASK

        # ALLOW: 纯只读命令
        if base_cmd in [a.lower() for a in self.ALLOW_COMMANDS]:
            return PermissionResult.ALLOW

        # 未知命令 → ASK
        return PermissionResult.ASK
