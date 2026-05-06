---
name: 主 Agent 工作流约束
description: 主 Agent 不能直接调用 Edit/Write 修改文件
type: project
tags: [工作流, edit, subagent]
summary: 禁止主 Agent 直接修改文件，必须通过 SubAgent 完成
date: 2025-01-21
---

## 问题
调用 SubAgent 执行任务后，主 Agent 自己调用 Edit 直接修改文件，违反工作流原则。

## 绝对禁止的行为
- 主 Agent 禁止直接调用 Edit、Write 等文件修改工具
- 所有文件修改必须通过 SubAgent 完成
- 如果需要修改，必须调用 chapter_polisher 并设置 show_result: true

## 正确的修复流程
当发现问题时：
1. 调用 SubAgent（show_result: true）
2. 等待 SubAgent 返回结果
3. 完成

## 检查机制
每次调用工具前检查：
- 当前操作是否已由 SubAgent 完成？
- 是否应该让 SubAgent 执行而非自己执行？
