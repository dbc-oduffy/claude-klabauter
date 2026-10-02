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

# A history subject: something that places the sentence in the past rather
# than describing how the code behaves now -- a date, a commit sha (hex with
# at least one digit, so a plain word like "defaced" never counts), or a
# narrator ("we", "previously", "formerly", "originally"). `narration` and
# `authored_by` fire only on a line that carries one (bar "per PM" and the
# "previously <past verb>" form, which name their own history); a bare
# "no longer", "written by" or "per request" is also how present-tense
# invariants and mechanism descriptions are phrased ("the cache no longer
# holds X once flushed", "the manifest written by setup", "one lock per
# request").
_HISTORY = (
    r"(?:\d{4}-\d{2}-\d{2}"
    r"|\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b"
    r"|\b(?:we|previously|formerly|originally)\b)"
)
_PAST_VERB = (
    r"(?:used\s+to|was|were|did|had|ran|returned|raised|emitted|wrote|took|called|stored)"
)

PATTERNS: dict[str, re.Pattern] = {
    "changelog_dated": re.compile(
        rf"{_MARKER}[^\n]{{0,40}}?\b(?:added|fixed|changed|updated|removed|as of|since)\b"
        rf"[^\n]{{0,30}}?\d{{4}}-\d{{2}}-\d{{2}}"
        rf"|{_MARKER}[^\n]{{0,40}}?\d{{4}}-\d{{2}}-\d{{2}}"
        rf"[^\n]{{0,30}}?\b(?:added|fixed|changed|updated|removed|as of|since)\b",
        re.IGNORECASE,
    ),
    "authored_by": re.compile(
        rf"{_MARKER}(?=[^\n]*{_HISTORY})[^\n]*?\b(?:added|written|requested)\s+by\s+\S+"
        rf"|{_MARKER}[^\n]*?\bper\s+(?:the\s+)?PM\b"
        rf"|{_MARKER}(?=[^\n]*{_HISTORY})[^\n]*?\bper\s+(?:task|request|ticket)\b",
        re.IGNORECASE,
    ),
    "task_ref": re.compile(
        rf"{_MARKER}[^\n]*?\b(?:pln|hnd|dlv)-[a-z0-9-]+-[0-9a-f]{{6}}\b"
        rf"|{_MARKER}[^\n]*?\bImplements\s+pln-"
        rf"|{_MARKER}[^\n]*?\bChunk\s+C\d+\b",
    ),
    "narration": re.compile(
        rf"{_MARKER}(?=[^\n]*{_HISTORY})[^\n]*?\b(?:used to|no longer)\b"
        rf"|{_MARKER}(?=[^\n]*{_HISTORY})[^\n]*?\bchanged from\b[^\n]*?\bto\b"
        rf"|{_MARKER}[^\n]*?\b(?:previously|formerly|originally)\b[,\s]+(?:\w+\s+){{0,2}}?{_PAST_VERB}\b"
        rf"|{_MARKER}[^\n]*?\bwas\s+\S[^\n]*?,\s*now\s+\S",
        re.IGNORECASE,
    ),
    "grep_bait_token": re.compile(
        rf"{_MARKER}\s*([A-Z0-9]+(?:-[A-Z0-9]+){{2,}})\s*(?:-->|\*/)?\s*$",
        re.MULTILINE,
    ),
}
