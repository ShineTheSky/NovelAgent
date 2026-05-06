---
name: SubAgent调用时的Attachments传递规则
description: 调用子Agent时，所有上下文都应通过attachments传递，不应让子Agent自行读取文件
type: project
tags: [subagent, 工作流, 核心规则]
summary: 任何涉及正文内容的操作都应在attachments里传递原文
---

# SubAgent调用规则

## 核心原则
所有上下文都应该通过attachments传递，不应该让子Agent自己读取文件。

## 具体要求
1. **创作类任务**：需要传大纲、章节要求、背景设定
2. **审阅类任务**：需要传被审阅的完整文件
3. **修改类任务**：需要传原文 + 修改要求

## 反面案例
```
错误：
SubAgent(preset="chapter_polisher", task="修改ch06.md", attachments=[逻辑问题.md])

正确：
SubAgent(preset="chapter_polisher", task="修改ch06.md", attachments=[
  {content: "ch06.md完整内容", path: "chapters/ch06.md"},
  {content: "逻辑问题清单", path: "逻辑问题.md"}
])
```

## 原因
- 子Agent不应直接操作文件（禁止直接Edit小说正文）
- 不应假设子Agent会"自己读取"——因为这违反了"禁止直接操作文件"的设计初衷
- 特别是"修复逻辑错误"类任务，子Agent需要完整的上下文来理解因果关系
