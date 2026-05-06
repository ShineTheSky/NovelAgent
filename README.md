# NovelAgent2

面向小说创作的7层AI Agent系统。用户通过自然语言描述创作意图，Agent自主规划、调用工具、管理上下文和长期记忆，完成从章节大纲到正文撰写的全流程创作任务。

## 架构总览

```
Web UI (React) ← SSE → FastAPI Server → Agent Loop (ReAct)
                                            │
              ┌─────────────────────────────┼──────────────────────────┐
              │                │            │              │           │
         工具层 (7 tools)   安全层 (3层)   上下文层      记忆层      LLM客户端
         Read/Write/Edit   1a静态规则     Prompt模板    FileStore    多Provider
         Glob/Grep/Bash    1b风险评估     Token计算     IndexManager  Anthropic
         SubAgent          1c自主判断     消息管理      异步预取      DeepSeek
                                          上下文压缩    自动记忆      OpenAI..
```

| 层级 | 职责 |
|------|------|
| 第1层 Web UI | 文本输入、流式渲染、中断控制 |
| 第2层 入口传输 | 启动初始化、请求标准化、会话管理、SSE管道 |
| 第3层 Agent Loop | ReAct循环、子Agent编排、LLM调用、记忆触发 |
| 第4层 工具层 | Read / Write / Edit / Glob / Grep / Bash / SubAgent |
| 第5层 安全层 | 三层权限检查、Bash命令两级分类、路径沙箱、审计 |
| 第6层 上下文层 | System Prompt管理、消息列表、Token计算、LLM摘要压缩 |
| 第7层 记忆层 | 文件化存储(.memory/)、memory.md索引、异步预取、自动记忆轮询 |

## 快速开始

### 环境要求

- Python >= 3.11
- Node.js >= 18
- ripgrep >= 14 (可选，Grep工具的加速后端)

### 安装

```bash
# 1. 安装 Python 依赖
pip install fastapi uvicorn tiktoken python-dotenv pydantic pydantic-settings sqlalchemy aiosqlite httpx

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

如果只需要 API（不启动前端）：

```bash
uvicorn novelagent.server.app:app --reload --port 8000
# API 文档: http://localhost:8000/docs
```

## 配置

### 配置文件

| 文件 | 说明 | 提交 git |
|------|------|---------|
| `config/config.yaml` | 工作目录、安全策略、会话上限、日志 | 是 |
| `config/llm_config.yaml` | LLM provider 连接 + 各位置模型配置 | 是 |
| `config/subagent_presets.yaml` | 子Agent预设（内嵌Prompt） | 是 |
| `.env` | API key 实际值 | **否** |

### LLM 配置

`config/llm_config.yaml` 支持为每个调用位置独立配置 provider 和 model：

```yaml
positions:
  main_loop:           # 主Agent → 最强模型
    provider: anthropic
    model: claude-opus-4-6

  sub_agent:           # 子Agent → 按预设细化
    provider: deepseek
    model: deepseek-chat
    overrides:
      chapter_writer:  # 章节撰写 → 可独立用更强模型
        provider: anthropic
        model: claude-sonnet-4-6

  memory_prefetch:     # 记忆预取 → 最快模型
    provider: anthropic
    model: claude-haiku-4-5
    timeout: 1.5

  context_compression: # 上下文压缩 → 长文本
    provider: anthropic
    model: claude-sonnet-4-6

  auto_memory:         # 自动记忆 → 最便宜
    provider: deepseek
    model: deepseek-chat
