"""Compare evidence excerpts without changing stored text or factual wording."""

import json
import re
import unicodedata
from difflib import SequenceMatcher


_PUNCTUATION = str.maketrans({
    **{c: '"' for c in '\'“”‘’„‟‚‛「」『』«»‹›〝〞〟'},
    '。': '.', '、': ',', '﹑': ',',
    '【': '[', '】': ']', '〔': '[', '〕': ']', '〖': '[', '〗': ']',
    '〘': '[', '〙': ']', '〚': '[', '〛': ']',
    '《': '<', '》': '>', '〈': '<', '〉': '>',
    '—': '--', '―': '--', '–': '-', '‐': '-', '‑': '-', '−': '-',
    '…': '...', '⋯': '...', '〜': '~',
})


def _normalize(text):
    # NFKC is limited to punctuation/symbols: never fold letters, digits or units.
    parts = []
    for char in text:
        folded = unicodedata.normalize('NFKC', char)
        if (unicodedata.category(char)[0] in 'PS'
                and all(unicodedata.category(c)[0] in 'PS' for c in folded)):
            char = folded
        parts.append(char)
    normalized = ''.join(parts).translate(_PUNCTUATION)
    normalized = re.sub(r'\\+(["\'])', r'\1', normalized)
    normalized = re.sub(r'\.{3,}', '...', normalized)
    return re.sub(r'-{2,}', '--', normalized)


def excerpt_matches(excerpt, content):
    """Continuous substring match, tolerating equivalent punctuation only."""
    return bool(excerpt) and _normalize(excerpt) in _normalize(content)


def _similar_event_excerpt(excerpt, content):
    """At least 90% character similarity to a continuous source window."""
    quoted, source = _normalize(excerpt), _normalize(content)
    if not quoted:
        return False
    if quoted in source:
        return True
    blocks = SequenceMatcher(None, quoted, source, autojunk=False).get_matching_blocks()
    starts = {max(0, block.b - block.a) for block in blocks if block.size}
    ends = {min(len(source), block.b + len(quoted) - block.a) for block in blocks if block.size}
    for start in starts:
        for end in ends:
            length = end - start
            # Even a perfect match cannot reach 90% outside these length bounds.
            if length <= 0 or 2 * min(length, len(quoted)) / (length + len(quoted)) < .90:
                continue
            candidate = SequenceMatcher(None, quoted, source[start:end], autojunk=False)
            if candidate.quick_ratio() >= .90 and candidate.ratio() >= .90:
                return True
    return False


def event_quote_matches(excerpt, content):
    """Tolerant event locator; JSON quotes must stay within one string value."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return _similar_event_excerpt(excerpt, content)

    def contains(item):
        if isinstance(item, str):
            return _similar_event_excerpt(excerpt, item)
        if isinstance(item, list):
            return any(contains(child) for child in item)
        if isinstance(item, dict):
            return any(contains(child) for child in item.values())
        return False

    return contains(value)


def user_excerpt_matches(excerpt, content):
    """Match plain user input or a single string value in an Ask answer."""
    try:
        answers = json.loads(content)
    except (TypeError, ValueError):
        return excerpt_matches(excerpt, content)

    def contains(value):
        if isinstance(value, str):
            return excerpt_matches(excerpt, value)
        if isinstance(value, list):
            return any(contains(item) for item in value)
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        return False

    if isinstance(answers, (list, dict)):
        return contains(answers)
    return excerpt_matches(excerpt, content) or contains(answers)
