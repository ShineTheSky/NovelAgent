# NovelAgent2

面向小说创作的 7 层 AI Agent 系统。用户通过自然语言描述创作意图，Agent 自主规划、调用工具、管理上下文和长期记忆，完成从章节大纲到正文撰写的全流程创作任务。

当前版本还提供：全局共享资料库、BM25 + 本地 Embedding 混合检索、主/子 Agent 过程回放、Trace → Insight → Memory → Pattern 的可追溯记忆生命周期，以及独立的 Agent 失败案例归档。

## 架构总览

```
Web UI (React) ← SSE → FastAPI Server → Agent Loop (ReAct)
                                            │
              ┌─────────────────────────────┼──────────────────────────┐
              │                │            │              │           │
         工具层 (10 tools)  安全层 (3层)   上下文层      洞察/记忆层  LLM客户端
         Read/Write/Edit   1a静态规则     Prompt模板    FileStore    多Provider
         Glob/Grep/Bash    1b风险评估     Token计算     IndexManager  Anthropic
         SubAgent/SearchRag 1c自主判断    消息管理      Trace分析    DeepSeek
         AskUserQuestion                  上下文压缩    Pattern注入  OpenAI..
```

| 层级 | 职责 |
|------|------|
| 第1层 Web UI | 对话与小说分屏、流式过程渲染、资料库、项目洞察、弹窗问答 |
| 第2层 入口传输 | 启动初始化、请求标准化、会话管理、SSE 管道、过程事件持久化与回放 |
| 第3层 Agent Loop | ReAct 循环、子 Agent 编排、审阅/润色工作流、Trace 触发、工具调用 |
| 第4层 工具层 | Read / Write / Edit / Glob / Grep / Bash / SubAgent / AskUserQuestion / CreateTraceCheckpoint / SearchRag |
| 第5层 安全层 | 三层权限检查、Bash命令两级分类、路径沙箱、审计 |
| 第6层 上下文层 | System Prompt 管理、消息列表、Token 计算、压缩、Pattern 和相关记忆注入 |
| 第7层 洞察/记忆层 | SQLite 不可变 Trace、文件化 Insight/Memory/Pattern、异步分析与索引 |
| 参考资料层 | 全局共享文章导入、段落/句子分块、BM25 + 本地向量混合检索、按工具调用 |

## 快速开始

### 环境要求

- Python >= 3.11
- Node.js >= 18

### 安装

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 下载本地 Embedding 模型（首次使用 RAG/Trace 分类前执行）
# 模型权重保存在 models/，已被 .gitignore 排除，不会提交到仓库。
huggingface-cli download BAAI/bge-base-zh-v1.5 --local-dir models/bge-base-zh-v1.5

# 3. 配置 API key
cp .env.example .env
# 编辑 .env，填入你的 LLM API key

# 4. 启动后端
uvicorn novelagent.server.app:app --reload --port 8000

# 5. 安装前端依赖（仅首次）
cd web && npm install && cd ..

# 6. 启动前端
cd web && npm run dev
```

打开 `http://localhost:5173` 开始使用。

### 仅后端

```bash
uvicorn novelagent.server.app:app --reload --port 8000
# API 文档: http://localhost:8000/docs
```

## 配置

### 配置文件

| 文件 | 说明 | 提交 git |
|------|------|---------|
| `config/config.yaml` | 工作目录、安全策略、会话上限、token限制 | 是 |
| `config/llm_config.yaml` | LLM provider 连接 + 各位置模型配置 | 是 |
| `config/subagent_presets.yaml` | 子Agent预设（内嵌Prompt） | 是 |
| `.env` | API key 实际值 | **否** |

### LLM 配置

`config/llm_config.yaml` 支持为每个调用位置独立配置 provider 和 model：

