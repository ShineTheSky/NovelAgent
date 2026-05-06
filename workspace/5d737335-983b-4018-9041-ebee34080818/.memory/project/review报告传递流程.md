---
name: review 报告传递流程
description: reviewer 返回后应保存完整报告传给下一个 SubAgent
type: project
tags: [工作流, review, 报告传递]
summary: reviewer 完整报告应保存并传递，不要自己提炼
date: 2025-01-21
---

## 正确的 review 流程
1. 调用 reviewer SubAgent（show_result: true）
2. reviewer 返回**完整报告**（包含所有分析）
3. 保存 reviewer 的完整报告到文件（如 `.memory/review/ch01_review.md`）
4. 调用 chapter_polisher SubAgent，attachments 中附上完整报告
5. 在 task 中写"参考附件中的审阅报告修改"

## 禁止的行为
- 主 Agent 自己提炼"修改清单"
- 主 Agent 自己判断哪些要改
- 主 Agent 直接告诉 polisher 要改什么

## 原因
自己提炼会遗漏细节（如舰船名字不一致、交易逻辑错误等），SubAgent 的完整报告更全面。
