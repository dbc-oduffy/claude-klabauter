"""Shared rule for a ledger row credited by a named pre-halt commit: the commit-sha token rule
and the credit predicate, used by the refusal, ledger and commit legs. Stdlib only; no git, no I/O."""
from __future__ import annotations

import re
from typing import Sequence

SHA_TOKEN_PATTERN: str = r"\b[0-9a-f]{7,40}\b"
_SHA = re.compile(SHA_TOKEN_PATTERN)

# JS twin of `_row_credited`: same regex, digit-and-letter test and either-direction prefix match.
CREDIT_MATCH_JS: str = (
    "(metBy, knownText) => {\n"
    "  const hex = (s) => (String(s ?? '').toLowerCase().match(/\\b[0-9a-f]{7,40}\\b/g) ?? [])"
    ".filter((t) => /[0-9]/.test(t) && /[a-f]/.test(t));\n"
    "  const named = hex(Array.isArray(metBy) ? metBy.join(' ') : metBy);\n"
    "  const known = hex(knownText);\n"
    "  return named.some((n) => known.some((k) => k.startsWith(n) || n.startsWith(k)));\n"
    "}"
)


def sha_tokens(text: object) -> list[str]:
    """Lowercased 7-40 hex tokens of str(text) that hold both a digit and a letter, in order, de-duplicated."""
    out: list[str] = []
    for tok in _SHA.findall(str(text).lower()):
        if tok not in out and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            out.append(tok)
    return out


def note_rerules(notes: Sequence[object]) -> bool:
    """True when any revise note names a sha token (the refusal leg's re-ruling test)."""
    return any(sha_tokens(n) for n in notes)


def _row_credited(met_by: object, known: Sequence[str]) -> bool:
    text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by)
    return any(k.startswith(n) or n.startswith(k) for n in sha_tokens(text) for k in known)


def credited_rows(resume: object, verdict: object) -> frozenset[str]:
    """Ids of `resume.blocking` rows the verdict judges status 'met' whose met_by (str or list)
    names a sha token that prefix-matches (either direction) a sha token of the resume's
    revise_notes, or of that row's resume.remaining[id] evidence/remaining. Empty when the resume
    carries no revise note, or either argument is malformed."""
    try:
        if not isinstance(resume, dict) or not isinstance(verdict, dict):
            return frozenset()
        notes, blocking = resume.get("revise_notes"), resume.get("blocking")
        items, remaining = verdict.get("items"), resume.get("remaining")
        if not isinstance(notes, list) or not notes or not isinstance(blocking, list):
            return frozenset()
        if not isinstance(items, list):
            return frozenset()
        if not isinstance(remaining, dict):
            remaining = {}
        note_shas = sha_tokens(" ".join(str(n) for n in notes))
        out: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("status") != "met":
                continue
            rid = item.get("id")
            if rid not in blocking:
                continue
            rec = remaining.get(rid)
            rec = rec if isinstance(rec, dict) else {}
            known = note_shas + sha_tokens(f"{rec.get('evidence', '')} {rec.get('remaining', '')}")
            if _row_credited(item.get("met_by"), known):
                out.add(str(rid))
        return frozenset(out)
    except Exception:
        return frozenset()
