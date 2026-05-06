---
name: 工作流规范：Review→Polisher传递
description: 调用 chapter_polisher 时的正确工作流规范
type: feedback
tags: [工作流, SubAgent, review]
summary: 调用 chapter_polisher 时必须传递完整 review 报告，不能自己提炼修改清单
---

# 工作流规范：Review → Polisher 传递规则

## 错误案例（2026-05-03）

**场景**：用户要求 review 第四章和第五章，然后调用润色 subagent 进行修改。

**错误做法**：我（主Agent）先自己做了 review，写了一份审阅报告。然后只把提炼后的"修改清单"传给 chapter_polisher，没有传递完整的 reviewer 报告。

**问题**：
1. 子Agent 看不到完整审阅报告，只能看到我提炼的问题清单
2. 如果我对 review 结果的解读有偏差，子Agent 也会跟着走偏
3. 子Agent 可能需要重复读取文件（自己调用 Read）

## 正确做法

**如果是 review → 修改的工作流**：
1. 调用 `reviewer` SubAgent 获取完整审阅报告
2. 把完整报告作为附件（attachments）传给 `chapter_polisher`
3. 同时把需要修改的文件内容也作为附件传给 `chapter_polisher`
4. task 里写"参考附件中的审阅报告进行修改，不需要再读文件"

**正确示例**：

```yaml
task: |
  根据reivew报告润色第4章和第5章。
  {review报告}原文
  参考附件中的章节进行修改，不需要再读文件。
  
attachments:
  - path: "chapters/ch04.md"
    content: |
      （完整文件内容）
  - path: "chapters/ch05.md"
    content: |
      （完整文件内容）
```

## 为什么不能自己提炼修改清单

1. **完整上下文**：reviewer 的报告包含了判断问题的逻辑和依据，子Agent 需要这些信息来做出决策
2. **避免信息丢失**：自己提炼可能遗漏重要细节
3. **避免解读偏差**：主Agent 对 review 结果的解读可能与原始报告有偏差
4. **子Agent 独立性**：子Agent 有独立上下文，应让它自己理解任务要求

## 什么时候可以自己提炼

只有在以下情况才可以自己提炼修改要求：
- 用户直接说"把某个角色的性格改得活泼一点"——这类明确的简单指令
- 不需要先 review 就能执行的简单修改任务
