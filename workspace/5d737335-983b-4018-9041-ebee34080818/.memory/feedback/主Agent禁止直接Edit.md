---
type: feedback
tags: [工作流, SubAgent, 禁止Edit]
summary: 主Agent禁止直接调用Edit，必须通过SubAgent完成修改
---

违规行为：SubAgent执行后，主Agent自己调用Edit修改文件
正确行为：所有文件修改必须通过SubAgent完成，主Agent禁止直接调用Edit/Write

正确流程：
1. 调用SubAgent（show_result: true）
2. 等待SubAgent返回结果
3. 完成