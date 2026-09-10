# NovelAgent2

面向小说创作的7层AI Agent系统。用户通过自然语言描述创作意图，Agent自主规划、调用工具、管理上下文和长期记忆，完成从章节大纲到正文撰写的全流程创作任务。

## 架构总览

```
Web UI (React) ← SSE → FastAPI Server → Agent Loop (ReAct)
                                            │
              ┌─────────────────────────────┼──────────────────────────┐
              │                │            │              │           │
         工具层 (8 tools)   安全层 (3层)   上下文层      记忆层      LLM客户端
         Read/Write/Edit   1a静态规则     Prompt模板    FileStore    多Provider
         Glob/Grep/Bash    1b风险评估     Token计算     IndexManager  Anthropic
         SubAgent          1c自主判断     消息管理      异步预取      DeepSeek
         AskUserQuestion                  上下文压缩    自动记忆      OpenAI..
```

| 层级 | 职责 |
|------|------|
| 第1层 Web UI | 文本输入、流式渲染、中断控制、弹窗问答 |
| 第2层 入口传输 | 启动初始化、请求标准化、会话管理、SSE管道 |
| 第3层 Agent Loop | ReAct循环、子Agent编排、LLM调用、记忆触发、工具调用 |
| 第4层 工具层 | Read / Write / Edit / Glob / Grep / Bash / SubAgent / AskUserQuestion |
| 第5层 安全层 | 三层权限检查、Bash命令两级分类、路径沙箱、审计 |
| 第6层 上下文层 | System Prompt管理、消息列表、Token计算、LLM摘要压缩 |
| 第7层 记忆层 | 文件化存储(.memory/)、memory.md索引、异步预取、memory_extractor子Agent |
| 参考资料层 | 项目级文章导入、段落分块、本地BM25检索、写作上下文注入 |

## 快速开始

### 环境要求

- Python >= 3.11
- Node.js >= 18

### 安装

```bash
# 1. 安装 Python 依赖
pip install fastapi uvicorn tiktoken python-dotenv pydantic pydantic-settings sqlalchemy aiosqlite httpx jinja2 pyyaml

# 2. 配置 API key
cp .env.example .env
# 编辑 .env，填入你的 LLM API key

# 3. 启动后端
uvicorn novelagent.server.app:app --reload --port 8000

# 4. 安装前端依赖（仅首次）
cd web && npm install && cd ..

# 5. 启动前端
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

  auto_memory:         # 自动记忆 → 最便宜
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
| `chapter_polisher` | Read, Write, Edit, Glob, Grep | 否 | 10 | 润色章节 |
| `reviewer` | Read, Glob, Grep | 否 | 8 | 独立审阅 |
| `character_designer` | Read, Write, Edit, Glob, Grep | 否 | 10 | 人物设定 |
| `outliner` | Read, Write, Glob, Grep, Bash, AskUserQuestion, Edit | 否 | 8 | 大纲规划 |
| `memory_extractor` | Read, Write, Grep, Glob | 是 | 3 | 提取长期记忆 |

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
| `/api/projects/{id}/rag/documents` | GET/POST | 查看或导入参考文章（TXT/Markdown文本） |
| `/api/projects/{id}/rag/documents/{document_id}` | DELETE | 删除参考文章及其分块 |
| `/api/projects/{id}/rag/search?q=...` | GET | 手动检索参考片段 |

### 参考资料库（RAG）

在项目页点击“资料库”，可导入 TXT 或 Markdown 文章。系统会按段落切分为约 900 字的小块，并保留约 120 字重叠内容。发送写作、续写或润色请求时，系统会在当前项目的资料库中自动检索最多 5 个相关片段，作为只读参考注入主 Agent 和子 Agent 上下文。

当前检索器使用本地 BM25，不需要额外的向量数据库或 embedding API，适合离线使用；后续可以在 `novelagent/rag/store.py` 中替换为向量检索实现。

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `text_delta` | LLM 流式文本输出 |
| `thinking` | DeepSeek 思考链内容 |
| `tool_call` | 工具调用 |
| `tool_result` | 工具执行结果 |
| `permission_ask` | 权限确认弹窗 |
| `question_ask` | 用户问答弹窗 |
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
│   ├── tools/                 # 工具层 (8 tools)
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
│   │   └── auto_memory.py     # 自动记忆轮询
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

### 记忆系统

记忆以 Markdown 文件存储在 `.memory/` 目录，采用 YAML frontmatter + 正文格式。LLM 使用基础工具（Write/Edit）直接读写记忆文件。

- **memory.md**：全局索引，含最近200条记忆的元信息表格，注入 System Prompt
- **异步预取**：用户输入后，轻量 LLM 判断相关性，1.5s 内返回则注入相关记忆原文
- **memory_extractor**：每轮对话结束后台运行，子Agent读取对话历史，用 Write 写入新的记忆文件并更新 memory.md 索引

### 安全模型

三层权限检查：`1a 静态规则 → 1b 风险评估 → 1c 工具自主判断`

- Bash 命令分三档：**BLOCKED**（sudo/shutdown 等，硬拒绝）、**ASK**（sed/rm 等，弹窗确认）、**ALLOW**（wc/cat 等，放行）
- Write/Edit 默认 ASK，仅 allow_rules/acceptEdits/.memory目录/内部文件/已批准 五种例外 ALLOW
- 所有文件操作限制在项目工作目录内

### AskUserQuestion

LLM 可调用 AskUserQuestion 工具向用户提问（偏好确认、方案选择等）。支持单选/多选，最多4个问题，每问2-4个选项。主Agent和子Agent均可用，前端弹出 QuestionDialog 等待用户回答后继续执行。（目前Prompt还需要调整，llm触发不够稳定）

### 上下文压缩

当 token 计数达到配置阈值（默认 `token_limit * 0.8`）时，在进入下一轮 ReAct 循环前自动触发：调用 LLM 生成对话摘要（300字以内），保留关键决策、人物变更、用户偏好，保留最近4条非系统消息。

### 子Agent

LLM 通过 SubAgent 工具引用预设名创建子Agent。每种预设定义独立的 System Prompt、工具子集、历史继承策略和最大轮次。子Agent 在自己的上下文中运行独立 ReAct 循环，流式输出进度到前端，结果以 tool_result 格式注入主 Agent。子Agent 之间不允许嵌套调用。
