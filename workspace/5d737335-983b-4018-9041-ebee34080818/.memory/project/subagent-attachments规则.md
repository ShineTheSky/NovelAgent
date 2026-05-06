---
name: SubAgent调用规范
type: project
tags: [subagent, attachments, 工作流设计]
summary: 任何涉及正文内容的操作，都应在attachments里传递完整原文
---

# SubAgent调用规范——Attachments必须包含完整原文

## 问题案例
- **场景**：调用polisher修改第6章逻辑错误
- **错误做法**：只传"逻辑问题清单"，没传原文
- **结果**：polisher无法完整理解上下文，修改不完整

## 核心规则
1. **任何涉及正文内容的操作，都应该在attachments里传递原文**
2. **不要假设子Agent会"自己读取"文件**——这违反了"禁止直接操作文件"的设计初衷
3. **在设计prompt时，应明确"需要哪些文件作为参考"**

## 不同任务类型的附件要求

| 任务类型 | 需要的附件 |
|---------|-----------|
| 创作新章节 | 大纲 + 前文摘要 |
| 审阅章节 | 完整章节内容 |
| 修复逻辑错误 | 完整章节内容 + 问题清单 |
| 润色/扩展 | 完整章节内容 |
| 修改片段 | 完整章节内容 + 修改指令 |

## 正确的调用模式
```
SubAgent(
  preset="chapter_polisher",
  attachments=[
    {content: "ch06.md完整内容", path: "chapters/ch06.md"},
    {content: "逻辑问题清单", path: "逻辑问题.md"}
  ],
  task="修改chapters/ch06.md的逻辑问题"
)
```

## 记忆来源
- 2026-06-01 对话：用户发现polisher没有收到完整章节内容
- 教训：第7步传了ch06.md给reviewer，但第8步没有传给polisher
