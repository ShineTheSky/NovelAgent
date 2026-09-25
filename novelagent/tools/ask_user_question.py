"""AskUserQuestion工具 — LLM向用户提问"""

from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


def resolve_question_answers(questions: list, answers: list) -> list[dict]:
    """Join UI answer labels back to their full option descriptions.

    The browser intentionally posts a compact ``string | string[]`` value for
    each question.  Agent tool results and Trace evidence must be self-contained,
    so resolve those values while the original tool arguments are still present.
    """
    resolved = []
    for index, raw_question in enumerate(questions or []):
        question = raw_question if isinstance(raw_question, dict) else {}
        raw_answer = answers[index] if index < len(answers or []) else ""
        values = raw_answer if isinstance(raw_answer, list) else [raw_answer]
        values = [str(value).strip() for value in values if str(value).strip()]

        options = [option for option in question.get("options", []) if isinstance(option, dict)]
        options_by_label = {
            str(option.get("label") or "").strip(): option
            for option in options
            if str(option.get("label") or "").strip()
        }
        selected_options = []
        custom_inputs = []
        for value in values:
            option = options_by_label.get(value)
            if option is None:
                custom_inputs.append(value)
                continue
            selected_options.append({
                "label": value,
                "description": str(option.get("description") or "").strip(),
            })

        resolved.append({
            "question_index": index,
            "header": str(question.get("header") or "").strip(),
            "question": str(question.get("question") or "").strip(),
            "multi_select": bool(question.get("multiSelect", False)),
            "selected_options": selected_options,
            "custom_input": "\n".join(custom_inputs),
        })
    return resolved


class AskUserQuestionTool(ToolProtocol):
    name = "AskUserQuestion"
    description = (
        "Use this tool when you need to ask the user questions during execution. "
        "This allows you to: 1. Gather user preferences or requirements. "
        "2. Clarify ambiguous instructions. 3. Get decisions on implementation choices. "
        "4. Offer choices to the user about what direction to take. "
        "Usage notes: Every question always includes a free-text input field in addition to the options. "
        "Use multiSelect: true to allow multiple answers to be selected. "
        "当需要询问用户问题时."
        "If you recommend a specific option, make that the first option and add '(Recommended)' at the end."
    )

    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask the user (1-4 questions)",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The complete question to ask the user. Should end with a question mark.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Very short label displayed as a chip/tag (max 12 chars).",
                        },
                        "options": {
                            "type": "array",
                            "description": "2-4 mutually exclusive options (unless multiSelect is enabled)",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "The display text for this option (1-5 words)."},
                                    "description": {"type": "string", "description": "Explanation of what this option means."},
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Set to true to allow multiple answers to be selected.",
                            "default": False,
                        },
                    },
                    "required": ["question", "header", "options", "multiSelect"],
                },
            },
        },
        "required": ["questions"],
    }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        # 实际由 agent_loop 拦截处理，这里不会被调用
        return ToolResult(success=True, data={"questions": params.get("questions", [])})

    def checkPermissions(self, params: dict, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW
