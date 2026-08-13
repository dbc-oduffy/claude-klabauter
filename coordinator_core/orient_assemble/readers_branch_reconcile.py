"""
coordinator_core.orient_assemble.readers_branch_reconcile — C2c reader port:
local-day/branch-span mismatch assertion + open-handoff auto-reconcile
observation.

Purpose: two independent read-only probes, both ported in-process rather
than shelled out to their source scripts, per
`docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md` § C2c:

1. Span-assert — the read-only comparison half of the fused
   `coordinator/bin/workday-start-day-branch-resolve.py` CLI. That script
   ALSO owns `cmd_reap_log` (a `subprocess.run` call to the co-located
   `reap-sessions.py`, plus a log-file append) — this reader deliberately
   does NOT import or call `cmd_reap_log`/`_run_reap_sessions`; only the
   pure `_span_assert` comparison and its `_current_branch` git read are
   ported into the in-process path.
2. `coordinator_core/ops/check_auto_reconcile.py` — imported AS-IS
   (already a clean, dependency-free module: `get_response()` dispatches
   `handoff.reconcile_open` in-process with no `dry_run` override, so the
   op's own `dry_run=True` default governs — observation only, never a
   transition).

A `clear`/`narrow` verdict computed under dry_run=true never reaches
`surfaced[]` (deliberate — see `handoff_reconcile.py`'s `_route_gate_clear`
docstring; routing it through `surfaced[]` would raise a spurious D1
conservation violation on the following run). Left unrendered, that steady
state is indistinguishable from "nothing to do," so `_read_auto_reconcile`
also renders `result.gates_cleared[]` entries where `dry_run` is truthy AND
`blocker_ids` is non-empty — the same discriminator
`coordinator/bin/check-auto-reconcile.py`'s `_render` uses for its own
would-flip line.

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md, chunk C2c
Spec backlink: cross-repo/inbox/2026-08-13-example-cockpit-repo-em-clear-verdict-invisible-under-dry-run-so-gates-never-announce.md

Negative-spec:
    - Does NOT import or invoke `cmd_reap_log` / `_run_reap_sessions` from
      the source script — that subprocess call to `reap-sessions.py` plus
      log-file append stays OUT of this in-process reader path (this
      chunk's explicit AC).
    - Does NOT pass a `dry_run` override to `check_auto_reconcile.get_response()`
      — the op's own conservative `dry_run=True` default is preserved
      unmodified.
    - Does NOT re-implement `handoff.reconcile_open`'s verdict logic — the
      `surfaced[]` list from its response is translated into
      `judgment_points[]` as-is, one entry per surfaced handoff.
    - Does NOT route a `gates_cleared[]` entry into `surfaced[]` or mutate
      `handoff_reconcile.py` in any way — this module is a read-only
      consumer of an already-computed response; it only renders an
      ADDITIONAL judgment point from a field the op already returns.
    - Does NOT render a `gates_cleared[]` entry whose `dry_run` is falsy or
      whose `blocker_ids` is empty — that shape is a genuine no-op
      (`_route_gate_clear`'s own guard: `if dry_run or not blocker_ids`)
      with nothing to announce.
    - Does NOT wire these results into `brief()` — `__init__.py`'s cadence
      dispatch is shared write-surface across C2a-C2d (the plan's own
      "same package — serial, write-overlap" note); this chunk lands
      alongside concurrently-dispatched sibling reader ports, so wiring
      `collect()` into `brief()` is left to a follow-up integration pass.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.orient_assemble.reader_result import (
    ReaderResult,
    cap_judgment_points,
    truncate_external_text,
)

#: The source CLI's absolute path — resolved relative to this file, never a
#: literal device path (portability discipline, AC-16). This file lives at
#: coordinator_core/orient_assemble/, so parents[2] is the claude-klabauter repo root
#: (parents[0]=orient_assemble, parents[1]=coordinator_core, parents[2]=repo
#: root — mirrors `readers_handoff_triage._SOURCE_PATH`'s same parents[2]).
_SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "bin"
    / "workday-start-day-branch-resolve.py"
)

_GIT_TIMEOUT = 10

#: Named cap on the auto-reconcile family's per-surfaced-handoff judgment-
#: point list — see `reader_result.cap_judgment_points`. Unbounded per-
#: handoff lists were ~40 of 148 JPs contributing to a 124KB
#: `brief('session')` payload before this cap existed.
_AUTO_RECONCILE_JUDGMENT_POINT_CAP = 5


def _load_source_module():
    """Load the hyphenated-filename source CLI as an importable module (same
    pattern as `readers_handoff_triage._load_source_module`) — a normal
    `import` statement cannot address a `-`-containing filename."""
    spec = importlib.util.spec_from_file_location(
        "workday_start_day_branch_resolve", _SOURCE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load source module at {_SOURCE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_day_branch_resolve = _load_source_module()
#: The pure comparison function — no side effects, unit-test seam in the
#: source module. Ported as-is, never reimplemented.
_span_assert_compute = _day_branch_resolve._span_assert


def _current_branch() -> str:
    """`git branch --show-current` — empty string on detached HEAD or
    failure. In-process read, not a shell-out to the fused CLI; mirrors the
    source module's own `_current_branch` (not reused directly since that
    helper is private to the loaded module and this call is trivial enough
    to keep local rather than reach back into the loaded module's private
    surface a second time)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _read_span_assert() -> ReaderResult:
    """Local-day/branch-span mismatch — a directive naming the ceremony's
    own remediation step (re-run `/workday-start` Step 0 or rename inline),
    never a judgment point (the source CLI's own contract: "a TRIPWIRE, not
    a retry — it never renames the branch itself"; the fix is deterministic,
    not an open human branch)."""
    from coordinator_core.daily_branch import format_span_suffix, parse_branch_span
    from coordinator_core.daily_day import local_day
    from coordinator_core.machine_resolver import compute_machine

    branch = _current_branch()
    today = local_day()
    msg = _span_assert_compute(
        branch, today, parse_branch_span, format_span_suffix, compute_machine
    )
    if msg is None:
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-branch-span-mismatch",
                "cli": "workday-start-day-branch-resolve",
                "args": ["span-assert"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": msg,
            }
        ]
    )


