"""Bash命令风险分类器"""

from enum import Enum


class BashRisk(Enum):
    BLOCKED = "blocked"  # 硬拒绝
    ASK = "ask"          # 需要用户确认
    ALLOW = "allow"      # 自动放行


class BashClassifier:
    ALLOW = ["wc", "cat", "ls", "find", "head", "tail", "sort", "uniq", "grep", "echo", "printf", "cut", "tr"]
    ASK = ["sed", "awk", "tee", "rm", "mv", "git"]
    BLOCKED = [
        "sudo", "shutdown", "reboot", "halt", "dd", "mkfs", "fdisk", "format", "diskpart", "fsutil",
        "kill", "pkill", "taskkill", "killall",
        "chmod", "chown", "chgrp", "cacls", "icacls", "setfacl", "getfacl",
        "apt", "apt-get", "yum", "dnf", "pacman", "pip", "pip3", "npm", "brew", "gem", "cargo",
        "gcc", "g++", "make", "cmake", "javac", "rustc",
        "eval", "exec", "source", ".",
        "nc", "ncat", "netcat", "socat", "tcpdump", "nmap", "telnet",
        "su", "runas", "doas", "pkexec", "systemctl", "service", "sc",
    ]

    def classify(self, command: str) -> BashRisk:
        if not command or not command.strip():
            return BashRisk.BLOCKED

        parts = command.strip().split()
        if not parts:
            return BashRisk.BLOCKED

        # Extract all command names (handle pipes)
        all_cmds = []
        for segment in command.split("|"):
            seg_parts = segment.strip().split()
            if seg_parts:
                all_cmds.append(seg_parts[0].lower())

        # BLOCKED check first (highest priority)
        for cmd in all_cmds:
            if cmd in [b.lower() for b in self.BLOCKED]:
                return BashRisk.BLOCKED
        # Check for destructive git patterns
        cmd_lower = command.lower().strip()
        for pattern in ["git push --force", "git reset --hard", "git clean -fd", "git branch -d"]:
            if cmd_lower.startswith(pattern):
                return BashRisk.BLOCKED

        # ASK: contains redirect operators
        if ">" in command:
            return BashRisk.ASK

        # ASK: sed/awk/tee/rm/mv/git
        for cmd in all_cmds:
            if cmd in [a.lower() for a in self.ASK]:
                return BashRisk.ASK

        # ALLOW
        base_cmd = all_cmds[0] if all_cmds else ""
        if base_cmd in [a.lower() for a in self.ALLOW]:
            return BashRisk.ALLOW

        # Unknown → ASK (conservative)
        return BashRisk.ASK
