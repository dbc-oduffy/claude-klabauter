"""Shipped-file claims in a delivery note, judged in process against the landing set and HEAD.

A note lists the files it shipped under a `What shipped` / `Shipped` heading; `shipped_gate`
refuses a landing whose note names a file the commit does not carry. No subprocess is spawned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.kept_work import HALT_RECORD_ONLY
from coordinator_core.ops.dispatch_emit.ledger_recovery import _posix
from coordinator_core.ops.dispatch_emit.reach_claims import _FENCE, _VERDICTS_HEAD

SHIPPED_CLAIM_REFUSED = "shipped-claim-unbacked"

SELF_APPLIED_WITHHOLDS = frozenset({HALT_RECORD_ONLY})  # reasons the halt path stamps on a run's own diff

SHIPPED_VERDICTS = ("backed", "untracked", "not-in-commit", "withheld", "unresolved")

_REFUSING = ("untracked", "not-in-commit", "withheld")
_ANCHOR_CAP = 300
_SHIPPED_HEAD = re.compile(r"^(#{2,6})[ \t]+(?:what[ \t]+)?shipped\b", re.I)
_ANY_HEAD = re.compile(r"^(#{1,6})[ \t]+\S")
_TICKED = re.compile(r"`([^`\n]+)`")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


_INPUT_WORDS = r"provenance|inputs?|sources?|references?|read-only"
_INPUT_BEFORE = re.compile(
    r"(?:\b(?:" + _INPUT_WORDS + r")\b[^`]{0,40}|\b(?:sourced|derived|taken|read|copied|cited)\s+from\s+)$", re.I
)
_INPUT_AFTER = re.compile(r"^\)?\s*[(\-—:,]\s*(?:as\s+|the\s+)*(?:" + _INPUT_WORDS + r")\b(?!\s*:)", re.I)
_INPUT_HEAD = re.compile(r"^#{3,6}[ \t]+(?:" + _INPUT_WORDS + r")\b", re.I)


def _cited_as_input(line: str, m: "re.Match[str]") -> bool:
    """True when the line presents the ticked path as an input/source/provenance, not delivered work."""
    return bool(_INPUT_BEFORE.search(line[: m.start()]) or _INPUT_AFTER.match(line[m.end():]))


@dataclass(frozen=True)
class ShippedClaim:
    path: str
    anchor: str


@dataclass(frozen=True)
class ShippedVerdict:
    claim: ShippedClaim
    verdict: str
    why: str


def _clean(token: str) -> str:
    tok = token.strip()
    tok = tok.split("#", 1)[0]
    tok = _LINE_SUFFIX.sub("", tok)
    return _posix(tok).strip()


def extract_shipped_claims(note_text: str) -> tuple[ShippedClaim, ...]:
    """Each distinct backticked path under a `What shipped`/`Shipped` heading (H2-H6), in note order.

    The section runs to the next heading of the same or higher level; fenced blocks and everything
    from `## Verdicts` on are ignored. A `:<line>` or `#<anchor>` suffix is dropped.
    """
    head = _VERDICTS_HEAD.search(note_text)
    text = note_text[: head.start()] if head else note_text
    text = _FENCE.sub("", text)
    level = 0
    inputs = False
    seen: set[str] = set()
    out: list[ShippedClaim] = []
    for line in text.splitlines():
        h = _ANY_HEAD.match(line)
        if h:
            depth = len(h.group(1))
            inputs = bool(level and depth > level and _INPUT_HEAD.match(line))
            if _SHIPPED_HEAD.match(line):
                level = depth
            elif level and depth <= level:
                level = 0
            continue
        if not level:
            continue
        if inputs:
            continue
        for m in _TICKED.finditer(line):
            path = _clean(m.group(1))
            if not path or re.search(r"\s", path) or path in seen:
                continue
            if _cited_as_input(line, m):
                continue
            seen.add(path)
            out.append(ShippedClaim(path, line.strip()[:_ANCHOR_CAP]))
    return tuple(out)


def content_withheld(withheld_paths) -> set[str]:
    """Paths withheld for a content reason. A Mapping[path, reason] drops reasons in
    `SELF_APPLIED_WITHHOLDS` (the halt path's own record of the run's diff); a bare iterable of
    paths is all content withholds."""
    if isinstance(withheld_paths, Mapping):
        return {_posix(p) for p, why in withheld_paths.items() if why not in SELF_APPLIED_WITHHOLDS}
    return {_posix(p) for p in withheld_paths}


def _safe_rel(path: str) -> bool:
    p = PurePosixPath(path)
    return not (p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", path))


def judge_shipped_claims(
    root: Path, claims: Iterable[ShippedClaim], *, landing_paths=(), deleted_paths=(), withheld_paths=(),
) -> tuple[ShippedVerdict, ...]:
    """One verdict per claim: withheld, backed, untracked, not-in-commit, else unresolved.

    Precedence runs in that order. Reads HEAD's tree and the worktree in process; a claim that
    names no file, a directory, or a file whose checkin filter cannot be reproduced is
    `unresolved`, which never refuses. A bare file name (no directory) is therefore never refused.
    Coupling: `_FILTERED`/`_worktree_blob` are land_run privates, imported lazily to avoid the cycle.
    """
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
    from coordinator_core.ops.dispatch_emit.land_run import _FILTERED, _worktree_blob

    landing = {_posix(p) for p in landing_paths}
    deleted = {_posix(p) for p in deleted_paths}
    withheld = content_withheld(withheld_paths)
    head = head_sha(root, fresh=True)
    common = resolve_git_common_dir(root)
    out: list[ShippedVerdict] = []
    for claim in claims:
        rel = claim.path
        verdict = lambda v, why, claim=claim: out.append(ShippedVerdict(claim, v, why))  # noqa: E731

        if rel in withheld:
            verdict("withheld", "the landing withholds this path")
            continue
        if rel in landing or rel in deleted:
            verdict("backed", "the landing carries this path")
            continue
        if not _safe_rel(rel) or (root / rel).is_dir():
            verdict("unresolved", "not a repo file path")
            continue
        if not head:
            verdict("unresolved", "HEAD is unreadable")
            continue
        at_head = _blob_at_commit(common, head, rel)
        if at_head is None:
            verdict("unresolved", "HEAD's tree is unreadable")
            continue
        now = _worktree_blob(root, rel)
        if now is None:
            verdict("unresolved", "no such file on disk or tracked and absent from disk")
        elif at_head is ABSENT:
            verdict("untracked", "on disk, absent at HEAD, and not in the landing")
        elif now == _FILTERED:
            verdict("unresolved", "its checkin filter cannot be reproduced in process")
        elif now == at_head:
            verdict("backed", "tracked at HEAD with unchanged bytes")
        else:
            verdict("not-in-commit", "tracked, differs from HEAD, and not in the landing")
    return tuple(out)


def shipped_refusal(verdicts: Iterable[ShippedVerdict]) -> Optional[str]:
    """None unless a verdict refuses; else the refusal naming the first such claim."""
    for v in verdicts:
        if v.verdict in _REFUSING:
            return (
                f"{SHIPPED_CLAIM_REFUSED}: `{v.claim.path}` is {v.verdict}: {v.why}. "
                f"If `{v.claim.path}` is an input you read rather than work you delivered, cite it as "
                "provenance (e.g. 'source: `path`') or drop it from the shipped list; otherwise "
                "correct the change. Then re-fire the same sizing."
            )
    return None


def shipped_gate(
    root, note_text: Optional[str], *, landing_paths=(), deleted_paths=(), withheld_paths=(),
) -> tuple[Optional[str], dict]:
    """``(refusal | None, reply_field)``; the reply is empty when the note carries no shipped claim."""
    claims = extract_shipped_claims(note_text) if note_text else ()
    if not claims:
        return None, {}
    vs = judge_shipped_claims(
        Path(root), claims, landing_paths=landing_paths, deleted_paths=deleted_paths,
        withheld_paths=withheld_paths,
    )
    return shipped_refusal(vs), {
        "checked": len(vs),
        "unbacked": [v.claim.anchor for v in vs if v.verdict in _REFUSING],
        "unresolved": [v.claim.anchor for v in vs if v.verdict == "unresolved"],
    }
