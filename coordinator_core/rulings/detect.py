"""Lexical detector for plans and DRs that defer or contradict an accepted PM ruling.

A tripwire, not a judge. Limits, stated so a caller never over-reads a pass:
  - Deferral: a defer-pattern match (defer/park/punt/await/wait-on + PM/human/owner, "leave it to
    the PM", "the PM should decide", "TBD", "open question", the five legacy phrases) in a
    paragraph (blank-line separated) is matched against each accepted ruling's scope tokens
    (word-boundary, case-insensitive; glob tokens via fnmatch) and its id. A deferral phrased
    outside the family is not seen.
  - A defer phrase inside double quotes or backticks is a mention, not a deferral, and is skipped.
  - Unnamed deferral: a defer phrase whose paragraph holds no `?`. It is refused whether or not
    a ruling matches; a named question is the only legitimate shape for surfacing to the PM.
  - Contradiction: a line citing a ruling id (word-boundary) or quoting its full text, beside a
    negation or withdrawal marker, or an explicit `contradicts: <id>`.
  - target_mismatch: a sentence naming a ruling target's quantity and stating a number that fails
    the target's comparator after unit normalisation (length, time, bytes). A number led by
    "not", "than", "from", "was", "previously" or "formerly" is a mention of the old value.
  - silent_override_value: a scope-hit paragraph's sentence states a different number, in the
    same dimension and sharing a content word (beyond the ruling's scope tokens), than a number in the ruling's text. Applies
    whether or not the id is cited.
  - silent_override_negation: a scope-hit paragraph's sentence that does not cite the ruling id,
    holds >=70% of a ruling sentence's content words, and has the opposite negation polarity.
    Both silent-override checks are lexical overlap: a paraphrase sharing no content word passes.
  - Scope-out: a line citing a ruling id that scopes it out of the run ("out of scope", "not in
    scope", "excluded", "do not touch") with no other negation is neither a contradiction nor a
    deferral of that ruling; a line that also overrides or rejects it is not a scope-out.
  - Clearance: only `pm_turn: <ref> <YYYY-MM-DD> <ruling-id>` clears, and only that id: the date
    must be after the ruling's `recorded` (an empty `recorded` sets no lower bound) and <ref> must
    be an existing repo-relative file inside the root. Unnamed deferrals are never cleared.

Pure: one `Path.exists` per citation, no subprocess, no git.
"""
from __future__ import annotations

import fnmatch
import math
import re
from pathlib import Path
from typing import Iterable

from coordinator_core.rulings.contract import GATE_ROUTE, STATUS_ACCEPTED, Ruling

_ACTOR = r"(?:(?:the|a|an|our|their)\s+)?(?:PM|human|(?:product\s+)?owner)(?:'s)?\b"
_GAP = r"[^.?!\n]{0,40}?"
DEFER_RE = re.compile(
    r"pending\s+an?\s+PM\s+ruling|PM\s+to\s+decide|needs?\s+an?\s+PM\s+ruling"
    r"|deferred\s+to\s+the\s+PM"
    rf"|\b(?:defer\w*|park(?:ed|ing|s)?|punt\w*|await\w*|wait(?:s|ing)?\s+(?:on|for)){_GAP}\b{_ACTOR}"
    rf"|\bleav(?:e|es|ing)\b{_GAP}\b(?:to|with|for|up\s+to)\s+{_ACTOR}"
    rf"|\b{_ACTOR}\s+(?:should|must|needs?\s+to|to|will|can)\s+(?:weigh\s+in|decide|rule|call|choose|confirm|approve)"
    r"|\bTBD\b|\bopen\s+questions?\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(?:not|no\s+longer|never|without|withdraw\w*|overrid\w+|supersed\w+|revok\w+|rescind\w*"
    r"|reject\w*|ignor\w+|waiv\w+|drop(?:s|ped)?|revers\w+|contrary\s+to|instead\s+of|despite)\b"
    r"|n't\b",
    re.IGNORECASE,
)
_CONTRADICTS_RE = re.compile(r"^\s*contradicts:\s*(\S+)", re.IGNORECASE)
_PM_TURN_RE = re.compile(r"^\s*pm_turn:\s*(\S+)\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$")
_QUOTED_RE = re.compile(r'"[^"\n]*"|`[^`\n]*`')
_WORD_RE = re.compile(r"[^\s,;()\[\]{}\"'`]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n")

