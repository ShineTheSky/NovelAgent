"""Read one JSON object without guessing missing punctuation or changing facts."""

import json
import re


def json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start = text.find("{")
    if start < 0:
        raise ValueError("Model response has no JSON object")
    result, end = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(result, dict) or text[end:].strip():
        raise ValueError("Model response must contain one complete JSON object")
    return result