```yaml
positions:
  main_loop:           # 主Agent → 最强模型
    provider: deepseek
    model: deepseek-v4-pro[1m]
    timeout: 300

  sub_agent:           # 子Agent → 可按预设覆写
    provider: deepseek
    model: deepseek-v4-pro[1m]
    overrides:
      chapter_writer:  # 章节撰写 → 可独立用更强模型
        provider: minimax
        model: MiniMax-M2.7

  memory_prefetch:     # 记忆预取 → 最快模型
    provider: minimax
    model: MiniMax-M2.7
    timeout: 1.5

  context_compression: # 上下文压缩 → 长文本
    provider: minimax
    model: MiniMax-M2.7

  auto_memory:         # Trace 分析（无对话分支时）→ 最便宜
    provider: minimax
    model: MiniMax-M2.7
```

支持的 provider：Anthropic, OpenAI, DeepSeek, MiniMax, OpenRouter, Azure OpenAI, 自部署 (vLLM 兼容)。

每个 position 可独立配置 `provider`, `model`, `max_tokens`, `temperature`, `timeout`, `retry`。DeepSeek 的 `reasoning_content`（思考链）会自动捕获并在后续请求中回传。

### 子Agent预设

`config/subagent_presets.yaml` 定义6种子Agent：

| 预设 | 工具 | 继承历史 | max_turns | 用途 |
|------|------|---------|-----------|------|
| `chapter_writer` | Read, Write, Edit, Glob, Grep, Bash | 否 | 15 | 章节撰写 |
| `chapter_polisher` | Read, Write, Edit, Glob, Grep, SearchRag | 否 | 12 | 润色章节 |
| `reviewer` | Read, Glob, Grep, AskUserQuestion | 否 | 10 | 独立审阅 |
| `character_designer` | Read, Write, Edit, Glob, Grep | 否 | 12 | 人物设定 |
| `outliner` | Read, Write, Glob, Grep, Bash, AskUserQuestion, Edit | 否 | 12 | 大纲规划 |
| `memory_extractor` | Read, Write, Grep, Glob | 是 | 6 | 保留的手动提取预设；默认生命周期由后台 Trace 分析处理 |

### Write 权限规则

Write/Edit 默认弹窗确认。以下情况自动放行：

