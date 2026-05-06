"""审计日志"""

import json
import time
from datetime import datetime, timezone


def audit_log(
    trace_id: str,
    tool_name: str,
    params: dict,
    risk_result: str,
    result_summary,
    duration_ms: float,
):
    summary = str(result_summary) if not isinstance(result_summary, str) else result_summary
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "tool": tool_name,
        "params_summary": _summarize(params),
        "risk_result": risk_result,
        "result_summary": summary[:200],
        "duration_ms": round(duration_ms, 2),
    }
    print(json.dumps(entry, ensure_ascii=False))


def _summarize(params: dict) -> str:
    """参数摘要（截断长值）"""
    items = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 80:
            s = s[:80] + "..."
        items.append(f"{k}={s}")
    return ", ".join(items)
