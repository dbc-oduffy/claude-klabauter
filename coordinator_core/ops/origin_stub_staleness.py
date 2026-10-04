"""Origin-stub staleness: the pair-join primitives shared with
``handoff.close_origin_stub`` and the report-only survey of live origin stubs
whose ``(roadmap_id, stub_id)`` pair a shipping record already carries.

Imports nothing from ``handoff_close_origin_stub``; that op imports the
primitives from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.lifecycle_constants import SPEC_RIPE_STATUSES
from coordinator_core.session import record_homes

_HANDOFFS_ROOT = Path(record_homes.home_dir("", "handoffs")).as_posix()
_PREFILTER_STUB = b"stub_id"
_PREFILTER_CLOSES = b"closes_stubs"
_LIVE_STATES = frozenset({"ready_to_fire", "awaiting_gate"})

_BATON_KINDS = frozenset(
    {"spinoff"} | set(kind_values_for_canonical("roadmap-baton"))
)


def is_baton_kind(kind: str | None) -> bool:
    """Origin-stub kinds this op is allowed to close (mirrors the bash's
    `spinoff|spinoff-roadmap` case match).

    C3 (baton-kind-vocabulary migration): retires the former
    `_BATON_KINDS = {"spinoff", "spinoff-roadmap"}` set in favor of the
    canonical `baton_class()` derivation (C2/D2) plus one explicit
    compatibility literal.

    FINDING — the original two-member set does not correspond to one
    `baton_class`: `spinoff` derives `deflection`, but `spinoff-roadmap`
    (D1's still-live pre-rename source name for `roadmap-baton`) derives
    `intention`. Preserved verbatim, not silently narrowed — report only,
    per this chunk's brief.
    """
    return kind in _BATON_KINDS


def read_pair(meta: dict) -> Optional[Tuple[str, str]]:
    rid = meta.get("roadmap_id")
    sid = meta.get("stub_id")
    rid_s = rid.strip() if isinstance(rid, str) else ""
    sid_s = sid.strip() if isinstance(sid, str) else ""
    if rid_s and sid_s:
        return (rid_s, sid_s)
    return None


def read_closes_stubs(meta: dict) -> List[Tuple[str, str]]:
    raw = meta.get("closes_stubs")
    if not isinstance(raw, list):
        return []
    pairs: List[Tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pair = read_pair(entry)
        if pair is not None:
            pairs.append(pair)
    return pairs


@dataclass(frozen=True)
class StaleOriginStub:
    """One live stub whose pair a shipping record carries.

    ``path`` and ``evidence_path`` are repo-relative forward-slash strings;
    ``evidence_kind`` is ``"plan"`` or ``"handoff"``.
    """

    path: str
    pair: Tuple[str, str]
    deployment_state: str
    evidence_path: str
    evidence_kind: str


@dataclass(frozen=True)
class OriginStubSurvey:
    """Result of ``survey``. ``unreadable`` names every file skipped on
    ``OSError``/``UnicodeDecodeError``; nothing is dropped silently."""

    stale: Tuple[StaleOriginStub, ...]
    live_with_pair: int
    unreadable: Tuple[str, ...]


def survey(repo_root: Path) -> OriginStubSurvey:
    """Report live origin stubs whose pair a shipping record already carries.

    Report-only: never flips a stub and mutates nothing (DR-263 section 6).
    The remedy it names is re-running ``handoff.close_origin_stub`` with the
    evidence record.

    One pass per corpus root: ``state/handoffs/*.md``,
    ``archive/handoffs/**/*.md``, ``docs/plans/*.md`` and
    ``archive/specs/**/*.md``. Each file is read once as bytes and decoded and
    frontmatter-parsed only if it contains ``b"stub_id"`` or
    ``b"closes_stubs"``. A live baton-kind stub in ``state/handoffs/`` at
    ``ready_to_fire``/``awaiting_gate`` is stale when its pair joins a plan
    whose status is in ``SPEC_RIPE_STATUSES`` or a handoff at
    ``deployment_state: shipped`` (its own pair or its ``closes_stubs``).
    """
    live: List[Tuple[str, Tuple[str, str], str]] = []
    evidence: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    unreadable: List[str] = []

    def _rel(p: Path) -> str:
        return p.relative_to(repo_root).as_posix()

    def _files(root: str, recursive: bool) -> List[Path]:
        base = repo_root / root
        if not base.is_dir():
            return []
        found = base.rglob("*.md") if recursive else base.glob("*.md")
        return sorted(found)

    roots = (
        (_HANDOFFS_ROOT, False, "handoff"),
        ("archive/handoffs", True, "handoff"),
        ("docs/plans", False, "plan"),
        ("archive/specs", True, "plan"),
    )
    for root, recursive, kind in roots:
        for path in _files(root, recursive):
            rel = _rel(path)
            try:
                raw = path.read_bytes()
                if _PREFILTER_STUB not in raw and _PREFILTER_CLOSES not in raw:
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                unreadable.append(rel)
                continue
            if split_frontmatter(text) is None:
                continue
            meta = _parse_frontmatter(text)
            pairs = read_closes_stubs(meta)
            own = read_pair(meta)
            if own is not None:
                pairs.append(own)
            if kind == "plan":
                if meta.get("status") in SPEC_RIPE_STATUSES:
                    for pair in pairs:
                        evidence.setdefault(pair, []).append((rel, "plan"))
                continue
            state = meta.get("deployment_state")
            if state == "shipped":
                for pair in pairs:
                    evidence.setdefault(pair, []).append((rel, "handoff"))
            elif (
                root == _HANDOFFS_ROOT
                and state in _LIVE_STATES
                and is_baton_kind(meta.get("kind"))
                and own is not None
            ):
                live.append((rel, own, state))

    stale: List[StaleOriginStub] = []
    for rel, pair, state in live:
        carriers = [e for e in sorted(evidence.get(pair, ())) if e[0] != rel]
        if carriers:
            stale.append(StaleOriginStub(rel, pair, state, carriers[0][0], carriers[0][1]))
    return OriginStubSurvey(
        stale=tuple(stale), live_with_pair=len(live), unreadable=tuple(unreadable)
    )
