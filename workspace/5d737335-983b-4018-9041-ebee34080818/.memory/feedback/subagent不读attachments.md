---
name: SubAgent 不使用 attachments
description: reviewer SubAgent 收到 attachments 后仍然读取文件
type: feedback
tags: [subagent, attachments, reviewer]
summary: reviewer 不优先使用 attachments，需在 system_prompt 中添加明确规则
date: 2025-01-21
---

## 问题现象
调用 reviewer SubAgent 时，已在 attachments 中传递了完整的文件内容，但 reviewer 仍然调用 Read 读取文件。

## 原因
SubAgent 预设的 system_prompt 中没有明确要求"优先使用 attachments"。

## 解决
在 `config/subagent_presets.yaml` 中 reviewer 预设的 system_prompt 添加：
```yaml
2. 优先使用 attachments。如果 attachments 中已提供完整文件内容，禁止再读取文件，禁止调用 Read/Grep 工具。
```

## 重要规则
- attachments 中的文件内容必须优先使用
- 禁止在已提供 attachments 的情况下重复读取文件
- 完整文件在 attachments，不在 attachments 里的文件需要自己读取
