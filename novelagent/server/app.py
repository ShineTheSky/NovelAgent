"""FastAPI应用入口"""

import yaml
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from novelagent.storage.database import init as db_init
from novelagent.llm.client import LLMClient
from novelagent.llm.config_loader import LLMConfigLoader
from novelagent.tools.registry import ToolRegistry
from novelagent.tools.read import ReadTool
from novelagent.tools.write import WriteTool
from novelagent.tools.edit import EditTool
from novelagent.tools.glob import GlobTool
from novelagent.tools.grep import GrepTool
from novelagent.tools.bash import BashTool
from novelagent.tools.subagent_tool import SubAgentTool
from novelagent.tools.ask_user_question import AskUserQuestionTool
from novelagent.security.permission_checker import PermissionChecker
from novelagent.context.builder import ContextBuilder
from novelagent.memory.memory_manager import MemoryManager, PatternContextProvider
from novelagent.core.agent_loop import AgentLoop
from novelagent.core.subagent import SubAgentRunner
from novelagent.trace.store import TraceStore
from novelagent.trace.recorder import TraceRecorder
from novelagent.trace.analyzer import PostTurnAnalyzer
from novelagent.server.routes import projects, sessions, files, novel, settings, traces
from novelagent.server.routes import rag
from novelagent.rag.store import RagStore


def load_config() -> dict:
    config_path = Path("config/config.yaml")
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def get_project_root() -> Path:
    return Path(__file__).parent.parent.parent.resolve()

def resolve_working_dir(relative_path: str) -> str:
    """将相对路径解析为绝对路径（相对于项目根目录）"""
    import os
    return os.path.normpath(os.path.join(str(get_project_root()), relative_path))


def create_app() -> FastAPI:
    app = FastAPI(title="NovelAgent2")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cfg = load_config()
    working_dir = resolve_working_dir(cfg.get("working_dir", "./workspace"))
    session_cfg = cfg.get("session", {})
    agent_cfg = cfg.get("agent", {})
    security_cfg = cfg.get("security", {})
    bash_cfg = security_cfg.get("bash", {})

    # Initialize components
    llm_config_path = str(get_project_root() / "config" / "llm_config.yaml")
    llm_client = LLMClient(llm_config_path)

    registry = ToolRegistry()
    tools = [ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool(), BashTool(), SubAgentTool(), AskUserQuestionTool()]
    for tool in tools:
        registry.register(tool)

    permission_checker = PermissionChecker(working_dir)
    context_builder = ContextBuilder("prompts", token_limit=session_cfg.get("token_limit", 150_000))
    memory_manager = MemoryManager(working_dir, llm_client)
    trace_store = TraceStore()
    trace_recorder = TraceRecorder(trace_store)
    post_turn_analyzer = PostTurnAnalyzer(llm_client, working_dir, trace_store)
    preference_context_provider = PatternContextProvider(working_dir)
    rag_store = RagStore()

    agent_config = {
        **session_cfg,
        **agent_cfg,
        "working_dir": working_dir,
        "write_allow_rules": security_cfg.get("write_allow_rules", []),
    }

    subagent_runner = SubAgentRunner(llm_client, registry, permission_checker, context_builder, working_dir)
    # 注入已解析的预设路径
    for tool in registry.list_all():
        if tool.name == "SubAgent":
            tool.presets_path = str(get_project_root() / "config" / "subagent_presets.yaml")
            tool._presets_cache = None  # 清除旧缓存
    agent_loop = AgentLoop(
        llm_client, registry, permission_checker, context_builder, memory_manager, agent_config, subagent_runner,
        trace_recorder, post_turn_analyzer, preference_context_provider,
        rag_store,
    )

    # Inject into app state
    app.state.agent_loop = agent_loop
    app.state.registry = registry
    app.state.memory_manager = memory_manager
    app.state.trace_store = trace_store
    app.state.config = cfg
    app.state.llm_config_path = llm_config_path
    app.state.rag_store = rag_store

    # Register routes
    app.include_router(projects.router)
    app.include_router(sessions.router)
    app.include_router(files.router)
    app.include_router(novel.router)
    app.include_router(settings.router)
    app.include_router(traces.router)
    app.include_router(rag.router)

    # Startup
    @app.on_event("startup")
    async def startup():
        await db_init()

    return app


app = create_app()
