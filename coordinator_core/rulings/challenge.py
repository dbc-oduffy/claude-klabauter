"""Lexical challenger for deferrals and claimed named reasons; advisory, never denies.

A tripwire, not a judge. Limits, stated so a caller never over-reads a pass:
  - Spans: `fields.unapplied_reason`, then each `fields.deferrals[]` entry, then each paragraph of
    `text` (blank-line separated). A paragraph with no defer phrase and no closed-list cue yields
    no verdict; a field always yields one.
  - A `detect.DEFER_RE` hit is a PM hand-off: it passes when its span holds `?`, else it is a
    challenge; `reason_class` is null either way.
  - Defer phrases beyond DEFER_RE: "not now", "later", "follow-up", "out of scope", "next pass".
    One with no closed-list class is a challenge.
  - Closed-list classes (tradeoff, another_repo, irreversible, plan_sized) are recognised by cue
    phrase and pass only when substantiated: a repo name, a plan or baton path or id, a named
    irreversible act, two sides of a trade. Substantiation is a token pattern; a plausible-looking
    name that is not a real repo or plan passes.
  - A phrase inside double quotes or backticks is a mention, not a deferral, and is skipped; a
    markdown heading is skipped.
"""
from __future__ import annotations

import re
from typing import Any

from coordinator_core.rulings.detect import (
    DEFER_RE,
    _blank_headings,
    _blank_quoted,
    _content_words,
    _paragraphs,
)

CLASSES = ("tradeoff", "another_repo", "irreversible", "plan_sized")
_I = re.IGNORECASE

_DEFER_PHRASE_RE = re.compile(
    r"\bnot\s+now\b|\blater\b|\bfollow[\s-]?ups?\b|\bout[\s-]of[\s-]scope\b|\bnext\s+pass\b", _I
)

_CUE: dict[str, re.Pattern[str]] = {
    "another_repo": re.compile(
        r"\b(?:another|other|different|separate|sibling|external)\s+repos?(?:sitory|sitories)?\b"
        r"|\bcross[\s-]repo\b|\bnot\s+(?:this|our)\s+repo\b",
        _I,
    ),
    "plan_sized": re.compile(
        r"\bplan[\s-]sized\b|\bplan[\s-]weight\b|\bneeds?\s+(?:its\s+own|a)\s+plan\b", _I
    ),
    "irreversible": re.compile(
        r"\birreversible\b|\b(?:can(?:no|')?t|cannot)\s+be\s+undone\b|\bone[\s-]way\b", _I
    ),
    "tradeoff": re.compile(
        r"\btrade[\s-]?offs?\b|\btraded?\s+(?:off\s+)?against\b|\bat\s+the\s+cost\s+of\b", _I
    ),
}

_REPO_STOP = frozenset(
    "cross-repo follow-up follow-ups plan-sized plan-weight non-owned one-way next-pass "
    "out-of-scope not-now another-repo other-repo trade-off".split()
)
_REPO_TOKEN_RE = re.compile(r"(?<![\w./-])(?:[A-Za-z][\w.]*(?:-[\w.]+)+|[\w.-]+/[\w.-]+)(?![\w/-])")
_REPO_NAME_RE = re.compile(r"\b(?:DoE|claude-klabauter|klabauter|example-retrieval-repo|uhura)\b", _I)
_PLAN_REF_RE = re.compile(
    r"[\w./-]*\b(?:plans?|batons?)/[\w./-]+|\bpln-[\w-]+|\bbtn-[\w-]+|\bbaton[-_][\w-]+"
    r"|\b\d{4}-\d{2}-\d{2}-[a-z][\w-]*",
    _I,
)
_ACT_RE = re.compile(
    r"\bforce[\s-]push\w*|\bpush(?:es|ed|ing)?\b|\bpublish\w*|\brelease[sd]?\b"
    r"|\bmerg\w+\s+(?:in)?to\s+main\b|\bp4\s+submit\b|\bshelv\w+|\b(?:send|sent|sending)\s+(?:an?\s+)?(?:e-?)?mail\b"
    r"|\bbranch\s+delet\w+|\bdelet\w+\s+(?:the\s+)?branch\b|\bhistory\s+rewrit\w+|\brewrit\w+\s+(?:the\s+)?history\b"
    r"|\bpull\s+request\b|\bthird[\s-]party\b",
    _I,
)
_SIDE_NOISE_RE = re.compile(r"\btrade[\s-]?offs?\b|\btraded?\s+off\b", _I)
_TRADE_CONN_RE = re.compile(r"\b(?:vs\.?|versus|against|at\s+the\s+cost\s+of)(?=\s|$)", _I)
_BETWEEN_RE = re.compile(r"\bbetween\s+(.+?)\s+and\s+(.+)", _I | re.DOTALL)
_SPAN_MAX = 160


