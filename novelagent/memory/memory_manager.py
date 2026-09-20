"""记忆总控"""

from pathlib import Path
import re
import yaml
from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager
from novelagent.memory.prefetcher import Prefetcher
from novelagent.memory.auto_memory import AutoMemory


class MemoryManager:
    PROJECT_INDEX_EXCLUDES = (
        "archive/", "feedback/", "workflow/", "review/", "agent/", "user_prefs.md",
    )

    def __init__(self, working_dir: str, llm_client=None, global_memory_dir: str | None = None, *,
                 bad_case_recorder=None, session_id: str = "", project_id: str = "",
                 source_trace_id: str = ""):
        self.working_dir = working_dir
        self.file_store = FileStore(working_dir)
        self.index_manager = IndexManager(self.file_store)
        agent_context = {
            "bad_case_recorder": bad_case_recorder, "session_id": session_id,
            "project_id": project_id, "source_trace_id": source_trace_id,
        }
        self.prefetcher = Prefetcher(self.index_manager, self.file_store, llm_client, **agent_context)
        self.auto_memory = AutoMemory(self.index_manager, self.file_store, llm_client, **agent_context)
        self.auto_interval = 5
        project_dir = Path(working_dir)
        self.global_memory_dir = Path(global_memory_dir) if global_memory_dir else project_dir.parent.parent / "global_memory"

    @property
    def global_user_path(self) -> Path:
        return self.global_memory_dir / "user.md"

    def init_global_memory(self):
        self.global_memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.global_user_path.exists():
            self.global_user_path.write_text(
                "---\ntype: global_user\ntags: []\nsummary: 跨项目用户偏好\nstatus: active\n---\n\n# 全局用户偏好\n",
                encoding="utf-8",
            )

    def _global_user_entries(self) -> list[dict]:
        if not self.global_user_path.exists():
            return []
        text = self.global_user_path.read_text(encoding="utf-8")
        pattern = re.compile(
            r"^## \[user\] (?P<title>.+?)\n<!-- memory-entry\n(?P<meta>.*?)\n-->",
            re.MULTILINE | re.DOTALL,
        )
        entries = []
        for match in pattern.finditer(text):
            try:
                meta = yaml.safe_load(match.group("meta")) or {}
            except yaml.YAMLError:
                meta = {}
            entry_id = str(meta.get("id", "")).strip()
            if not entry_id:
                continue
            tags = meta.get("tags", [])
            entries.append({
                "type": "global_user",
                "tags": tags if isinstance(tags, list) else [],
                "summary": str(meta.get("summary") or match.group("title")).strip(),
                "weight": meta.get("weight", 60),
                "created": str(meta.get("created", "")),
                "updated": str(meta.get("updated", "")),
                "_path": f"global_memory/user.md#{entry_id}",
            })
        return entries

    def init_project_memory(self):
        """新建项目时初始化项目规则与参考绑定目录。"""
        memory_dir = Path(self.working_dir) / ".memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        for sub in ["user", "project", "reference", "agent", "references", "archive/legacy"]:
            (memory_dir / sub).mkdir(parents=True, exist_ok=True)
        rules_path = memory_dir / "project_rules.md"
        if not rules_path.exists():
            rules_path.write_text(
                "---\ntype: project_rules\ntags: []\nsummary: 当前项目的长期规则与修正\nstatus: active\n---\n\n# 项目规则与修正\n",
                encoding="utf-8",
            )
        self.init_global_memory()
        self.index_manager.rebuild(self.PROJECT_INDEX_EXCLUDES, self._global_user_entries())

    async def on_user_input(self, user_message: str) -> list[str] | None:
        return await self.prefetcher.fetch(user_message)

    def prepare_context(self, user_message: str) -> tuple[str, object]:
        """Read memory.md once and use that same snapshot for relevance prefetch."""
        memory_index = self.get_index_content()
        global_user = self.global_user_path.read_text(encoding="utf-8") if self.global_user_path.exists() else ""
        return memory_index, self.prefetcher.fetch(
            user_message,
            memory_index,
            {"global_memory/user.md": global_user} if global_user else {},
        )

    # async def on_round_complete(self, round_num: int, recent_messages: list):
    #     if self.auto_memory.should_trigger(round_num):
    #         self.auto_memory.run_background(recent_messages)  # 不阻塞主流程

    def rebuild_index(self) -> str:
        return self.index_manager.rebuild(self.PROJECT_INDEX_EXCLUDES, self._global_user_entries())

    def get_index_content(self) -> str:
        return self.index_manager.get_content()
