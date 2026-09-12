# 项目存储结构

每个项目目录只使用下面的结构。三级编号保留在文件名中，方便人工与 Agent 直接用 `Grep` 精确定位内容。

```text
workspace/<project-id>/
├─ project.yaml
├─ outlines/
│  ├─ outline_1.0.0.md
│  └─ outline_1.1.0.md
├─ chapters/
│  └─ content_1.1.1.md
├─ world/
├─ characters/
├─ reference/
├─ .memory/
└─ archive/legacy/                 # 仅保存迁移时与当前卷纲冲突的旧文件
```

## 编号与归属

- `outlines/outline_1.0.0.md`：第 1 卷卷纲。
- `outlines/outline_1.2.0.md`：第 1 卷第 2 章章纲。
- `chapters/content_1.2.3.md`：第 1 卷第 2 章第 3 小节正文。
- 世界观、角色卡和参考设定只能分别写入 `world/`、`characters/`、`reference/`。

`archive/legacy/` 不参与小说结构、资料栏或 Agent 的日常检索；它仅用于保留迁移时无法同时作为当前版本展示的旧文件。迁移不会删除创作文件，也不会覆盖已有目标文件。
