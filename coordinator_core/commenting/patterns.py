"""Regex families for `coordinator_core.commenting.detector.scan_text`.

Every family below is anchored to BOTH a comment marker (`#`, `//`, `/*`, a
leading `*`, or `<!--`) AND a shape -- a bare date, a bare "per", or a bare
ALL-CAPS word never matches on its own (see `_MARKER`). This mirrors
`coordinator_core.attribution.patterns`'s `persona_attributed` anchoring, and
exists for the same reason: an unanchored token match false-positives on
ordinary prose that happens to contain the word.

No `commented_out_code` family here -- telling a commented-out statement
from ordinary prose is a parse question, not a regex one, and is named as a
follow-up in the owning plan's Out of scope rather than approximated here.
"""

import re

_MARKER = r"(?:#|//|/\*|\*(?!/)|<!--)"

PATTERNS: dict[str, re.Pattern] = {
    "changelog_dated": re.compile(
        rf"{_MARKER}[^\n]{{0,40}}?\b(?:added|fixed|changed|updated|removed|as of|since)\b"
        rf"[^\n]{{0,30}}?\d{{4}}-\d{{2}}-\d{{2}}"
        rf"|{_MARKER}[^\n]{{0,40}}?\d{{4}}-\d{{2}}-\d{{2}}"
        rf"[^\n]{{0,30}}?\b(?:added|fixed|changed|updated|removed|as of|since)\b",
        re.IGNORECASE,
    ),
    "authored_by": re.compile(
        rf"{_MARKER}[^\n]*?\b(?:added|written|requested)\s+by\s+\S+"
        rf"|{_MARKER}[^\n]*?\bper\s+(?:the\s+)?PM\b"
        rf"|{_MARKER}[^\n]*?\bper\s+(?:task|request|ticket)\b",
        re.IGNORECASE,
    ),
    "task_ref": re.compile(
        rf"{_MARKER}[^\n]*?\b(?:pln|hnd|dlv)-[a-z0-9-]+-[0-9a-f]{{6}}\b"
        rf"|{_MARKER}[^\n]*?\bImplements\s+pln-"
        rf"|{_MARKER}[^\n]*?\bChunk\s+C\d+\b",
    ),
    "narration": re.compile(
        rf"{_MARKER}[^\n]*?\b(?:used to|no longer)\b"
        rf"|{_MARKER}[^\n]*?\bchanged from\b[^\n]*?\bto\b"
        rf"|{_MARKER}[^\n]*?\bwas\s+\S[^\n]*?,\s*now\s+\S",
        re.IGNORECASE,
    ),
    "grep_bait_token": re.compile(
        rf"{_MARKER}\s*([A-Z0-9]+(?:-[A-Z0-9]+){{2,}})\s*(?:-->|\*/)?\s*$",
        re.MULTILINE,
    ),
}