# unit -> (dimension, factor to the dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {
    "um": ("length", 1e-6), "µm": ("length", 1e-6), "mm": ("length", 1e-3),
    "cm": ("length", 1e-2), "m": ("length", 1.0), "km": ("length", 1e3),
    "inch": ("length", 0.0254), "inches": ("length", 0.0254),
    "ms": ("time", 1e-3), "s": ("time", 1.0), "sec": ("time", 1.0), "secs": ("time", 1.0),
    "second": ("time", 1.0), "seconds": ("time", 1.0), "min": ("time", 60.0),
    "mins": ("time", 60.0), "minute": ("time", 60.0), "minutes": ("time", 60.0),
    "h": ("time", 3600.0), "hr": ("time", 3600.0), "hrs": ("time", 3600.0),
    "hour": ("time", 3600.0), "hours": ("time", 3600.0),
    "b": ("bytes", 1.0), "kb": ("bytes", 1e3), "mb": ("bytes", 1e6), "gb": ("bytes", 1e9),
    "kib": ("bytes", 1024.0), "mib": ("bytes", 1024.0**2), "gib": ("bytes", 1024.0**3),
    "%": ("percent", 1.0),
}
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNITS if u != "%"), key=len, reverse=True))
_NUMBER_RE = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?)(?:\s*(%|(?:{_UNIT_ALT})(?![A-Za-z0-9])))?",
    re.IGNORECASE,
)
_OLD_VALUE_LEAD_RE = re.compile(
    r"(?:\bnot|\bthan|\bfrom|\bwas|\bpreviously|\bformerly)\s+(?:about\s+|~\s*)?$", re.IGNORECASE
)
_STOPWORDS = frozenset(
    "the and for that with this are was any all but can will must should has have its from into "
    "than then when what which there their does been being per not never without nor cannot "
    "longer one two out use used via".split()
)
_CONTENT_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_QUANTITY_SEP = r"[\s_-]+"


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


_IDENT_RE = re.compile(r"[\w.]+(?:[-/][\w.]+)+")


def _prose_negation(line: str) -> bool:
    """A negation word in `line` outside hyphen/underscore/slash-joined identifiers: `never` inside
    a cited ruling id or filename is a name, not a negation."""
    return NEGATION_RE.search(_IDENT_RE.sub(lambda m: " " * len(m.group(0)), line)) is not None


def _cites_id(line: str, rid: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9._-]){re.escape(rid)}(?![A-Za-z0-9_-])", line) is not None


_SCOPE_OUT_RE = re.compile(
    r"\bout[\s-]of[\s-]scope\b|\bnot\s+in\s+scope\b|\boutside\s+(?:the\s+|this\s+)?(?:scope|plan|run|ask)\b"
    r"|\bscop(?:e|es|ed|ing)\s+(?:\w+\s+)?out\b|\bexcluded\b"
    r"|\b(?:do|does|will)\s+not\s+(?:touch|cover|address|include|handle)\b"
    r"|\bnot\s+part\s+of\s+(?:this|the)\s+(?:plan|run|ask)\b",
    re.IGNORECASE,
)


def _scope_out(line: str) -> bool:
    """`line` scopes something out of the run and says nothing else negative: a scope-out phrase
    whose own negation ("not in scope") is its only one. A line that also overrides, rejects or
    drops keeps its negation and is not a scope-out."""
    if not _SCOPE_OUT_RE.search(line):
        return False
    return not _prose_negation(_SCOPE_OUT_RE.sub(lambda m: " " * len(m.group(0)), line))


def _scoped_out_ids(paragraph: str, rulings: list[Ruling]) -> set[str]:
    """Ids of `rulings` cited on a scope-out line of `paragraph`."""
    lines = [ln for ln in paragraph.splitlines() if _scope_out(ln)]
    return {r.id for r in rulings if any(_cites_id(ln, r.id) for ln in lines)}


def _scope_hit(paragraph: str, ruling: Ruling) -> bool:
    if _cites_id(paragraph, ruling.id):
        return True
    low = paragraph.lower()
    words = _WORD_RE.findall(low)
    for tok in ruling.scope:
        t = tok.lower().strip()
        if not t:
            continue
        if any(c in t for c in "*?["):
            if any(fnmatch.fnmatchcase(w, t) for w in words):
                return True
        elif re.search(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])", low):
            return True
    return False


def _blank_quoted(paragraph: str) -> str:
    """`paragraph` with double-quoted and backticked spans blanked (offsets kept): a defer phrase
    that is quoted is being mentioned, not made."""
    return _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), paragraph)


