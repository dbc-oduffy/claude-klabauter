"""
coordinator_core.claim_state — canonical ledger-first claim state accessor.

Purpose: one READ-ONLY accessor answering "who holds this handoff's claim, and
which of the two claim records answered" — replacing the ~30 decision sites
across this codebase that consult only the tracked-frontmatter mirror
(`status: claimed` / `claimed_by` / `claimed_at`), which is branch-dependent
and silently reverts on a branch switch.

THE INCIDENT THIS GENERALIZES A FIX FOR (2026-08-07, this repo, this plan's own
Problem section). A handoff was claimed on branch `two-tier-engine-root`
(commit 11fe08d51 stamped the frontmatter mirror). A peer moved the shared
worktree to `main`, which never carried that commit — the mirror reverted to
`status: open` with no claim fields while the branch-independent claim ledger
(`<common_dir>/coordinator-sessions/handoff-claims/<basename>/`, living inside
the git *directory*, not the worktree) still held the claim. A fully-worked
baton advertised itself for pickup with an entire session's work already done.

This module generalizes `baton_assemble/apply.py`'s `_ledger_claim_record` /
`_reconcile_claim_from_ledger` (the one call path that already implements this
remedy, and whose docstring names this same incident by commit sha) into a
single canonical accessor. It also re-homes `handoff_claim_dir` and
`_sessions_dir` out of `coordinator_core.ops.fleet._common`, which re-exports
both names for its existing importers.

POSTURE (mirrors `coordinator_core.ops.ownership_index.build_ownership_index`):
the ledger is authoritative; a frontmatter-mirror disagreement is a distinct,
inspectable state on the returned result — surfaced, never silently coerced to
either half and never trusted over the ledger.

Import discipline (load-bearing, see the plan's "Beware the import cycle"
anti-scope bullet): this module's own dependencies are limited to
`coordinator_core.lifecycle` (`git_common_dir`), `coordinator_core.liveness`
(`cs_claim_holder_live`), and `coordinator_core.frontmatter.primitives`.
NOTHING under `coordinator_core.ops.*` or `coordinator_core.session.*` — the
`coordinator_core.ops` package's `__init__` eager-imports every op module,
including `ops/session/reap.py`, which imports back from `session/claims.py`;
a module-level import of anything under `coordinator_core.ops.*` here would
trip that cycle. This module is a leaf specifically so it cannot.

Cost (hot path — `coverage.py`'s DAG fixpoint, and
`ops/dirty_tree_gate.py::_build_known_scope` runs this per handoff on the
dirty-tree gate): `resolve_claim_state` takes an optional pre-resolved
`common_dir`, matching `handoff_claim_dir`'s own existing signature, so a
caller that already has `common_dir` in hand (the hot-path callers do) never
triggers a second resolution. `lifecycle.git_common_dir` is itself
`lru_cache`d, so even the fallback-resolve path spawns no subprocess per call
— see `test_claim_state_accessor.py`'s explicit confirmation of this.

DR-084: reads stay dual-tolerant — `claimed_by` (canonical) and `consumed_by`
(legacy) on the mirror side, via the same `claimed_by`-wins-on-both-present
resolution `coverage.py::_parse_handoff_consumed_by` uses.

Negative-spec:
  - Does NOT write — not the ledger, not the frontmatter mirror. Read-only,
    unconditionally.
  - Does NOT reuse or re-derive `archival.claimed_or_shipped_at_path` — that
    predicate belongs to plan chunk C10, which depends on this module. The
    import direction is archival -> claim_state, one way only; a mutual
    import would recreate the exact cycle this module exists to avoid.
  - Does NOT gate ledger membership on anything beyond
    `cs_claim_holder_live` — a dead-holder ledger claim degrades to "no
    ledger claim" for this accessor's purposes (the mirror, if any, still
    answers); it does not raise and does not silently claim a live ledger
    entry belongs to a dead holder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.liveness import cs_claim_holder_live

# ---------------------------------------------------------------------------
# Claim-dir path convention — re-homed from coordinator_core.ops.fleet._common
# (handoff_claim_dir / _sessions_dir lived at ~lines 696/704 there). That
# module re-exports both names for its existing importers; this is now the
# single source of truth for the handoff-claim-dir convention.
# ---------------------------------------------------------------------------

#: Mirrors ops.fleet._common._CLAIM_SUBDIRS[0] ("handoff-claims") — kept as a
#: local literal rather than importing _common (which sits under
#: coordinator_core.ops.*, forbidden here — see module docstring's import
#: discipline). _common.py's own plan_claim_dir/_CLAIM_SUBDIRS are unchanged
#: and untouched by this move.
_HANDOFF_CLAIM_SUBDIR = "handoff-claims"


def _sessions_dir(common_dir: Path) -> Path:
    """Return the coordinator-sessions dir: <common_dir>/coordinator-sessions/.

    The sessions dir lives inside the git dir (common_dir), NOT the worktree.
    """
    return common_dir / "coordinator-sessions"


def handoff_claim_dir(common_dir: Path, handoff_path: Path) -> Path:
    """Derive the handoff claim-lock dir for a given handoff path.

    <common_dir>/coordinator-sessions/handoff-claims/<handoff_path.name> —
    the single shared claim-dir convention (see
    ``coordinator_core.ops.fleet._common``'s original docstring for this
    function, preserved verbatim there via re-export).
    """
    return _sessions_dir(common_dir) / _HANDOFF_CLAIM_SUBDIR / handoff_path.name


# ---------------------------------------------------------------------------
# The canonical ledger-first claim state accessor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimState:
    """Resolved claim state for one handoff, ledger-first with mirror fallback.

    Attributes:
        holder: The resolved claim holder session id, or None if neither
            source has a live claim.
        claimed_at: The resolved claim timestamp string (raw, as stored — no
            reformatting), or None.
        source: Which source answered — "ledger", "mirror", or "none".
            Ledger wins whenever it holds a live claim, regardless of what
            the mirror says (POSTURE: ledger authoritative).
        disagreement: True when the ledger holds a live claim but the
            frontmatter mirror carries no claimed_by/consumed_by — the
            branch-switch-revert desync this module exists to detect. This is
            a DISTINCT, inspectable flag — the accessor never silently
            coerces a disagreement into either half's answer.

            This flag is
            DELIBERATELY NARROWER than "any ledger/mirror disagreement". It
            is `bool(ledger_holder) and not bool(mirror_holder)` only — it
            does NOT flag the case where both sides hold a live claim but
            disagree on *who* (`ledger_holder != mirror_holder`, both
            non-None). That narrowing is intentional: the only real-world
            incident shape this module generalizes a fix for is
            branch-switch-revert-to-empty (see module docstring), not a
            same-slot holder mismatch. A holder mismatch is NOT hidden by
            this narrowing — it remains independently detectable by any
            caller via the `ledger_holder`/`mirror_holder` fields below,
            which this dataclass always exposes regardless of `disagreement`.
        ledger_holder: The raw ledger-side holder (None if no live ledger
            claim), independent of which source `holder`/`source` above
            resolved to.
        mirror_holder: The raw mirror-side holder (None if the mirror carries
            no claimed_by/consumed_by), independent of `holder`/`source`.
    """

    holder: Optional[str]
    claimed_at: Optional[str]
    source: str
    disagreement: bool
    ledger_holder: Optional[str] = None
    mirror_holder: Optional[str] = None


def _read_ledger_claim(claim_dir: Path) -> Optional[tuple]:
    """Read (session_id, claimed_at) off a claim-lock dir, or None.

    Read-only. Returns None on any missing/unreadable/empty-session-id
    record — "no usable ledger evidence", never raised as an error (mirrors
    baton_assemble/apply.py's original `_ledger_claim_record` degrade
    discipline this function is generalized from).
    """
    if not claim_dir.is_dir():
        return None
    # em-authored-finding — degrade (OSError, ValueError) on both
    # reads, not just OSError. read_text(encoding="utf-8") raises
    # UnicodeDecodeError (a ValueError subclass) on invalid UTF-8, which
    # propagated uncaught to ~25 callers all written assuming this accessor
    # degrades rather than raises. A malformed record is "no usable ledger
    # evidence", never a live claim and never a raise.
    try:
        session_id = (claim_dir / "session_id").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not session_id:
        # A pid-only legacy claim dir carries no session id — no usable
        # holder identity, so treat as no ledger record (mirrors the
        # generalized-from function's own degrade).
        return None
    try:
        claimed_at = (claim_dir / "claimed_at").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        claimed_at = ""
    return session_id, (claimed_at or None)


def _read_mirror_claim(handoff_path: Path) -> tuple:
    """Read (holder, claimed_at) off the tracked-frontmatter mirror, or
    (None, None) on any read failure.

    DR-084 dual-tolerant: `claimed_by` (canonical) wins over `consumed_by`
    (legacy) when both are present — mirrors
    `coverage.py::_parse_handoff_consumed_by`'s exact resolution order and 4
    KiB read cap (avoids loading large handoff bodies; the frontmatter block
    is always near the top of the file).
    """
    try:
        with open(handoff_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(4096)
    except OSError:
        return None, None

    holder = None
    for field in ("claimed_by", "consumed_by"):
        val = read_fm_field_unquoted(content, field)
        if val:
            val = val.strip()
            if val and val.lower() not in ("null", "none"):
                holder = val
                break

    claimed_at = read_fm_field_unquoted(content, "claimed_at")
    if claimed_at is not None:
        claimed_at = claimed_at.strip()
        if not claimed_at or claimed_at.lower() in ("null", "none"):
            claimed_at = None

    return holder, claimed_at


def resolve_claim_state(
    handoff_path: Union[Path, str],
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[Union[Path, str]] = None,
) -> ClaimState:
    """Resolve a handoff's claim state ledger-first, frontmatter mirror as
    fallback. READ-ONLY — never writes the ledger or the frontmatter.

    Args:
        handoff_path: Path to the handoff file (used both to derive the
            claim-dir basename and to read the frontmatter mirror).
        common_dir: Pre-resolved git common dir
            (`<worktree>/.git` or the shared common dir for a linked
            worktree), matching `handoff_claim_dir`'s own parameter — pass
            this on a hot path to skip a second `git_common_dir` resolution.
            `git_common_dir` is `lru_cache`d, so even the fallback path below
            spawns no subprocess per call; this parameter exists purely to
            avoid a redundant *cached-dict-lookup* per call on the hottest
            paths (coverage.py's DAG fixpoint; commit_gates.py's per-handoff
            known-scope build), not to avoid a real cost.
        repo_root: Only consulted when `common_dir` is omitted, to resolve
            `git_common_dir`. Defaults to `handoff_path`'s parent directory.

    Returns:
        A `ClaimState`. `source == "none"` when neither side has a claim;
        `disagreement` is independently True whenever the ledger holds a live
        claim the mirror does not reflect, regardless of `source` (which is
        always "ledger" in that case — ledger wins).
    """
    handoff_path = Path(handoff_path)

    if common_dir is None:
        root = Path(repo_root) if repo_root is not None else handoff_path.parent
        try:
            resolved_common_dir = git_common_dir(root)
        except Exception:
            resolved_common_dir = None
    else:
        resolved_common_dir = Path(common_dir)

    ledger_holder: Optional[str] = None
    ledger_claimed_at: Optional[str] = None
    if resolved_common_dir is not None:
        claim_dir = handoff_claim_dir(resolved_common_dir, handoff_path)
        record = _read_ledger_claim(claim_dir)
        if record is not None:
            session_id, claimed_at = record
            try:
                holder_live = cs_claim_holder_live(str(claim_dir))
            except Exception:
                # Fail-closed-to-mirror — an unreadable/errored liveness read
                # is not evidence of a live ledger claim; degrade to "no
                # ledger claim" and let the mirror (if any) answer. Mirrors
                # handoff_reconcile.py::_ancestor_liveness_blocked's own
                # fail-closed-to-keep discipline for the same primitive.
                holder_live = False
            if holder_live:
                ledger_holder = session_id
                ledger_claimed_at = claimed_at

    mirror_holder, mirror_claimed_at = _read_mirror_claim(handoff_path)

    disagreement = bool(ledger_holder) and not bool(mirror_holder)

    if ledger_holder is not None:
        return ClaimState(
            holder=ledger_holder,
            claimed_at=ledger_claimed_at,
            source="ledger",
            disagreement=disagreement,
            ledger_holder=ledger_holder,
            mirror_holder=mirror_holder,
        )
    if mirror_holder is not None:
        return ClaimState(
            holder=mirror_holder,
            claimed_at=mirror_claimed_at,
            source="mirror",
            disagreement=disagreement,
            ledger_holder=ledger_holder,
            mirror_holder=mirror_holder,
        )
    return ClaimState(
        holder=None,
        claimed_at=None,
        source="none",
        disagreement=disagreement,
        ledger_holder=ledger_holder,
        mirror_holder=mirror_holder,
    )


def resolve_historical_claim(
    handoff_path: Union[Path, str],
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[Union[Path, str]] = None,
) -> Optional[tuple[str, Optional[str]]]:
    """The `(session_id, claimed_at)` of the claim event the durable ledger
    recorded for this handoff, WITHOUT the holder-liveness gate — or None when
    the ledger holds no usable record. READ-ONLY.

    A DIFFERENT QUESTION from `resolve_claim_state`, deliberately. That accessor
    answers "who holds this baton NOW", so a dead holder correctly degrades to
    "no ledger claim" (see this module's Negative-spec) — a live-work decision
    must never treat a crashed session's lock as an active claim. This accessor
    answers the retrospective "was this baton ever claimed, and by whom", where
    the holder's liveness is irrelevant: a session that claimed a baton, worked
    it, and exited is exactly the attribution a terminal record needs to name.

    Its one intended shape of caller is a writer stamping a TERMINAL state onto
    a record whose own frontmatter mirror carries no holder — the branch-
    dependence desync this module's docstring opens with — so that the mirror
    names the consumer instead of asserting an unattributable
    `status: claimed`. Spec: `docs/decisions/DR-084`; the mirror-side cross-field
    rule it feeds is `frontmatter/schemas/handoff.schema.json`'s
    `status: claimed` + `claimed_at` => `claimed_by` conditional.

    Negative-spec:
      - Does NOT write, and does NOT reconcile — a caller wanting the mirror
        re-stamped through the single claim writer uses
        `baton_assemble/apply.py::_reconcile_claim_from_ledger`, which routes
        through the `handoff.transition` verb="claim" op.
      - Is NOT a substitute for `resolve_claim_state` at any liveness,
        ownership, or pickup-eligibility decision. Treating a dead holder's
        ledger entry as a live claim there re-strands every crash-orphaned
        baton, which is precisely why the liveness gate exists.
      - Does NOT consult the frontmatter mirror. A caller holding the mirror
        already knows what it says; the value of this accessor is the OTHER
        half of the pair.
    """
    handoff_path = Path(handoff_path)

    if common_dir is None:
        root = Path(repo_root) if repo_root is not None else handoff_path.parent
        try:
            resolved_common_dir = git_common_dir(root)
        except Exception:
            return None
    else:
        resolved_common_dir = Path(common_dir)

    if resolved_common_dir is None:
        return None
    return _read_ledger_claim(handoff_claim_dir(resolved_common_dir, handoff_path))


# ---------------------------------------------------------------------------
# The comparator — Track B (AC6, AC7, AC9). Composed over `resolve_claim_state`,
# not a second read of either side. Read-only.
# ---------------------------------------------------------------------------

#: Reported when the ledger record carries no resolver-source stamp — every
#: row filed before the forward instrumentation (this plan's own C10/AC12)
#: lands, and every row filed by an engine that has not. The comparator does
#: not resolve identity itself and gains no new read here: absent field this
#: literal degrade, never a second resolution.
_LEDGER_RESOLVER_SOURCE_NOT_RECORDED = "not-recorded"

#: The AC12 forward-instrumentation field name inside a claim-lock dir,
#: sibling to `session_id`/`claimed_at`. Not written by anything yet (C10
#: lands after this row) — reading it here is forward-compatible, not a
#: dependency on C10.
_LEDGER_RESOLVER_SOURCE_FILENAME = "resolver_source"

#: Reported in place of a numeric age whenever no onset is measurable —
#: `mirror-only` (nothing records when the ledger side went away) and any
#: absent-or-unparseable `claimed_at`. Never raised, never a wrong-typed age.
AGE_NOT_MEASURABLE = "not measurable"

#: AC9's named bound, in seconds, with the bound value in the constant's own
#: name. A stated, not measured, choice: this comparator has no persistent
#: state across calls, so "the bound" is read as wall-clock elapsed time
#: since the ledger side's recorded onset, not a call-count ("until the next
#: apply") — the only bound shape a single stateless read can evaluate.
DISAGREEMENT_AGE_BOUND_SECONDS_300: float = 300.0


@dataclass(frozen=True)
class ClaimComparisonReport:
    """One handoff's ledger-vs-mirror comparison. Read-only, AC6's contract.

    Attributes:
        verdict: One of "agree" / "ledger-only" / "mirror-only" /
            "holder-mismatch" / "neither". `neither` (both sides absent)
            is a distinct, non-comparable state — no claim exists to
            compare — not a disagreement.
        ledger_holder: The raw ledger-side holder (gated on
            `cs_claim_holder_live`, the same read `resolve_claim_state`
            uses), or None.
        mirror_holder: The raw mirror-side holder, or None.
        age: The measured age (seconds, float) of a non-agreeing state
            where one is measurable — `ledger-only`/`holder-mismatch` age
            off the ledger side's `claimed_at` (the onset proxy this
            comparator states, not measures). `AGE_NOT_MEASURABLE` for
            `mirror-only` and any absent-or-unparseable timestamp. None
            for `agree`/`neither` (nothing to age).
        ledger_resolver_source: The resolver source observed at claim time
            (AC12's forward instrumentation), or
            `_LEDGER_RESOLVER_SOURCE_NOT_RECORDED` when the field is absent.
            This is what qualifies the verdict — an `agree` sourced from
            `resolve_session_id`-under-warm, or `not-recorded`, is agreement
            between two reads of a resolver that can manufacture a
            plausible wrong holder.
        bound_seconds: AC9's named bound (`DISAGREEMENT_AGE_BOUND_SECONDS_300`),
            echoed on every report regardless of verdict.
        bound_exceeded: True when `age` is numeric and exceeds
            `bound_seconds`. An unmeasurable age never counts as a breach.
            Changes no gate outcome (AC9, AC8) — a report field only.
    """

    verdict: str
    ledger_holder: Optional[str]
    mirror_holder: Optional[str]
    age: Union[float, str, None]
    ledger_resolver_source: str
    bound_seconds: float
    bound_exceeded: bool


def _read_ledger_resolver_source(claim_dir: Path) -> str:
    """Read the AC12 forward-instrumentation resolver-source field off a
    claim-lock dir, or `_LEDGER_RESOLVER_SOURCE_NOT_RECORDED` on any
    missing/unreadable/empty record. Never raises — mirrors
    `_read_ledger_claim`'s own degrade discipline."""
    try:
        value = (claim_dir / _LEDGER_RESOLVER_SOURCE_FILENAME).read_text(
            encoding="utf-8"
        ).strip()
    except (OSError, ValueError):
        return _LEDGER_RESOLVER_SOURCE_NOT_RECORDED
    return value or _LEDGER_RESOLVER_SOURCE_NOT_RECORDED


def _compute_age(claimed_at: Optional[str], *, now: Optional[float] = None) -> Union[float, str]:
    """Seconds elapsed since `claimed_at` (raw, as stored), or
    `AGE_NOT_MEASURABLE` on any absent-or-unparseable timestamp — never
    raises, never reports a wrong type as an age. `now` (unix timestamp) is
    an injectable clock for tests; defaults to the current UTC time."""
    if not claimed_at:
        return AGE_NOT_MEASURABLE
    text = claimed_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        onset = datetime.fromisoformat(text)
    except ValueError:
        return AGE_NOT_MEASURABLE
    if onset.tzinfo is None:
        onset = onset.replace(tzinfo=timezone.utc)
    current = (
        datetime.fromtimestamp(now, tz=timezone.utc)
        if now is not None
        else datetime.now(timezone.utc)
    )
    return (current - onset).total_seconds()


def compare_claim_state(
    handoff_path: Union[Path, str],
    *,
    common_dir: Optional[Path] = None,
    repo_root: Optional[Union[Path, str]] = None,
    now: Optional[float] = None,
) -> ClaimComparisonReport:
    """The comparator (AC6, AC7, AC9). Composed over `resolve_claim_state` —
    not a second read of either side. READ-ONLY, changes no gate outcome.

    Args:
        handoff_path, common_dir, repo_root: Same contract as
            `resolve_claim_state`.
        now: Injectable clock (unix timestamp) for `_compute_age`; tests
            only, defaults to the current UTC time.

    Returns:
        A `ClaimComparisonReport`. AC7: this function does not read, use, or
        alter `resolve_claim_state`'s `disagreement` field — that flag keeps
        its exact current (narrower) semantics, unmodified, as a distinct
        axis beside this comparator's verdict.
    """
    handoff_path = Path(handoff_path)
    state = resolve_claim_state(handoff_path, common_dir=common_dir, repo_root=repo_root)

    ledger_holder = state.ledger_holder
    mirror_holder = state.mirror_holder

    if ledger_holder is not None and mirror_holder is not None:
        verdict = "agree" if ledger_holder == mirror_holder else "holder-mismatch"
    elif ledger_holder is not None:
        verdict = "ledger-only"
    elif mirror_holder is not None:
        verdict = "mirror-only"
    else:
        verdict = "neither"

    if verdict in ("ledger-only", "holder-mismatch"):
        # Onset proxy is the ledger side's claimed_at in both cases — for
        # holder-mismatch onset is ambiguous between the two timestamps;
        # the ledger side is chosen for consistency with ledger-only, a
        # stated, not measured, choice (AC6). `state.claimed_at` is the
        # ledger side's value here: `resolve_claim_state` returns
        # `source="ledger"` whenever `ledger_holder` is not None.
        age: Union[float, str, None] = _compute_age(state.claimed_at, now=now)
    elif verdict == "mirror-only":
        # Nothing in the tree records when the ledger side went away (reap,
        # or a dead holder degrading through cs_claim_holder_live) — no
        # onset is measurable.
        age = AGE_NOT_MEASURABLE
    else:
        # agree / neither: nothing to age.
        age = None

    # Re-derive the claim dir the same way resolve_claim_state does, to read
    # the AC12 field without a second resolution of the claim itself — no
    # new read of who holds the claim, only the sibling resolver-source file
    # beside session_id/claimed_at.
    if common_dir is None:
        root = Path(repo_root) if repo_root is not None else handoff_path.parent
        try:
            resolved_common_dir: Optional[Path] = git_common_dir(root)
        except Exception:
            resolved_common_dir = None
    else:
        resolved_common_dir = Path(common_dir)

    ledger_resolver_source = _LEDGER_RESOLVER_SOURCE_NOT_RECORDED
    if resolved_common_dir is not None:
        claim_dir = handoff_claim_dir(resolved_common_dir, handoff_path)
        ledger_resolver_source = _read_ledger_resolver_source(claim_dir)

    bound_exceeded = isinstance(age, (int, float)) and age > DISAGREEMENT_AGE_BOUND_SECONDS_300

    return ClaimComparisonReport(
        verdict=verdict,
        ledger_holder=ledger_holder,
        mirror_holder=mirror_holder,
        age=age,
        ledger_resolver_source=ledger_resolver_source,
        bound_seconds=DISAGREEMENT_AGE_BOUND_SECONDS_300,
        bound_exceeded=bound_exceeded,
    )
