"""AskUserQuestion工具 — LLM向用户提问"""

from novelagent.tools.base import ToolProtocol, ToolResult, ToolContext, PermissionResult


class AskUserQuestionTool(ToolProtocol):
    name = "AskUserQuestion"
    description = (
        "Use this tool when you need to ask the user questions during execution. "
        "This allows you to: 1. Gather user preferences or requirements. "
        "2. Clarify ambiguous instructions. 3. Get decisions on implementation choices. "
        "4. Offer choices to the user about what direction to take. "
        "Usage notes: Users will always be able to select 'Other' to provide custom text input. "
        "Use multiSelect: true to allow multiple answers to be selected. "
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
