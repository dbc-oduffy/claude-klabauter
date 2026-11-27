"""Which added comments the lint never judges: tooling, tagged, tripwire, engine-gap, generated."""
from __future__ import annotations

import re

from coordinator_core.comment_strip.keep_rules import is_tooling_comment

TAGS: tuple[str, ...] = ("trap", "invariant", "external-constraint")

_MARKER_RE = re.compile(r"^(?:#+|/{2,}|/\*+|\*+)[ \t]*")
_TAG_RE = re.compile(r"^(?:%s):" % "|".join(re.escape(t) for t in TAGS))
_TRIPWIRE_RE = re.compile(r"^(?:tripwire:\s*)?[A-Z0-9]+(?:-[A-Z0-9]+){2,}[.:]?$")
_ENGINE_GAP_RE = re.compile(r"^engine-gap:")
_DR_RE = re.compile(r"\bDR-\d+\b")
_GENERATED_RE = re.compile(
    r"@generated|auto-?generated|do not edit|code generated|generated (?:by|from|file)",
    re.IGNORECASE,
)


def strip_marker(text: str) -> str:
    """Comment text without its leading marker and, for a block comment, its closing `*/`."""
    body = _MARKER_RE.sub("", text.strip(), count=1)
    return body[:-2].rstrip() if body.endswith("*/") else body


def has_tag(text: str) -> bool:
    """True when the comment opens with `trap:`, `invariant:` or `external-constraint:` right after its marker (case-sensitive)."""
    return bool(_TAG_RE.match(strip_marker(text)))


def is_allowed_comment(text: str) -> bool:
    """A `#`/`//`/`/* */` comment that raises no finding; tag-continuation lines are the caller's concern."""
    if is_tooling_comment(text):
        return True
    body = strip_marker(text)
    return bool(_TAG_RE.match(body) or _TRIPWIRE_RE.match(body) or _ENGINE_GAP_RE.match(body))


def is_allowed_docstring(text: str) -> bool:
    """A docstring (quotes stripped) that carries a DR backlink or opens with an allowed tag."""
    return bool(_DR_RE.search(text) or _TAG_RE.match(text.lstrip()))


def is_generated_file(source: str) -> bool:
    """True when the first 5 lines declare the file generated."""
    head = "\n".join(source.splitlines()[:5])
    return bool(_GENERATED_RE.search(head))
