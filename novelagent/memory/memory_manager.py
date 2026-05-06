"""记忆总控"""

from pathlib import Path
from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager
from novelagent.memory.prefetcher import Prefetcher
from novelagent.memory.auto_memory import AutoMemory


class MemoryManager:
    def __init__(self, working_dir: str, llm_client=None):
        self.working_dir = working_dir
        self.file_store = FileStore(working_dir)
        self.index_manager = IndexManager(self.file_store)
        self.prefetcher = Prefetcher(self.index_manager, self.file_store, llm_client)
        self.auto_memory = AutoMemory(self.index_manager, self.file_store, llm_client)
        self.auto_interval = 5

    def init_project_memory(self):
        """新建项目时初始化.memory/目录（四大类型）"""
        memory_dir = Path(self.working_dir) / ".memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        for sub in ["user", "feedback", "reference", "project"]:
            (memory_dir / sub).mkdir(exist_ok=True)
        self.index_manager.rebuild()

    async def on_user_input(self, user_message: str) -> list[str] | None:
        return await self.prefetcher.fetch(user_message)

    # async def on_round_complete(self, round_num: int, recent_messages: list):
    #     if self.auto_memory.should_trigger(round_num):
    #         self.auto_memory.run_background(recent_messages)  # 不阻塞主流程

    def rebuild_index(self) -> str:
        return self.index_manager.rebuild()

    def get_index_content(self) -> str:
        return self.index_manager.get_content()
