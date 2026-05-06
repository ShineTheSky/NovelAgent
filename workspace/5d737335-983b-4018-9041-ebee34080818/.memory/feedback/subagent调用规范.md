---
name: SubAgent调用规范
description: SubAgent调用的正确工作流程
type: feedback
tags: [SubAgent, 工作流, 工具使用]
summary: SubAgent必须先Read文件再传递完整内容，禁止自己Write
---

## 错误案例
调用SubAgent时传递了placeholder或压缩摘要，导致返回空结果。

## 正确工作流程
1. **必须先Read读取完整文件**（outline.md等）
2. **通过attachments传递完整内容**，不能用placeholder
3. **task中写清楚任务要求**
4. **如果返回空，应该重试**，而不是自己Write写文件

## 违规记录
曾因SubAgent返回空，直接自己Write写入大纲——这是严重违规。