```

支持的 provider：Anthropic, OpenAI, DeepSeek, OpenRouter, Azure OpenAI, 自部署 (vLLM 兼容)。

### 子Agent预设

`config/subagent_presets.yaml` 定义4种子Agent：

| 预设 | 工具 | 继承历史 | 用途 |
|------|------|---------|------|
| `chapter_writer` | Read, Write, Edit, Glob, Grep, Bash | 是 | 章节撰写 |
| `reviewer` | Read, Glob, Grep | 否 | 独立审阅 |
| `character_designer` | Read, Write, Edit, Glob, Grep | 是 | 人物设定 |
| `outliner` | Read, Write, Glob, Grep, Bash | 是 | 大纲规划 |

### Write 权限规则

Write/Edit 默认弹窗确认。以下情况自动放行：

1. 路径匹配 `write_allow_rules`（如 `chapters/**`）
2. acceptEdits 模式开启
3. 内部可编辑文件（`.claude/plans/`, `.claude/scratchpad.md`）
4. 会话内同路径已批准过

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/projects` | GET | 项目列表 |
| `/api/projects` | POST | 创建项目 |
| `/api/projects/{id}/sessions` | GET | 会话列表 |
| `/api/projects/{id}/sessions` | POST | 创建会话 |
| `/api/sessions/{id}` | GET | 会话详情 |
| `/api/sessions/{id}/message` | POST | 发送消息 (SSE流) |
| `/api/sessions/{id}/stop` | POST | 中断生成 |
| `/api/sessions/{id}/accept-edits` | POST | 切换 acceptEdits 模式 |

## 开发

### 项目结构

```
NovelAgent2/
├── config/                    # 配置文件
│   ├── config.yaml
│   ├── llm_config.yaml
│   └── subagent_presets.yaml
├── prompts/                   # Jinja2 Prompt模板
│   ├── base_system.j2
│   ├── partials/
│   └── dynamic/
├── novelagent/                # Python 后端
│   ├── server/                # FastAPI Web 服务
│   │   ├── app.py
│   │   ├── sse.py
│   │   └── routes/
│   ├── core/                  # Agent Loop + 子Agent
│   │   ├── agent_loop.py
│   │   ├── subagent.py
│   │   └── session.py
│   ├── tools/                 # 工具层 (7 tools)
│   │   ├── base.py, registry.py
│   │   ├── read.py, write.py, edit.py
│   │   ├── glob.py, grep.py, bash.py
│   │   └── subagent_tool.py
│   ├── security/              # 安全层
│   │   ├── permission_checker.py
│   │   ├── bash_classifier.py
│   │   ├── path_validator.py
│   │   └── audit.py
│   ├── context/               # 上下文层
│   │   ├── builder.py, prompt_manager.py
│   │   ├── message_manager.py, token_counter.py
│   ├── memory/                # 记忆层
│   │   ├── memory_manager.py, file_store.py
│   │   ├── index_manager.py, prefetcher.py, auto_memory.py
│   ├── llm/                   # LLM 客户端
│   │   ├── client.py, config_loader.py, stream.py
│   └── storage/               # 持久化
│       ├── database.py, models.py
├── web/                       # React 前端
│   └── src/
│       ├── App.tsx
│       └── api/client.ts
├── tests/                     # 自动化测试
├── 需求文档.md
├── 项目计划.md
├── 验收测试.md
└── 待办事项.md
```

### 运行测试

```bash
# 全部测试
python -m pytest tests/ -v

# 按模块运行
python -m pytest tests/test_tools/ -v
python -m pytest tests/test_security/ -v
python -m pytest tests/test_context/ -v
python -m pytest tests/test_memory/ -v
python -m pytest tests/test_llm/ -v
```

### 前端开发

```bash
cd web
npm run dev     # 开发服务器（热更新，代理 /api 到 localhost:8000）
npm run build   # 生产构建
```

## 核心设计

### 记忆系统

记忆以 Markdown 文件存储在 `.memory/` 目录，采用 frontmatter + 正文格式。LLM 使用基础工具（Grep/Glob/Write/Edit）直接读写记忆文件，无专用记忆工具。

- **memory.md**：全局索引，含最近200条记忆的元信息表格，注入 System Prompt
- **KV-Cache优化**：memory.md 仅在上下文压缩时更新，同会话内 System Prompt 不变
- **异步预取**：用户输入后，轻量 LLM 判断相关性，1.5s 内返回则注入相关记忆原文
- **自动记忆**：每5轮后台询问 LLM 是否需要存储新记忆

### 安全模型

三层权限检查：`1a 静态规则 → 1b 风险评估 → 1c 工具自主判断`

- Bash 命令分三档：**BLOCKED**（sudo/shutdown 等，硬拒绝）、**ASK**（sed/rm 等，弹窗确认）、**ALLOW**（wc/cat 等，放行）
- Write/Edit 默认 ASK，仅 allow_rules/acceptEdits/内部文件/已批准 四种例外 ALLOW
- 所有文件操作限制在项目工作目录内

### 子Agent

LLM 通过 SubAgent 工具引用预设名创建子Agent。每种预设定义独立的 System Prompt、工具子集和历史继承策略。子Agent 在自己的上下文中运行独立 ReAct 循环，结果以 tool_result 格式注入主 Agent。

## 文档

- [需求文档](需求文档.md) — 7层架构详细需求
- [项目计划](项目计划.md) — 实现计划与代码框架
- [验收测试](验收测试.md) — 242项验收用例
- [待办事项](待办事项.md) — 实现进度追踪