_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t].*$", re.MULTILINE)


def _blank_headings(paragraph: str) -> str:
    """`paragraph` with markdown heading lines blanked (offsets kept): a heading such as
    `## Open questions` titles a section and defers nothing; the questions under it are scanned
    as their own paragraphs."""
    return _HEADING_RE.sub(lambda m: " " * len(m.group(0)), paragraph)


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _unit_info(unit: str) -> tuple[str, float]:
    u = unit.strip().lower()
    return _UNITS.get(u, (u, 1.0))


def _numbers(sentence: str) -> list[tuple[float, str]]:
    """(base value, dimension) for each number in `sentence`; dimension is "" for a bare number.
    A number led by an old-value marker ("not 5 mm", "from 5 mm") is skipped."""
    out: list[tuple[float, str]] = []
    for m in _NUMBER_RE.finditer(sentence):
        if _OLD_VALUE_LEAD_RE.search(sentence[: m.start()]):
            continue
        val = float(m.group(1))
        if m.group(2):
            dim, factor = _unit_info(m.group(2))
            out.append((val * factor, dim))
        else:
            out.append((val, ""))
    return out


def _quantity_re(quantity: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(_QUANTITY_SEP, quantity.strip()) if w]
    joined = _QUANTITY_SEP.join(words)
    return re.compile(rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])", re.IGNORECASE)


def _satisfies(body: float, target: float, comparator: str) -> bool:
    close = math.isclose(body, target, rel_tol=1e-9, abs_tol=1e-12)
    if comparator == "==":
        return close
    if comparator == "<=":
        return close or body < target
    if comparator == ">=":
        return close or body > target
    if comparator == "<":
        return body < target and not close
    return body > target and not close


def _target_mismatch(sentences: list[str], ruling: Ruling) -> bool:
    for t in ruling.targets:
        dim, factor = _unit_info(t.unit) if t.unit else ("", 1.0)
        want = t.value * factor
        qre = _quantity_re(t.quantity)
        for s in sentences:
            if not qre.search(s):
                continue
            for val, d in _numbers(s):
                if d == dim and not _satisfies(val, want, t.comparator):
                    return True
    return False


def _content_words(sentence: str) -> set[str]:
    return {w for w in _CONTENT_WORD_RE.findall(sentence.lower()) if w not in _STOPWORDS}


def _polarity(sentence: str) -> int:
    return len(NEGATION_RE.findall(sentence)) % 2


def _silent_value(para_sentences: list[str], ruling: Ruling) -> bool:
    target_dims = {_unit_info(t.unit)[0] for t in ruling.targets if t.unit}
    scope_words = {w for tok in ruling.scope for w in _CONTENT_WORD_RE.findall(tok.lower())}
    facts = [
        (_numbers(s), _content_words(s) - scope_words) for s in _sentences(ruling.text)
    ]
    for fact_nums, fact_words in facts:
        fact_nums = [(v, d) for v, d in fact_nums if d and d not in target_dims]
        if not fact_nums:
            continue
        for s in para_sentences:
            if not (fact_words & (_content_words(s) - scope_words)):
                continue
            body_nums = _numbers(s)
            for _, dim in fact_nums:
                same = [v for v, d in body_nums if d == dim]
                if same and not any(
                    math.isclose(v, fv, rel_tol=1e-9) for v in same for fv, fd in fact_nums if fd == dim
                ):
                    return True
    return False


def _silent_negation(para_sentences: list[str], ruling: Ruling) -> bool:
    for rs in _sentences(ruling.text):
        rwords = _content_words(rs)
        if len(rwords) < 3:
            continue
        rpol = _polarity(rs)
        for s in para_sentences:
            if _cites_id(s, ruling.id) or _polarity(s) == rpol:
                continue
            if len(rwords & _content_words(s)) / len(rwords) >= 0.7:
                return True
    return False


def cleared_ids(text: str, rulings: Iterable[Ruling], root: Path) -> set[str]:
    """Ruling ids cleared by a valid `pm_turn:` citation in `text` (see module docstring)."""
    by_id = {r.id: r for r in rulings}
    root = Path(root)
    out: set[str] = set()
    for line in text.splitlines():
        m = _PM_TURN_RE.match(line)
        if not m:
            continue
        ref, date, rid = m.groups()
        ruling = by_id.get(rid)
        if ruling is None or date <= ruling.recorded:
            continue
        p = Path(ref)
        if p.is_absolute() or ".." in p.parts:
            continue
        if (root / p).is_file():
            out.add(rid)
    return out


