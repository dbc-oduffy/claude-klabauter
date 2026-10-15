"""The gate-owed marker contract: one line, two kinds, shared by every reader.

An executor whose build gate was its only miss ends its reply with
``gate-blocker: <kind>: <detail>`` beneath the status line. The emitted row runner
(``GATE_OWED_JS_RE``, against the JSON-stringified reply) and terminal_commit's regrade
(``match``, against the report file) read the same line; neither keys on prose.

Leaf module: stdlib only.
"""

from __future__ import annotations

import re
from typing import Optional

KIND_OUTSIDE_FOOTPRINT = "outside-footprint"
KIND_GUARD_DENIED = "guard-denied"
KINDS = (KIND_OUTSIDE_FOOTPRINT, KIND_GUARD_DENIED)

GATE_OWED_VAR = "_gateOwed"

_KIND_ALT = "|".join(re.escape(k) for k in KINDS)


def report_line(kind: str, detail: str) -> str:
    """The canonical marker line. ``kind`` must be one of ``KINDS``."""
    if kind not in KINDS:
        raise ValueError(f"unknown gate-owed kind: {kind!r}")
    return f"gate-blocker: {kind}: {detail}"


GATE_OWED_RE = re.compile(
    r"^[ \t>#-]*[*_`]{0,2}gate-blocker[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*"
    rf"(?P<kind>{_KIND_ALT})\b(?P<rest>[^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

# Group 1 is the kind, group 2 the rest of the line. Line start is emit's
# ``_LINE_START_JS`` shape: the stringified reply's first line or a (possibly escaped) newline.
GATE_OWED_JS_RE = (
    r'/(?:^"?|\n|\\n)(?:[ >#-]|\\t)*[*_`]{0,2}gate-blocker[*_`]{0,2}(?:[ ]|\\t)*:(?:[ ]|\\t)*[*_`]{0,2}'
    rf'(?:[ ]|\\t)*({_KIND_ALT})\b((?:[^"\\\n]|\\[^n])*)/i'
)


def match(text: str, kind: Optional[str] = None) -> Optional[tuple]:
    """First marker line in ``text`` as ``(kind, rest)`` with ``kind`` lowercased, else
    ``None``. A ``kind`` argument skips lines of any other kind."""
    for m in GATE_OWED_RE.finditer(text):
        found = m.group("kind").lower()
        if kind is None or found == kind:
            return found, m.group("rest")
    return None
