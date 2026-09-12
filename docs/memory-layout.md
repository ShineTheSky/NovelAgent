# 记忆落盘布局

项目记忆只使用以下三类位置：

| 类别 | 路径 | 读取策略 |
|---|---|---|
| 全局用户偏好 | `global_memory/user.md` | 跨项目，按当前请求相关性注入。 |
| 项目规则与修正 | `workspace/{project_id}/.memory/project_rules.md` | 当前项目的聊天、写作、审阅和润色按相关性读取。 |
| 参考用途绑定 | `workspace/{project_id}/.memory/references/*.md` | 润色/扩写需要参考资料时读取，并据此调用 RAG。 |

`Trace → Memory → Rule` 是后台证据演化流程：Trace 保存证据，Memory 和 Rule 保存到数据库并关联 Trace ID；它不是第四种项目文件记忆。

旧目录 `.memory/user/`、`.memory/feedback/`、`.memory/project/`、`.memory/workflow/`、`.memory/review/` 与 `.memory/reference/` 只由迁移脚本读取。迁移后原文件保留在 `.memory/archive/legacy/`，新读取逻辑不再索引它们。