def detect(text: str, rulings: Iterable[Ruling], root: Path) -> dict:
    """Gate `text` against the accepted rulings; return a GateReply-shaped dict.

    `root` resolves `pm_turn:` refs. The reply carries `route` only on refuse and `reasons`
    (ruling id -> reason codes) only when a ruling is contradicted.
    """
    accepted = [r for r in rulings if r.status == STATUS_ACCEPTED]
    cleared = cleared_ids(text, accepted, root)
    deferred: list[str] = []
    unnamed: list[str] = []
    for para in _paragraphs(text):
        scoped_out = _scoped_out_ids(para, accepted)
        rest = "\n".join(ln for ln in para.splitlines() if not (scoped_out and _scope_out(ln)))
        phrases = [
            m
            for m in DEFER_RE.finditer(_blank_headings(_blank_quoted(para)))
            if not (scoped_out and _scope_out(_line_of(para, m.start())))
        ]
        if not phrases:
            continue
        if "?" not in para:
            unnamed.extend(_line_of(para, m.start()) for m in phrases)
        for r in accepted:
            if r.id not in deferred and r.id not in cleared and _scope_hit(rest if r.id in scoped_out else para, r):
                deferred.append(r.id)

    contradicted: list[str] = []
    reasons: dict[str, list[str]] = {}

    def _flag(rid: str, reason: str) -> None:
        if rid not in contradicted:
            contradicted.append(rid)
        if reason not in reasons.setdefault(rid, []):
            reasons[rid].append(reason)

    for line in text.splitlines():
        if _PM_TURN_RE.match(line):
            continue
        explicit = _CONTRADICTS_RE.match(line)
        nline = _norm(line)
        for r in accepted:
            if r.id in contradicted or r.id in cleared:
                continue
            if explicit and explicit.group(1).strip().rstrip(".,;") == r.id:
                _flag(r.id, "explicit_contradicts")
            elif _prose_negation(line) and not _scope_out(line) and (
                _cites_id(line, r.id) or (_norm(r.text) and _norm(r.text) in nline)
            ):
                _flag(r.id, "cited_with_negation")

    body_lines = [
        ln for ln in text.splitlines() if not _PM_TURN_RE.match(ln) and not _CONTRADICTS_RE.match(ln)
    ]
    body = _blank_quoted("\n".join(body_lines))
    body_sentences = _sentences(body)
    for r in accepted:
        if r.id in cleared:
            continue
        if r.targets and _target_mismatch(body_sentences, r):
            _flag(r.id, "target_mismatch")
    for para in _paragraphs(body):
        para_sentences = _sentences(para)
        for r in accepted:
            if r.id in cleared or not _scope_hit(para, r):
                continue
            if _silent_value(para_sentences, r):
                _flag(r.id, "silent_override_value")
            if _silent_negation(para_sentences, r):
                _flag(r.id, "silent_override_negation")

    unnamed = list(dict.fromkeys(unnamed))
    if not (deferred or contradicted or unnamed):
        return {
            "verdict": "pass",
            "deferred": [],
            "contradicted": [],
            "unnamed_deferrals": [],
            "message": "no accepted ruling deferred or contradicted",
        }
    parts = []
    if deferred:
        parts.append(f"defers accepted ruling {', '.join(deferred)}")
    if contradicted:
        why = "; ".join(f"{rid}: {', '.join(reasons[rid])}" for rid in contradicted)
        parts.append(f"contradicts accepted ruling {', '.join(contradicted)} ({why})")
    if unnamed:
        parts.append(f"defers to the PM with no named question ({len(unnamed)})")
    out: dict = {
        "verdict": "refuse",
        "deferred": deferred,
        "contradicted": contradicted,
        "unnamed_deferrals": unnamed,
        "route": GATE_ROUTE,
        "message": (
            f"Text {'; '.join(parts)}. "
            "Ask coordinator:apm the named question; it rules or flags it to the PM."
        ),
    }
    if reasons:
        out["reasons"] = reasons
    return out


def _line_of(paragraph: str, offset: int) -> str:
    start = paragraph.rfind("\n", 0, offset) + 1
    end = paragraph.find("\n", offset)
    return paragraph[start : end if end != -1 else len(paragraph)].strip()
