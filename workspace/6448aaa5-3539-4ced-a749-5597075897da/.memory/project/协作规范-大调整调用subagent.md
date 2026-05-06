---
name: 协作规范：大调整调用subagent
description: 调用subagent执行大幅修改的工作流程规范
type: project
tags: [工作流程, subagent, 规范]
summary: 大幅修改必须调用subagent，小幅修正可直接Edit但需说明原因
---

用户强调工作流程规范：
1. 大幅调整（如重构章节、重写冲突）必须通过调用subagent执行，不能跳过
2. 小幅修正（如添加一条设定）可以直接Edit，但需要向用户说明原因
3. 调用subagent任务时不应包含"读取文件"的指令，应通过attachments传递文件内容