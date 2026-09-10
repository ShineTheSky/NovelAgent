# 项目长期记忆

## 运行环境（2026-09-10 核查）
- 项目无 requirements.txt / pyproject.toml，依赖以 README「快速开始」列出的为准：
  fastapi, uvicorn, tiktoken, python-dotenv, pydantic, pydantic-settings, sqlalchemy, aiosqlite, httpx, jinja2, pyyaml；Python >= 3.11。
- 本机 conda 环境（miniforge3）：base(3.13)、flairgpt(3.12)、idesign(3.9)、layoutgpt(3.9)、saaa(3.11)、sceneweaver(3.13)。
- 结论（旧）：原有环境都不完整；idesign / layoutgpt 为 Python 3.9，不满足 3.11 要求，不可用作后端环境。
- **2026-09-10 已新建专用环境 `novelagent`（Python 3.12.14）**，为本项目推荐运行环境：
  `conda activate novelagent`（位置 D:\soft\miniforge3\envs\novelagent）。
  已装：fastapi 0.141.1、uvicorn[standard] 0.52.4、starlette 1.6.0、tiktoken 0.14.0、python-dotenv 1.2.3、
  pydantic 2.13.5、pydantic-settings 2.15.0、sqlalchemy 2.0.52、aiosqlite 0.22.1、httpx 0.28.1、jinja2 3.1.6、pyyaml 6.0.3。
  已冒烟验证：`import novelagent.server.app` 成功（FastAPI app 构建正常）。项目仍无 requirements.txt。
- Node 相关：本机无系统 Node、无 nvm/fnm。前端 `web/` 用 Vite 8 + TS 6 + React 19，建议 Node 22/24 LTS（26 太新）。
  conda 环境里可直接 `conda install -c conda-forge nodejs=22` 拿到 node/npm（已装 22.23.2 / npm 10.9.8）；
  Python 官方 venv 才装不了 Node。
- Node 目录规则：`npm install` 的 node_modules 落在**项目目录 `web/`** 下；只有 `npm i -g` 的全局包才进
  `envs\novelagent\node_modules`。**必须先 `conda activate novelagent`**，否则 PATH 里没有 node.exe，
  直接调 npm.cmd 跑 vite 会报 `'node' is not recognized`。
- 后端启动：`uvicorn novelagent.server.app:app --reload --port 8000`；前端 `cd web && npm run dev`（Node >= 18）。
