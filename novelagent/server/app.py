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
from novelagent.tools.text_stats import TextStatsTool
from novelagent.tools.check_content import CheckContentTool
from novelagent.tools.bash import BashTool
from novelagent.tools.subagent_tool import SubAgentTool
from novelagent.tools.ask_user_question import AskUserQuestionTool
from novelagent.tools.create_trace_checkpoint import CreateTraceCheckpointTool
from novelagent.tools.search_rag import SearchRagTool
from novelagent.security.permission_checker import PermissionChecker
from novelagent.context.builder import ContextBuilder
from novelagent.memory.memory_manager import MemoryManager
from novelagent.core.agent_loop import AgentLoop
from novelagent.core.subagent import SubAgentRunner
from novelagent.trace.store import TraceStore
from novelagent.trace.recorder import TraceRecorder
from novelagent.trace.agent_run import AgentTraceTaskManager
from novelagent.trace.file_analyzer import FileTraceAnalyzer, FilePatternContextProvider
from novelagent.trace.agent_bad_cases import AgentBadCaseRecorder
from novelagent.trace.bad_case_analyzer import BadCaseAnalyzer
from novelagent.trace.bash_cases import BashCaseRecorder
from novelagent.trace.bash_case_analyzer import BashCaseAnalyzer
from novelagent.trace.embedding_gate import EmbeddingGate
from novelagent.embeddings.local_model import LocalEmbeddingModel
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
    embedding_cfg = cfg.get("embedding", {})
    security_cfg = cfg.get("security", {})
    bash_cfg = security_cfg.get("bash", {})
    case_analysis_cfg = cfg.get("case_analysis", {})

    # Initialize components
    llm_config_path = str(get_project_root() / "config" / "llm_config.yaml")
    llm_client = LLMClient(llm_config_path)

    embedding_model = LocalEmbeddingModel(str(get_project_root() / embedding_cfg.get("model_path", "models/bge-base-zh-v1.5")))
    rag_store = RagStore(embedding_model)
    trace_store = TraceStore()
    bash_case_analyzer = BashCaseAnalyzer(
        llm_client, working_dir, case_analysis_cfg, trace_store,
    )
    bash_case_recorder = BashCaseRecorder(working_dir, bash_case_analyzer)
    registry = ToolRegistry()
    for tool in [ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool(), TextStatsTool(), CheckContentTool(), BashTool(
        bash_case_recorder,
        command_timeout=bash_cfg.get("command_timeout", 30),
        max_output_size=bash_cfg.get("max_output_size", 10240),
    ), SubAgentTool(), AskUserQuestionTool(), CreateTraceCheckpointTool(), SearchRagTool(rag_store)]:
        registry.register(tool)

    permission_checker = PermissionChecker(working_dir)
    context_builder = ContextBuilder("prompts", token_limit=session_cfg.get("token_limit", 150_000))
    memory_manager = MemoryManager(working_dir, llm_client)
    trace_recorder = TraceRecorder(trace_store)
    agent_trace_tasks = AgentTraceTaskManager()
    bad_case_analyzer = BadCaseAnalyzer(
        llm_client, working_dir, case_analysis_cfg, trace_store,
    )
    bad_case_recorder = AgentBadCaseRecorder(trace_store, working_dir, bad_case_analyzer)
    bash_case_analyzer.bad_case_recorder = bad_case_recorder
    embedding_gate = EmbeddingGate(
        str(get_project_root() / embedding_cfg.get("model_path", "models/bge-base-zh-v1.5")),
        enabled=embedding_cfg.get("enabled", True),
        routine_min_similarity=float(embedding_cfg.get("routine_min_similarity", 0.70)),
        routine_min_margin=float(embedding_cfg.get("routine_min_margin", 0.12)),
    )
    embedding_gate.embedding_model = embedding_model
    post_turn_analyzer = FileTraceAnalyzer(
        llm_client, working_dir, trace_store, embedding_gate, rag_store, bad_case_recorder,
        agent_trace_tasks,
    )
    preference_context_provider = FilePatternContextProvider(working_dir)

    agent_config = {
        **session_cfg,
        **agent_cfg,
        "working_dir": working_dir,
        "global_memory_dir": str(get_project_root() / cfg.get("memory", {}).get("global_dir", "global_memory")),
        "write_allow_rules": security_cfg.get("write_allow_rules", []),
    }

    subagent_runner = SubAgentRunner(
        llm_client, registry, permission_checker, context_builder, working_dir, bad_case_recorder,
        agent_trace_tasks,
    )
    # 注入已解析的预设路径
    for tool in registry.list_all():
        if tool.name == "SubAgent":
            tool.presets_path = str(get_project_root() / "config" / "subagent_presets.yaml")
            tool._presets_cache = None  # 清除旧缓存
    agent_loop = AgentLoop(
        llm_client, registry, permission_checker, context_builder, memory_manager, agent_config, subagent_runner,
        trace_recorder, post_turn_analyzer, preference_context_provider,
        rag_store, bash_case_recorder, bad_case_analyzer, agent_trace_tasks,
    )

    # Inject into app state
    app.state.agent_loop = agent_loop
    app.state.registry = registry
    app.state.memory_manager = memory_manager
    app.state.trace_store = trace_store
    app.state.agent_trace_tasks = agent_trace_tasks
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
        agent_trace_tasks.submit(
            post_turn_analyzer.recover_pending_windows(), label="trace-window-recovery",
        )

    @app.on_event("shutdown")
    async def shutdown():
        await agent_trace_tasks.drain()

    return app


app = create_app()