1. 路径匹配 `write_allow_rules`（如 `chapters/**`）
2. acceptEdits 模式开启
3. `.memory/` 目录（记忆系统免确认）
4. 内部可编辑文件（`.claude/plans/`, `.claude/scratchpad.md`）
5. 会话内同路径已批准过

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/projects` | GET | 项目列表 |
| `/api/projects` | POST | 创建项目 |
| `/api/projects/{id}/sessions` | GET | 会话列表 |
| `/api/projects/{id}/sessions` | POST | 创建会话 |
| `/api/sessions/{id}` | GET | 会话详情 |
| `/api/sessions/{id}` | DELETE | 删除会话 |
| `/api/sessions/{id}/message` | POST | 发送消息 (SSE流) |
| `/api/sessions/{id}/stop` | POST | 中断生成 |
| `/api/sessions/{id}/compress` | POST | 手动压缩上下文 |
| `/api/sessions/{id}/permission-response` | POST | 权限确认响应 |
| `/api/sessions/{id}/question-response` | POST | AskUserQuestion 问答响应 |
| `/api/sessions/{id}/accept-edits` | POST | 切换 acceptEdits 模式 |
| `/api/rag/documents` | GET/POST | 查看或导入全局共享参考文章（TXT/Markdown 文本） |
| `/api/rag/documents/{document_id}` | DELETE | 删除参考文章及其分块 |
| `/api/rag/documents/{document_id}/chunks` | GET | 分页查看资料分块 |
| `/api/rag/embeddings/rebuild` | POST | 后台补齐尚未生成的本地向量 |
| `/api/rag/embeddings/rebuild/{job_id}` | GET | 查询向量构建进度 |
| `/api/rag/search?q=...` | GET | 手动检索参考片段 |
| `/api/projects/{id}/insights` | GET | 项目的低置信洞察 |
| `/api/projects/{id}/memories` | GET | 项目的原子记忆 |
| `/api/projects/{id}/patterns` | GET | 项目的稳定模式 |
| `/api/projects/{id}/{layer}/{record_id}/downgrade` | POST | 用户确认后降低 Memory / Pattern 层级 |

### 参考资料库（RAG）

在侧栏“资料库”可导入 TXT 或 Markdown 文章。资料库在所有项目之间共享，系统优先按段落切分为约 900 字的小块；过长段落会在完整句子边界切分，重叠内容也只取完整句子。导入后，Embedding 在后台分批生成，前端显示模型加载、处理和完成进度。

资料库采用本地 **BM25 + Embedding** 混合检索：BM25 负责关键词命中，`BAAI/bge-base-zh-v1.5` 负责语义相似度。两项分数都会显示在前端检索结果中；不需要第三方 embedding API，也不需要向量数据库。

RAG 不会在每轮对话自动注入。Agent 在需要扩写、改写或仿写表现手法时，通过 `SearchRag` 工具主动检索，返回结果仅可借鉴写法，不能复用原句、人物或情节。

Embedding 模型路径由 `config/config.yaml` 的 `embedding.model_path` 指定，默认是 `models/bge-base-zh-v1.5`。服务仅加载本地模型（不会在运行时下载）；首次部署请先执行快速开始中的 `huggingface-cli download` 命令。模型权重、RAG 索引和本地数据库均在 `.gitignore` 中排除。

### Trace、项目洞察与长期记忆

每轮会话的工具调用、结果、错误和最终回答会被记录为可脱敏回溯的原始事件。系统默认每 5 个会话轮次聚合为一个 Trace；主 Agent 可通过 `CreateTraceCheckpoint` 主动触发，且上下文压缩前会强制捕获尚未聚合的内容。Trace 分析在后台分支异步进行，不阻塞当前回复；Embedding 会先过滤“继续写作”等常规操作，降低不必要的 LLM 调用。

Trace 本身是 SQLite 中不可变的证据；分析结果以 Markdown + YAML frontmatter 写入项目目录：

| 层级 | 路径 | 含义 |
|------|------|------|
| Insight | `.insight/{user,project,reference,agent}/` | 从 Trace 推导出的单次、较弱或尚待验证的观察与假设 |
| Memory | `.memory/{user,project,reference,agent}/` | 明确、可复用的原子偏好、修正或约束 |
| Pattern | `.pattern/{user,project,reference,agent}/` | 多条 Memory 支持后形成的稳定模式 |

- 每条派生记录保留 `trace_id`、`source_event_ids`、权重、更新时间和关联关系，便于回溯。
- 相似 Insight 低幅加分；满足门槛后可升为 Memory。Memory 可以同时与旧 Insight 合并，并在至少 3 条支持、权重达到 200 时升为 Pattern；权重上限为 300。
- 活跃 Memory 索引最多 200 条。容量淘汰只会将旧记录退回 Insight，不会删除对应 Trace 证据；Pattern 不会因时间自然降级。
- Pattern 全文会注入 System Prompt；Memory 标题进入 `memory.md` 滑动索引，命中后再将正文作为附件注入。
- 用户可在“项目洞察”中将 Memory 或 Pattern 降级。降级备注会留在正文，后续相似反馈只能低幅恢复，除非用户明确再次确认。
- `agent` 分类只用于优化工具和 Agent 调度，不进入小说写作上下文或项目洞察名额。工具/Agent 执行失败会额外写入全局 `data/agent_bad_cases/`，并保留脱敏参数与近期上下文，便于复现。
- 每次 Bash 请求（正常执行、安全策略拦截或用户拒绝）都会在全局 `data/bash_cases/YYYY-MM-DD/` 留下一条脱敏 JSON Case，记录命令、权限决策、结果摘要、耗时及会话/项目来源；它用于后续识别高频命令并收敛为可监控工具，不进入项目记忆。Bash Case 是行为样本，不是错误：`outcome` 明确区分 `succeeded`、`failed`、`blocked` 和 `not_executed`；仅实际执行失败才会额外进入 Agent Bad Case。所有 Bash Case 会按命令族聚合；同一命令族积累 3 条样本后，`bash_case_analysis` 会将成功与失败一起分析，生成风险和待审核的受监控工具候选，写入 `data/bash_cases/analysis/{command_family}.md`。
- Agent Bad Case 先以规则分类为 `llm_api`、`llm_timeout`、`tool_permission`、`tool_execution`、`bash_execution`、`subagent`、`workflow` 或 `agent_execution`。同类样本累计到 3 条时，后台 `bad_case_analysis` 路由会异步调用 LLM，生成“可能根因 + 证据 Case ID + 待审核优化建议”，写入 `data/agent_bad_cases/analysis/{category}.md`。分析不会自动修改 Prompt、路由或工具。

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `text_delta` | LLM 流式文本输出 |
| `thinking` | DeepSeek 思考链内容 |
| `tool_call` | 工具调用 |
| `tool_result` | 工具执行结果 |
| `permission_ask` | 权限确认弹窗 |
| `question_ask` | 用户问答弹窗 |
| `subagent_start` | 子 Agent 过程卡片开始 |
| `subagent_result` | 子 Agent 返回阶段性结果 |
| `subagent_done` | 子Agent完成 |
| `done` | 本轮回复结束 |
| `error` | 错误信息 |

## 开发

### 项目结构

```
NovelAgent2/
├── config/                    # 配置文件
│   ├── config.yaml
│   ├── llm_config.yaml
│   └── subagent_presets.yaml
├── prompts/                   # Jinja2 Prompt模板
│   ├── base_system.j2         # 主Agent系统提示
│   ├── base_subagent.j2       # 子Agent系统提示
│   ├── partials/              # 可复用片段
│   └── dynamic/
├── novelagent/                # Python 后端
│   ├── server/                # FastAPI Web 服务
│   │   ├── app.py
│   │   └── routes/
│   │       ├── projects.py
│   │       └── sessions.py
│   ├── core/                  # Agent Loop + 子Agent + 共享turn
│   │   ├── agent_loop.py      # 主Agent ReAct循环
│   │   ├── subagent.py        # 子Agent执行器
│   │   ├── llm_turn.py        # query() 共享ReAct循环 + 消息构建
│   │   └── session.py         # 会话/请求/响应数据类
│   ├── tools/                 # 工具层（含 Trace 与 RAG 工具）
│   │   ├── base.py            # ToolProtocol / ToolResult / ToolContext
│   │   ├── registry.py        # ToolRegistry
│   │   ├── read.py, write.py, edit.py
│   │   ├── glob.py, grep.py, bash.py
│   │   ├── subagent_tool.py   # SubAgent 工具定义
│   │   └── ask_user_question.py  # AskUserQuestion 工具定义
│   ├── security/              # 安全层
│   │   ├── permission_checker.py
│   │   ├── bash_classifier.py
│   │   ├── path_validator.py
│   │   └── audit.py
│   ├── context/               # 上下文层
│   │   ├── builder.py         # ContextBuilder
│   │   ├── prompt_manager.py  # Jinja2模板管理
│   │   ├── message_manager.py # Message数据类
│   │   └── token_counter.py   # tiktoken计数
│   ├── memory/                # 记忆层
│   │   ├── memory_manager.py  # 记忆总控
│   │   ├── file_store.py      # 文件读写
│   │   ├── index_manager.py   # memory.md索引
│   │   ├── prefetcher.py      # 异步预取
│   │   └── auto_memory.py     # 旧的自动记忆实现（当前由 Trace 分析替代）
│   ├── trace/                 # 不可变 Trace、后台分析、Insight/Memory/Pattern 生命周期
│   ├── rag/                   # 全局资料库、分块、BM25 + Embedding 混合检索
│   ├── embeddings/            # 本地 SentenceTransformer 模型封装
│   ├── llm/                   # LLM 客户端
│   │   ├── client.py          # LLMClient (多provider, 流式, 重试)
│   │   ├── config_loader.py   # LLMConfigLoader
│   │   └── stream.py          # SSE解析
│   └── storage/               # SQLite持久化
│       ├── database.py
│       └── models.py
├── web/                       # React 前端
│   └── src/
│       ├── App.tsx            # 主布局
│       ├── hooks/
│       │   └── useChat.ts     # 核心状态管理 + SSE事件处理
│       ├── components/
│       │   ├── MessageBubble.tsx
│       │   ├── ToolCard.tsx / ToolResultCard.tsx
│       │   ├── PermissionDialog.tsx  # 权限确认弹窗
│       │   ├── QuestionDialog.tsx    # 用户问答弹窗
│       │   ├── FileInsightsPanel.tsx # Insight / Memory / Pattern 面板
│       │   └── TokenBar.tsx
│       └── api/
│           └── client.ts      # API 客户端
└── workspace/                 # 运行时项目文件
```

### 运行测试

```bash
python -m pytest tests/ -v
```

### 前端开发

```bash
cd web
npm run dev     # 开发服务器（热更新，代理 /api 到 localhost:8000）
npm run build   # 生产构建
```

## 核心设计

### ReAct 循环

主Agent和子Agent共用 `llm_turn.py` 的底层逻辑：

- `query()`：完整 ReAct 循环（LLM流式调用 → 工具执行 → 消息追加 → 循环控制），子Agent直接使用
- `build_assistant_message()`：构建 OpenAI 兼容的 assistant(tool_calls) 消息，主Agent和子Agent共用

主Agent在此基础上增加权限检查、SubAgent编排、会话持久化、审计日志。

### 会话过程回放

每个完成的会话轮次会同时保存模型上下文与前端展示事件。重新打开或刷新页面时，前端使用展示事件恢复主 Agent 文本、工具调用、子 Agent 过程和结果，而不会把原本的子 Agent 卡片错误渲染成普通成功/失败气泡。

### 记忆系统

记忆体系以“Trace 是不可变证据、文件是派生知识”为原则。详见上方的“Trace、项目洞察与长期记忆”：`memory.md` 是最多 200 条活跃标题的滑动索引，相关正文才会按需作为附件进入上下文；稳定 Pattern 则直接注入完整内容。

### 安全模型

三层权限检查：`1a 静态规则 → 1b 风险评估 → 1c 工具自主判断`

- Bash 命令分三档：**BLOCKED**（sudo/shutdown 等，硬拒绝）、**ASK**（sed/rm 等，弹窗确认）、**ALLOW**（wc/cat 等，放行）
- Write/Edit 默认 ASK，仅 allow_rules/acceptEdits/.memory目录/内部文件/已批准 五种例外 ALLOW
- 所有文件操作限制在项目工作目录内

### AskUserQuestion

LLM 可调用 AskUserQuestion 工具向用户提问（偏好确认、方案选择等）。支持单选/多选，最多 4 个问题、每问 2–4 个选项；每个问题始终附带一个自由文本输入框，用户可输入未列出的答案。主 Agent 和子 Agent 均可用，前端弹出 QuestionDialog 等待回答后继续执行。

### 上下文压缩

当 token 计数达到配置阈值（默认 `token_limit * 0.8`）时，在进入下一轮 ReAct 循环前自动触发：先捕获尚未聚合的 Trace，再调用 LLM 生成对话摘要（300 字以内），保留关键决策、人物变更和用户偏好，并保留最近对话消息。

### 子Agent

LLM 通过 SubAgent 工具引用预设名创建子 Agent。每种预设定义独立的 System Prompt、工具子集、历史继承策略和最大轮次。子 Agent 在自己的上下文中运行独立 ReAct 循环，流式输出进度到前端，完成结果以 tool_result 注入主 Agent；章节写作完成后可触发审阅 → 润色工作流。子 Agent 之间不允许嵌套调用。