def _excerpt(label: str, s: str) -> str:
    one = " ".join(s.split())
    return f"{label}: {one[:_SPAN_MAX]}"


def _repo_named(span: str, cue: re.Pattern[str]) -> bool:
    rest = cue.sub(" ", span)
    return _REPO_NAME_RE.search(rest) is not None or any(
        m.group(0).lower() not in _REPO_STOP for m in _REPO_TOKEN_RE.finditer(rest)
    )


def _traded(span: str) -> bool:
    for m in _TRADE_CONN_RE.finditer(span):
        left = _content_words(_SIDE_NOISE_RE.sub(" ", span[: m.start()]))
        right = _content_words(span[m.end() :])
        if left and right:
            return True
    b = _BETWEEN_RE.search(span)
    return bool(b and _content_words(b.group(1)) and _content_words(b.group(2)))


def _substantiation(cls: str, span: str) -> str | None:
    """None when `span` substantiates `cls`; else the missing element."""
    if cls == "another_repo":
        return None if _repo_named(span, _CUE[cls]) else "names no repo"
    if cls == "plan_sized":
        return None if _PLAN_REF_RE.search(span) else "names no plan or baton path or id"
    if cls == "irreversible":
        return None if _ACT_RE.search(span) else "names no irreversible act"
    return None if _traded(span) else "names no tradeoff sides (what against what)"


def _verdict(span: str, kind: str, cls: str | None, verdict: str, why: str) -> dict[str, Any]:
    return {"span": span, "kind": kind, "reason_class": cls, "verdict": verdict, "why": why}


def _classify(raw: str, label: str, *, field: bool) -> dict[str, Any] | None:
    """Verdict for one span, or None when a text paragraph holds nothing to challenge."""
    span = _excerpt(label, raw)
    seen = _blank_headings(_blank_quoted(raw))
    if DEFER_RE.search(seen):
        if "?" in raw:
            return _verdict(span, "deferral", None, "pass", "PM hand-off names a question")
        return _verdict(span, "deferral", None, "challenge", "defers to the PM with no named question")
    hits = [c for c in CLASSES if _CUE[c].search(seen)]
    if hits:
        missing: dict[str, str] = {}
        for cls in hits:
            gap = _substantiation(cls, raw)
            if gap is None:
                return _verdict(span, "named_reason", cls, "pass", f"{cls} reason is substantiated")
            missing[cls] = gap
        first = hits[0]
        return _verdict(
            span, "named_reason", first, "challenge", f"{first} reason unsubstantiated: {missing[first]}"
        )
    if _DEFER_PHRASE_RE.search(seen):
        return _verdict(
            span, "deferral", None, "challenge",
            "defers with no closed-list reason (tradeoff, another_repo, irreversible, plan_sized)",
        )
    if field:
        return _verdict(
            span, "deferral", None, "challenge",
            "unapplied reason names no closed-list reason (tradeoff, another_repo, irreversible, plan_sized)",
        )
    return None


def _field_spans(fields: Any) -> list[tuple[str, str]]:
    if not isinstance(fields, dict):
        return []
    out: list[tuple[str, str]] = []
    reason = fields.get("unapplied_reason")
    if isinstance(reason, str) and reason.strip():
        out.append(("unapplied_reason", reason))
    items = fields.get("deferrals")
    if isinstance(items, (list, tuple)):
        for i, item in enumerate(items):
            if isinstance(item, dict):
                item = item.get("reason") or item.get("text") or ""
            if isinstance(item, str) and item.strip():
                out.append((f"deferrals[{i}]", item))
    return out


def challenge(surface: str, text: str, fields: dict | None) -> dict[str, Any]:
    """Per-span verdicts for `text` and `fields`; `flag_em` is true when any verdict is a challenge."""
    verdicts: list[dict[str, Any]] = []
    for label, raw in _field_spans(fields):
        v = _classify(raw, f"fields.{label}", field=True)
        if v:
            verdicts.append(v)
    body = text if isinstance(text, str) else ""
    for i, para in enumerate(_paragraphs(body), 1):
        v = _classify(para, f"{surface}.text[{i}]", field=False)
        if v:
            verdicts.append(v)
    return {"verdicts": verdicts, "flag_em": any(v["verdict"] == "challenge" for v in verdicts)}
