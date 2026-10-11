"""Keep a persisted LLM failure's identity while callers propagate its message."""

from contextvars import ContextVar
import re


llm_failure_context = ContextVar("llm_failure_context", default=None)


def is_balance_error(error) -> bool:
    """Billing failures are operational notifications, not Agent Bad Cases."""
    text = str(error).lower()
    return getattr(error, "status_code", None) == 402 or bool(re.search(r"\bhttp\s*(?:error\s*)?402\b", text)) or any(
        marker in text for marker in ("insufficient balance", "insufficient_balance", "balance is insufficient",
                                     "payment required", "余额不足", "余额耗尽", "余额用完"))


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
