# 记忆落盘布局

项目记忆只使用以下三类位置：

| 类别 | 路径 | 读取策略 |
|---|---|---|
| 全局用户偏好 | `global_memory/user.md` | 跨项目，按当前请求相关性注入。 |
| 项目规则与修正 | `workspace/{project_id}/.memory/project_rules.md` | 当前项目的聊天、写作、审阅和润色按相关性读取。 |
| 参考用途绑定 | `workspace/{project_id}/.memory/references/*.md` | 润色/扩写需要参考资料时读取，并据此调用 RAG。 |

普通记忆的来源是项目内 `.history/history.sqlite3`：文件修改按请求和文件聚合版本、需求及执行证据；无文件目标的项目讨论形成对话 History，并记录相关 Session turn 范围。提取后的 `semantic_evidence` 放入 Insight/Memory 记录；合并时先读语义证据，不够时按 History ID 回读原始内容。Trace 继续承担完整执行记录、bad/bash case 与上下文压缩摘要，不再由定期 Trace 窗口提炼普通记忆。History 是证据存储，不是另一类可直接注入的记忆。

旧目录 `.memory/user/`、`.memory/feedback/`、`.memory/project/`、`.memory/workflow/`、`.memory/review/` 与 `.memory/reference/` 只由迁移脚本读取。迁移后原文件保留在 `.memory/archive/legacy/`，新读取逻辑不再索引它们。
