---
type: feedback
tags: [SubAgent, attachments, 优先级]
summary: SubAgent应优先使用attachments，禁止再读取已提供的文件
---

问题：SubAgent收到attachments后仍然读取文件
解决：在subagent_presets.yaml中，system_prompt第2步改为：
"优先使用attachments中的文件内容进行分析。如果attachments中已提供完整文件内容，禁止再读取文件，禁止调用Read/Grep工具。"