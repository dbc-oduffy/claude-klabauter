"""Shipped-file claims in a delivery note, judged in process against the landing set and HEAD.

A note lists the files it shipped under a `What shipped` / `Shipped` heading; `shipped_gate`
refuses a landing whose note names a file the commit does not carry. No subprocess is spawned.

Two claim sources are reconciled: the note's claim set (verify agent, drawing on ledger evidence)
and the landing set (credited rows, owned paths). A refusing claim on a stranded row's file is
`row-ungraded` (EM credits the row); one owned by another run is `peer-owned` (never refuses). The
`owners` lookup runs once and only on the would-refuse path (DR-344).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.kept_work import HALT_RECORD_ONLY
from coordinator_core.ops.dispatch_emit.ledger_recovery import _posix
from coordinator_core.ops.dispatch_emit.reach_claims import _FENCE, _VERDICTS_HEAD

SHIPPED_CLAIM_REFUSED = "shipped-claim-unbacked"

SELF_APPLIED_WITHHOLDS = frozenset({HALT_RECORD_ONLY})  # reasons the halt path stamps on a run's own diff

ROW_GRADE_OWED = "partial-row-grade-owed"
NOT_DECLARED = "no row of this run declares it"

SHIPPED_VERDICTS = ("backed", "untracked", "not-in-commit", "withheld", "unresolved", "peer-owned", "row-ungraded", "foreign-dropped")

_REFUSING = ("untracked", "not-in-commit", "withheld")
_ROW_LEFT_OUT = re.compile(r"^row (\S+) stays out: (.*)$", re.S)
_ANCHOR_CAP = 300
_SHIPPED_HEAD = re.compile(r"^(#{2,6})[ \t]+(?:what[ \t]+)?shipped\b", re.I)
_ANY_HEAD = re.compile(r"^(#{1,6})[ \t]+\S")
_TICKED = re.compile(r"`([^`\n]+)`")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")


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
    listed: bool = True  # False: the ticked token sits in prose, not on a list item


@dataclass(frozen=True)
class ShippedVerdict:
    claim: ShippedClaim
    verdict: str
    why: str
    left_out: str = ""


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
            out.append(ShippedClaim(path, line.strip()[:_ANCHOR_CAP], bool(_LIST_ITEM.match(line))))
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


def _under(path: str, d: str) -> bool:
    return path.startswith(d + "/")


def _judge_dir(d, *, landing, deleted, withheld):
    """`(verdict, why)` for directory `d` from the landing alone: a directory is a claim only when
    the landing carries files under it; untracked or tracked-differing files never decide it."""
    carried = {p for p in landing | deleted if _under(p, d)}
    if not carried:
        return "unresolved", "a directory no landed row declares is a mention, not a claim"
    if all(p in withheld for p in carried):
        return "withheld", "the landing withholds every file it carries under it"
    return "backed", "the landing carries files under it"


def judge_shipped_claims(
    root: Path, claims: Iterable[ShippedClaim], *, landing_paths=(), deleted_paths=(), withheld_paths=(),
    own_paths=None, row_of=None, widened=(),
) -> tuple[ShippedVerdict, ...]:
    """One verdict per claim: withheld, backed, untracked, not-in-commit, else unresolved.

    Precedence runs in that order. Reads HEAD's tree and the worktree in process; a claim that
    names no file, or a file whose checkin filter cannot be reproduced, is `unresolved`, which
    never refuses. A bare file name (no directory) is therefore never refused. A directory claim
    is backed only by landing paths under it and otherwise unresolved. No subprocess is spawned.
    Each verdict's `left_out` is the landing's reason the file is not carried. A refusing verdict on
    a path in `row_of` ({path: (row id, grade)}) becomes `row-ungraded`, except a withhold carrying
    a reason (content or ownership): crediting the row would not land that file.
    `widened` (the run's recorded in-run widens) counts as declared writes beside `own_paths`.
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
    reasons = (
        {_posix(p): str(why) for p, why in withheld_paths.items()} if isinstance(withheld_paths, Mapping) else None
    )
    own = None if own_paths is None else {_posix(p) for p in own_paths} | {_posix(p) for p in widened}
    rows = {_posix(p): v for p, v in (row_of or {}).items()}
    out: list[ShippedVerdict] = []
    claims = tuple(claims)
    for claim in claims:
        rel = claim.path

        def verdict(v, why, claim=claim, rel=rel):
            left = why
            if v == "withheld" and reasons is not None:
                left = reasons.get(rel) or why
            elif v in ("untracked", "not-in-commit") and own is not None and rel not in own:
                left = NOT_DECLARED
            if v in _REFUSING and rel in rows and not (v == "withheld" and reasons is not None):
                cid, grade = rows[rel]
                v, left = "row-ungraded", f"row {cid} stays out: {grade}"
            out.append(ShippedVerdict(claim, v, why, left))

        if rel in withheld:
            verdict("withheld", "the landing withholds this path")
            continue
        if rel in landing or rel in deleted:
            verdict("backed", "the landing carries this path")
            continue
        if not _safe_rel(rel) or rel.rstrip("/") in ("", "."):
            verdict("unresolved", "not a repo file path")
            continue
        if not head:
            verdict("unresolved", "HEAD is unreadable")
            continue
        if rel.endswith("/") or (root / rel).is_dir():
            verdict(*_judge_dir(rel.rstrip("/"), landing=landing, deleted=deleted, withheld=withheld))
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


def _row_split(v: ShippedVerdict) -> tuple[str, str]:
    m = _ROW_LEFT_OUT.match(v.left_out)
    return (m.group(1), m.group(2)) if m else ("?", v.left_out)


def _item(v: ShippedVerdict) -> str:
    return (
        f"`{v.claim.path}` is {v.verdict}: claimed in the delivery note's What shipped ({v.claim.anchor}); "
        f"the landing left it out: {v.left_out or v.why}."
    )


def shipped_refusal(verdicts: Iterable[ShippedVerdict], run_id: Optional[str] = None) -> Optional[str]:
    """None unless a verdict refuses; else the refusal naming every refusing claim, unbacked first.

    The prefix is `shipped-claim-unbacked:` when any unbacked claim exists, else `partial-row-grade-owed:`.
    """
    vs = list(verdicts)
    unbacked = [v for v in vs if v.verdict in _REFUSING]
    rows = [v for v in vs if v.verdict == "row-ungraded"]
    if not unbacked and not rows:
        return None
    parts = [f"{SHIPPED_CLAIM_REFUSED if unbacked else ROW_GRADE_OWED}:"]
    parts += [_item(v) for v in unbacked + rows]
    if unbacked:
        parts.append(
            "If a path is an input you read rather than work you delivered, cite it as "
            "provenance (e.g. 'source: `path`') or drop it from the shipped list; otherwise "
            "correct the change. Then re-fire the same sizing."
        )
    for cid in dict.fromkeys(_row_split(v)[0] for v in rows):
        parts.append(
            f"Decide row {cid}: if its delivered edits are verified, credit it -- "
            f'workflow.revise run_id={run_id or "<run_id>"} credit_chunks=[{{chunk: "{cid}", evidence}}] -- '
            "then re-fire; else drop the path from What shipped."
        )
    return " ".join(parts)


def refused_code(reply: Mapping) -> str:
    """The refusal code for a `shipped_gate` reply: row-grade only when no unbacked claim exists."""
    if reply.get("row_grade_owed") and not reply.get("unbacked"):
        return ROW_GRADE_OWED
    return SHIPPED_CLAIM_REFUSED


def shipped_gate(
    root, note_text: Optional[str], *, landing_paths=(), deleted_paths=(), withheld_paths=(),
    own_paths=None, row_of=None, owners=None, run_id=None, widened=(),
) -> tuple[Optional[str], dict]:
    """``(refusal | None, reply_field)``; the reply is empty when the note carries no shipped claim.

    `owners(paths) -> {path: owner}` is called once, only with refusing claim paths in neither
    `own_paths` nor `row_of`; a mapped claim is `peer-owned` and does not refuse. A raise leaves the refusal.
    An untracked/not-in-commit claim no row declares (and no owner claims) is `foreign-dropped`: listed
    under `dropped_foreign`, never refusing. Withheld and declared-path claims still refuse.
    """
    claims = extract_shipped_claims(note_text) if note_text else ()
    if not claims:
        return None, {}
    vs = list(judge_shipped_claims(
        Path(root), claims, landing_paths=landing_paths, deleted_paths=deleted_paths,
        withheld_paths=withheld_paths, own_paths=own_paths, row_of=row_of, widened=widened,
    ))
    if owners is not None:
        skip = {_posix(p) for p in (own_paths or ())} | {_posix(p) for p in widened} | {_posix(p) for p in (row_of or {})}
        cand = [v.claim.path for v in vs if v.verdict in _REFUSING and v.claim.path not in skip]
        if cand:
            try:
                found = owners(cand)
            except Exception:
                found = {}
            found = found if isinstance(found, Mapping) else {}
            for i, v in enumerate(vs):
                if v.claim.path in cand and found.get(v.claim.path):
                    vs[i] = replace(v, verdict="peer-owned", left_out=f"owned by {found[v.claim.path]}")
    for i, v in enumerate(vs):
        if v.verdict in ("untracked", "not-in-commit") and v.left_out == NOT_DECLARED:
            vs[i] = replace(v, verdict="foreign-dropped")
    reply: dict = {
        "checked": len(vs),
        "unbacked": [v.claim.anchor for v in vs if v.verdict in _REFUSING],
        "unresolved": [v.claim.anchor for v in vs if v.verdict == "unresolved"],
    }
    peer = [{"path": v.claim.path, "owner": v.left_out.removeprefix("owned by ")} for v in vs if v.verdict == "peer-owned"]
    if peer:
        reply["peer_owned"] = peer
    dropped = [v.claim.path for v in vs if v.verdict == "foreign-dropped"]
    if dropped:
        reply["dropped_foreign"] = dropped
    owed = [
        {"chunk": _row_split(v)[0], "path": v.claim.path, "grade": _row_split(v)[1]}
        for v in vs if v.verdict == "row-ungraded"
    ]
    if owed:
        reply["row_grade_owed"] = owed
    return shipped_refusal(vs, run_id), reply
