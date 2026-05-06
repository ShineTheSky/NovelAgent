---
name: SubAgent调用规则
description: 关于何时、如何调用SubAgent的核心规则
type: project
tags: [workflow, SubAgent, prompt规则]
summary: 所有涉及内容创作的修改必须通过SubAgent，attachments需传递完整信息
---

## 核心规则
1. **禁止直接创作**：所有创作必须通过SubAgent完成，包括大纲、正文、设定文件的任何修改
2. **禁止直接操作文件**：不能用Edit工具直接修改任何创作内容

## 操作流程
1. 先用Read读取原文
2. 通过attachments传递完整内容
3. 调用对应的SubAgent
4. SubAgent完成修改

## 教训记录

### 错误1：假设SubAgent会自己读取文件
- **错误认知**：polisher修改时，只需要传"问题描述"，它会自己读取原文
- **正确做法**：无论什么任务，都要在attachments里传完整原文
- **原因**：违反"禁止直接操作文件"的设计，SubAgent应该通过attachments获取所有上下文

### 错误2：大纲不属于"正文"，可以直接修改
- **错误认知**：outline.md是设定文件，不是正文，可以直接Edit
- **正确做法**：大纲也是创作内容，必须通过SubAgent修改
- **原因**：核心规则是"禁止直接创作"，大纲的修改涉及内容判断，属于创作

## 正确示例
```
用户：修改第七章大纲
正确：Read outline.md → SubAgent(preset="outliner", attachments=[outline.md], task="...") → 呈现结果
错误：Read outline.md → Edit outline.md → 呈现结果
```

## 适用场景
- 修改大纲
- 修改人物设定
- 修改正文章节
- 修改世界观设定
- 任何涉及内容判断和创作的操作
