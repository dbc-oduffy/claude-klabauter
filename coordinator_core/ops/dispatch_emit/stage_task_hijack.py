"""Stage-task hijack classifier: detects a stage reply that declined or replaced its task for a relayed/user request.

One regex source feeds both the Python predicate and the JS boolean expression emitted into workflow
scripts, so the two cannot drift. Patterns stay inside the Python/JS common subset (no inline flags,
no named groups, no lookbehind); case-insensitivity is passed as a flag on each side.
"""

import re
from typing import Optional

HIJACK_HALT_REASON = "stage-task-hijacked"

# A phrase saying the stage task was not done.
DECLINE_SOURCE = (
    r"\bno plan\b|\bnot run\b|\bwrote no\b|\btakes precedence\b|\bonly asks? for\b|\bdeclin\w*"
)
# A reference to a relayed or user request/message.
USER_REQUEST_SOURCE = (
    r"\b(?:relayed|user)\b\W+(?:\w+\W+){0,2}(?:request|message)s?\b"
)
# Both must co-occur within one window, in either order.
_WINDOW = r"[\s\S]{0,200}?"
HIJACK_SOURCE = (
    r"(?:" + DECLINE_SOURCE + r")" + _WINDOW + r"(?:" + USER_REQUEST_SOURCE + r")"
    r"|(?:" + USER_REQUEST_SOURCE + r")" + _WINDOW + r"(?:" + DECLINE_SOURCE + r")"
)

_HIJACK_RE = re.compile(HIJACK_SOURCE, re.IGNORECASE)
_DECLINE_RE = re.compile(DECLINE_SOURCE, re.IGNORECASE)

_EXCERPT_RADIUS = 120


def classify(text: Optional[str]) -> Optional[str]:
    """Return an excerpt around the decline phrase when `text` declines its task citing a relayed/user request, else None."""
    if not text or not isinstance(text, str):
        return None
    hit = _HIJACK_RE.search(text)
    if hit is None:
        return None
    m = _DECLINE_RE.search(hit.group(0))
    if m is None:
        return None
    m_start, m_end = hit.start() + m.start(), hit.start() + m.end()
    start = max(0, m_start - _EXCERPT_RADIUS)
    end = min(len(text), m_end + _EXCERPT_RADIUS)
    return text[start:end].strip()


def hijack_test_js(expr: str) -> str:
    """Return a JS boolean expression testing `expr` (a JS string expression) against the shared pattern."""
    src = HIJACK_SOURCE.replace("/", "\\/")
    return "(/" + src + "/i.test(String(" + expr + " == null ? '' : " + expr + ")))"
