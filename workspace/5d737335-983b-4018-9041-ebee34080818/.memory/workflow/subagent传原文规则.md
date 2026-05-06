---
type: workflow
tags: [subagent, attachments, 流程]
summary: subagent必须通过attachments传原文
---

调用任何SubAgent（如 chapter_polisher、chapter_reviewer 等）时，必须通过attachments传递所有相关文件（章节大纲、人设文档、待修改章节等）的完整内容，让SubAgent直接处理，不需要它自己读取文件。

附件传递格式示例：
```python
attachments=[
  {"content": "...大纲内容...", "path": "outline.md"},
  {"content": "...第六章全文...", "path": "chapters/ch06.md"}
]
```

不要假设SubAgent可以通过其他方式获取文件内容。
