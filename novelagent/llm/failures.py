"""Keep a persisted LLM failure's identity while callers propagate its message."""

from contextvars import ContextVar


llm_failure_context = ContextVar("llm_failure_context", default=None)


class RecordedLLMError(str):
    def __new__(cls, message, bad_case_id=""):
        value = super().__new__(cls, message)
        value.bad_case_id = bad_case_id
        return value


def recorded_bad_case_id(error) -> str:
    identity = getattr(error, "bad_case_id", "")
    if identity:
        return identity
    if isinstance(error, Exception) and error.args:
        return recorded_bad_case_id(error.args[0])
    return ""
