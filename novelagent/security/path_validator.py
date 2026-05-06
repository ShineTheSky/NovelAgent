"""路径沙箱验证器"""

from pathlib import Path


class PathValidator:
    BLOCKED_PATHS = [
        # Linux
        "/etc", "/proc", "/sys", "/dev", "/boot", "/root",
        "/var/log", "/var/run", "/var/spool", "/var/tmp",
        "/usr/lib", "/usr/lib64", "/usr/bin", "/usr/sbin",
        "/lib", "/lib64", "/bin", "/sbin",
        # Windows
        "C:\\Windows", "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
        "C:\\Program Files", "C:\\Program Files (x86)",
        "C:\\ProgramData", "C:\\$Recycle.Bin",
        "C:\\System Volume Information",
        "C:\\Users", "C:\\Documents and Settings",
    ]

    PROTECTED_DIRS = [
        ".git",
        ".claude/settings",
        ".env",
        ".memory/memory.md",
    ]

    def __init__(self, working_dir: str):
        import os
        try:
            self.working_dir = os.path.realpath(working_dir)
        except Exception:
            self.working_dir = os.path.normpath(os.path.abspath(working_dir))

    def validate(self, path_str: str) -> bool:
        """检查路径是否安全（在工作目录内且不触及系统路径）"""
        import os
        full_path = Path(self.working_dir) / path_str
        try:
            resolved = os.path.realpath(str(full_path))
        except Exception:
            resolved = os.path.normpath(os.path.abspath(str(full_path)))

        wd = os.path.realpath(self.working_dir)
        resolved_lower = resolved.lower().replace("\\", "/")
        wd_lower = wd.lower().replace("\\", "/")

        # 路径逃逸检查（必须在 working_dir 内）
        if not resolved_lower.startswith(wd_lower):
            # 路径逃逸 → 再检查是否触及系统路径（双重保险）
            for blocked in self.BLOCKED_PATHS:
                blocked_norm = blocked.lower().replace("\\", "/")
                if resolved_lower.startswith(blocked_norm):
                    return False
            return False

        # 路径在工作目录内 → 无需检查系统路径黑名单（工作目录已受信任）
        return True

    def is_protected(self, path_str: str) -> bool:
        """检查是否触及保护目录"""
        norm = path_str.replace("\\", "/")
        for pd in self.PROTECTED_DIRS:
            pd_norm = pd.replace("\\", "/")
            if pd_norm in norm:
                return True
        return False