def _read_auto_reconcile() -> ReaderResult:
    """Open-handoff auto-reconcile observation — `check_auto_reconcile.get_response()`
    dispatches `handoff.reconcile_open` in-process under its own conservative
    `dry_run=True` default (never overridden here). Each `surfaced[]` entry
    names a handoff the reconcile op could not auto-resolve — an open human
    branch (review/reconcile manually), so becomes a `judgment_points[]`
    entry, never a silently-applied directive.

    Cap-order note (Review: code-reviewer — Finding 5): unlike the memo
    family, `surfaced[]` carries no meaningful priority key — it is
    `handoff.reconcile_open`'s own response order, not a date or severity
    ranking. This reader does NOT invent a sort key to fake one; when the
    cap binds, entries are kept in response order and the overflow judgment
    point's `list_command` is the complete, unordered view — cap order here
    carries no priority signal, so a withheld entry is not "less important,"
    only "later in an arbitrary order."

    Re-asked and re-answered 2026-08-13 (DR-300, its correction block):
    every `surfaced.append(...)` call site in `handoff_reconcile.py` (the
    `gate_eval`-verdict branches, the `narrow+surface` composite, the C9
    desync-read-error branch, and the terminal `commit_reality` fallthrough)
    writes only `handoff_id`, `reason`, `evidence`, and — on two branches
    only — `gate_evidence_resolved`/`contradiction`. No staleness, date, or
    confidence field exists on any entry to sort by; inventing one here
    would fabricate a ranking the producer never computed. DR-300 confirms
    this residual is real but small: the cap's own overflow judgment point
    already states the true total and the command to list every entry
    (`cap_judgment_points`'s "{N} total ... {cap} shown, {withheld}
    withheld" contract, live since `4f131b1bf`), so an arbitrary-order
    5-entry surface never reads as "these are the only 5 that exist." Do
    not re-open this question again without a genuine new field landing on
    `surfaced[]` upstream.

    Legibility (spec: docs/plans/2026-08-13-legible-reconcile-surface-and-
    single-baton-check.md, chunk C1; docs/decisions/DR-300-pickup-may-not-
    call-the-reconcile-orchestrator.md): the arbitrary order is a non-issue
    only because `cap_judgment_points`' overflow entry states the true
    surfaced total (`"{total} total ... {cap} shown, {withheld} withheld"`)
    whenever the cap binds — shape (a), an aggregate judgment point naming
    the true total, not shape (b) a ranking key. A reader always sees
    either every surfaced entry (count <= cap, nothing withheld) or the
    exact count withheld and the command to list them all; "5 shown" can
    never be mistaken for "5 exist."
    """
    from coordinator_core.ops.check_auto_reconcile import get_response

    response = get_response()
    if not response:
        return ReaderResult()
    result = response.get("result") or {}
    surfaced = result.get("surfaced") or []
    reconciled = result.get("reconciled") or []
    gates_cleared = result.get("gates_cleared") or []
    if not isinstance(gates_cleared, list):
        gates_cleared = []
    dry_run_clears = [
        entry
        for entry in gates_cleared
        if isinstance(entry, dict) and entry.get("dry_run") and entry.get("blocker_ids")
    ]
    failed_reconciles = [
        entry for entry in reconciled if entry.get("exit_code", 0) != 0
    ]
    if not surfaced and not failed_reconciles and not dry_run_clears:
        return ReaderResult()

    judgment_points: list[dict[str, Any]] = []
    for idx, entry in enumerate(surfaced):
        handoff_id = entry.get("handoff_id") or "?"
        reason = truncate_external_text(
            entry.get("reason") or "surfaced by handoff.reconcile_open"
        )
        evidence_text = truncate_external_text(entry.get("evidence") or reason)
        judgment_points.append(
            build_judgment_point(
                None,
                id=f"j-auto-reconcile-{idx + 1}",
                question=(
                    f"Handoff {handoff_id!r} surfaced by auto-reconcile "
                    f"({reason}) — review manually?"
                ),
                dispositions=[
                    build_disposition("pm_reviews_manually"),
                    build_disposition("leave_for_now"),
                ],
                evidence=(
                    f"{evidence_text} | reason: "
                    "handoff.reconcile_open could not auto-ship or "
                    "gate-cascade-clear this handoff — never silently resolved"
                ),
                reason="recommendation-forbidden",
            )
        )
    judgment_points = cap_judgment_points(
        judgment_points,
        cap=_AUTO_RECONCILE_JUDGMENT_POINT_CAP,
        overflow_id="j-overflow-auto-reconcile",
        item_label="surfaced auto-reconcile handoffs",
        list_command="check-auto-reconcile",
    )

    gate_clear_points: list[dict[str, Any]] = []
    for idx, entry in enumerate(dry_run_clears):
        handoff_id = entry.get("handoff_id") or "?"
        verdict = entry.get("verdict") or "clear"
        target = "ready_to_fire" if verdict == "clear" else "awaiting_gate (narrowed)"
        blocker_ids = entry.get("blocker_ids") or []
        blockers = ", ".join(str(b) for b in blocker_ids)
        gate_clear_points.append(
            build_judgment_point(
                None,
                id=f"j-gate-cleared-{idx + 1}",
                question=(
                    f"Handoff {handoff_id!r} gate cleared (verdict={verdict}) — "
                    f"would flip awaiting_gate → {target} (dry-run, not applied) — "
                    "arm the reconciler?"
                ),
                dispositions=[
                    build_disposition("pm_reviews_manually"),
                    build_disposition("leave_for_now"),
                ],
                evidence=(
                    f"blockers cleared: {blockers} | dry_run=true, no transition "
                    "applied — handoff.reconcile_open computed this verdict but "
                    "arming (dry_run=false) is a separate, named posture change"
                ),
                reason="recommendation-forbidden",
            )
        )
    gate_clear_points = cap_judgment_points(
        gate_clear_points,
        cap=_AUTO_RECONCILE_JUDGMENT_POINT_CAP,
        overflow_id="j-overflow-gate-cleared",
        item_label="dry-run gate-cleared handoffs",
        list_command="check-auto-reconcile",
    )

    desync_points: list[dict[str, Any]] = []
    for idx, entry in enumerate(failed_reconciles):
        message = truncate_external_text(entry.get("message") or "no message")
        desync_points.append(
            build_judgment_point(
                None,
                id=f"j-desync-repair-failed-{idx + 1}",
                question="Ledger/frontmatter desync repair failed — review manually?",
                dispositions=[
                    build_disposition("pm_reviews_manually"),
                    build_disposition("leave_for_now"),
                ],
                evidence=f"exit_code={entry.get('exit_code')} | {message}",
                reason="recommendation-forbidden",
            )
        )
    desync_points = cap_judgment_points(
        desync_points,
        cap=_AUTO_RECONCILE_JUDGMENT_POINT_CAP,
        overflow_id="j-overflow-desync-repair-failed",
        item_label="failed desync repairs",
        list_command="check-auto-reconcile",
    )
    return ReaderResult(judgment_points=judgment_points + gate_clear_points + desync_points)


def collect(cadence: str) -> ReaderResult:
    """Compute this reader family's directives/judgment_points.

    `cadence` is accepted for signature parity with sibling reader families
    (`readers_clean_ops.collect`) but unused here — neither probe's
    severity varies by cadence.
    """
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in (
        _read_span_assert(),
        _read_auto_reconcile(),
    ):
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
