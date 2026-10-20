"""
coordinator_core.workday_complete.brief — the `workday-complete` computed-skill
engine's READ-ONLY compute half.

Purpose: computes `coordinator/commands/workday-complete.md`'s mechanical
step inventory (Step 1/1.5/2.6/2.65/3/3.5-PhaseA0/4a/6/7.5/8/9/10/10.5/10.6) into the
canonical 8-key decision-object envelope, and surfaces every judgment-shaped
step (Step 2.5 exit-2 ambiguous-dirty-tree ask, Step 3.5 Phase A/B backfill-cap
PM ask, Step 4b/4.5 Sonnet-dispatch decisions, Step 4e health-ledger new-row
confirmation) as an overridable `judgment_points[]` offer rather than deciding
it for the caller.

This module imports its envelope/judgment-point constructors from the shipped
canonical-resolution-engine library (`coordinator_core.resolution.facade`,
`coordinator_core.contract.decision_object.{envelope,judgment}`) rather than
reimplementing them — see the negative-spec below.

Contract (frozen, reviewed): coordinator-content-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: coordinator-content-repo:pln-b1-ceremony-complete-computed--9ffa54, chunk C2

Consumes-manifest (C1 census, plan § Tasks C1 body) — orchestrates, reimplements
none of the following existing atomic CLIs (every `directives[].cli` value below
is a literal name drawn from exactly this set):
    workday-complete-args-and-validate, workday-complete-reconcile,
    workday-complete-step2_5-dirty-tree, workday-complete-step3-consolidate,
    workday-complete-backfill-scan, workday-complete-backfill-anchor,
    workday-complete-close, standup,
    query-completions, coordinator-queue-append, prune-closed-bugs,
    workday-start-advisory-counters, check-weekly-staleness, goal-close-day,
    coordinator-ceremony-hook, emit-cadence

Ceremony-close tail (Step 10.5/10.6, post-command hook + emit-cadence): built
via the shared `coordinator_core.ceremony_common.tail.build_ceremony_close_tail`
(C5, AC9) — the byte-identity check against `workweek_complete.brief`'s Step
13.5/13.6 pair confirmed the two tails are identical in every load-bearing
field (both name `coordinator-ceremony-hook`/`emit-cadence`, empty args,
`depends_on=None`), so C5 factored the shared builder rather than each
assembler hand-maintaining its own copy of the same two directives.

Day-goal close-out (C4, docs/plans/2026-07-25-day-goal-close-out-lifecycle.md
§ C4): `jp_day_goal_closeout` + directive `d_goal_close_day` wire C2/C3's
`coordinator_core.ops.goal_close_day` read/write legs into this same
mechanical spine — NOT a bespoke prose ceremony step. Both the judgment
point and its gated directive are emitted ONLY when
`coordinator_core.ops.goal_close_day.collect_open_day_goals` reports at
least one open row (today or stale) for the invoking repo — unlike the
five standing judgment points above (always emitted regardless of
whether their condition is live), this one is conditional so a repo with
nothing to close never carries a permanently-blocked directive. The
per-goal done/dropped outcomes ride a SEPARATE free-form
`decisions["day_goal_closeout"]` map (`{goal_id: "done"|"dropped"}`),
threaded into `_build_directives`'s now non-empty parameter list — follows
the same `decisions`-as-caller-input pattern `workstream_complete
.build_directives(gate, decisions)` established, though the parameter
shapes differ (that precedent's first param is a required gate object this
module has no equivalent of) — never encoded as the judgment point's own
disposition value.

Negative-spec:
    - Does NOT reimplement `build_envelope`/`emit`/`build_judgment_point`/
      `build_disposition`/`resolve_operator_config` — imported from the shipped
      library, per the Staff Engineer F3 `build_untrusted_gate_judgment_point` is
      deliberately NOT imported here: every judgment point this module builds
      is either trusted-caller-authored evidence (Step 2.5's ambiguous-file ask
      is sourced from `git status --porcelain`, not attacker-influenceable
      content) or a genuine Sonnet-dispatch recommendation — neither shape is
      an untrusted gate.
    - `_compute_open_day_goals` never raises and never fails the ceremony —
      an unresolvable repo root or any read error degrades to an empty
      today/stale partition (mirrors `goal.close_day`'s own read-side degrade
      posture; every claude-klabauter-op consumer in DoE's commands carries this
      "offers, not nags" mandatory degradation clause).
    - Does NOT add a mutating code path — every directive names an existing
      CLI for the apply half (`coordinator_core.workday_complete.apply`) to
      invoke; this module only reads disk/git state via the named CLIs' own
      dry/inspect verbs where one exists, or composes evidence strings for the
      judgment points from already-shipped read-only primitives.
    - Does NOT represent Step 2 (RAG staleness nudge) or Step 5 (plugin
      validation suite) as `directives[]` entries — neither has a corresponding
      entry in the C1 consumes-manifest (Step 2 is a `ToolSearch`-gated
      skill-native check; Step 5 is a direct `node --test` invocation), so
      representing either as a `directives[].cli` value would violate the
      "every directive names an EXISTING CLI, never a phantom verb" rule
      (AC15c). Both are noted in `narration` instead — a deliberate scope
      line, not an oversight (see this chunk's executor report).
    - `coordinator-queue-append` IS a genuine C1 consumes-manifest member but
      is never a `directives[].cli` value: the strategic observer that invokes
      it dispatches from `/workweek-complete` (Step 7b), not from this brief.
      A dispatched worker's own CLI use is outside what a `directives[]` entry
      can express.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from coordinator_core.cadence.period_mode import (
    CATCH_UP_FLAG,
    CONCLUDE_FLAG,
    CONCLUDING_DIRECTIVES,
    NARRATION_LEAD_PREFIX,
    PERIOD_MODE_KEY,
    PeriodMode,
    local_now,
    period_flag_error,
    resolve_period_mode,
)
from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    emit,
    extend_exit_codes,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    partition_reportable,
)
from coordinator_core.completion_receipts.day import receipts_for_day
from coordinator_core.git.git_state import head_branch
from coordinator_core.git.repo_root import git_common_dir
from coordinator_core.ops.emit.resolvers import resolve_context
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.goal_close_day import collect_open_day_goals
from coordinator_core.ops.requirement_register import StallReport, stall_judgment_point, stall_report
from coordinator_core.ops.workday_complete_backfill_scan import (
    DEFAULT_LOOKBACK as LOOKBACK_DAYS,
    GapScan,
    gap_summary_lines,
    scan as backfill_scan,
)
from coordinator_core.ops.workday_complete_step2_5_dirty_tree import (
    classify_dirty_tree,
)
from coordinator_core.resolution.facade import resolve_operator_config
from coordinator_core.workday_complete.cockpit_contract_freshness import (
    compute_cockpit_contract_freshness,
)

WorkdayExitCode = extend_exit_codes(
    "WorkdayExitCode",
    BUSINESS_FAIL=1,
    USAGE=2,
    TRANSPORT_FAIL=3,
)

CONSUMES_MANIFEST: tuple[str, ...] = (
    "workday-complete-args-and-validate",
    "workday-complete-reconcile",
    "workday-complete-step2_5-dirty-tree",
    "handoff-housekeeping",
    "workday-complete-step3-consolidate",
    "workday-complete-backfill-scan",
    "workday-complete-backfill-anchor",
    "workday-complete-close",
    "standup",
    "query-completions",
    "coordinator-queue-append",
    "prune-closed-bugs",
    "workday-start-advisory-counters",
    "check-weekly-staleness",
    "goal-close-day",
    "coordinator-ceremony-hook",
)


def _resolve_repo_common_dir_for_ceremony(start: Optional[Path] = None) -> Optional[Path]:
    cwd = start or Path.cwd()
    out = git_common_dir(str(cwd))
    return Path(out) if out else None


def _compute_open_day_goals() -> dict[str, Any]:
    common_dir = _resolve_repo_common_dir_for_ceremony()
    if common_dir is None:
        return {
            "today": [],
            "stale": [],
            "unreadable_error": (
                "could not resolve the invoking repo's git common dir "
                "(git rev-parse --path-format=absolute --git-common-dir)"
            ),
        }
    try:
        repo_root = main_worktree_root(common_dir)
        ctx = resolve_context(repo_root)
        return collect_open_day_goals(ctx.central_state_root, ctx.repo_name, coordinator_root_path=".")
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        print(
            f"workday_complete.brief: day-goal open-row read failed, degrading to zero rows: {exc}",
            file=sys.stderr,
        )
        return {"today": [], "stale": [], "unreadable_error": str(exc)}


def _compute_register_stall() -> StallReport:
    """Register stall scan from the main worktree; an unreadable tree reports nothing."""
    try:
        return stall_report(Path(_main_worktree_root_for_directive()), date.today())
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return StallReport()


_CONCLUDING_IDS = frozenset(CONCLUDING_DIRECTIVES["workday-complete"])

#: Gap rows above which the backfill fan-out needs a PM glance.
BACKFILL_CAP_ROWS = 10


def _compute_backfill_scan() -> tuple[GapScan, str]:
    """Read-only pre-A0 scan of the main worktree: `(scan, "")`, or `(empty scan, narration line)`
    when the scan raises, so the brief degrades to no cap point and never fails the ceremony."""
    try:
        return backfill_scan(_main_worktree_root_for_directive(), LOOKBACK_DAYS), ""
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        return GapScan(), (
            f"Backfill scan unavailable ({exc!r}); jp_step3_5_backfill_cap not emitted, "
            "run workday-complete-backfill-scan by hand."
        )


def _backfill_cap_live(scan: Optional[GapScan]) -> bool:
    """True when the cap point is live. `None` (scan not probed) keeps the point."""
    if scan is None:
        return True
    return len(scan.rows) > BACKFILL_CAP_ROWS or bool(scan.older_gaps)


def _older_gaps_line(older_gaps: list[str]) -> str:
    days = sorted(older_gaps)
    return f"older gaps: {len(days)} day(s), {days[0]}..{days[-1]} (before the {LOOKBACK_DAYS}-day window)"


def _backfill_cap_judgment_point(scan: Optional[GapScan]) -> dict[str, Any]:
    """`jp_step3_5_backfill_cap`; `scan` is the pre-A0 read, `None` when not probed."""
    if scan is None:
        question = (
            "Backfill scan may find more than "
            f"{BACKFILL_CAP_ROWS} pre-A0 gap rows. Backfill all, or a bounded subset?"
        )
        evidence = f"workday-complete-backfill-scan --lookback {LOOKBACK_DAYS} (pre-A0 gap rows)"
    else:
        question = (
            f"Backfill scan found {len(scan.rows)} pre-A0 gap row(s) in the last {LOOKBACK_DAYS} days"
            + (f" and {len(scan.older_gaps)} older uncovered day(s)" if scan.older_gaps else "")
            + ". Backfill all, or a bounded subset?"
        )
        lines = gap_summary_lines(scan.rows)
        if scan.older_gaps:
            lines.append(_older_gaps_line(scan.older_gaps))
        evidence = (
            f"workday-complete-backfill-scan --lookback {LOOKBACK_DAYS} (pre-A0):\n" + "\n".join(lines)
        )
    return build_judgment_point(
        {
            "disposition": "bounded_subset",
            "rationale": (
                "A 10+-day gap is a signal worth a human glance, not a "
                "silent 10-agent burst; default to the PM-named bounded "
                "subset over an unbounded auto-fan."
            ),
        },
        id="jp_step3_5_backfill_cap",
        question=question,
        dispositions=[
            build_disposition("backfill_all", resolves=["d_step3_5_backfill_phase_b"]),
            build_disposition("bounded_subset", resolves=["d_step3_5_backfill_phase_b"]),
        ],
        evidence=evidence,
        reason="pm-scoped-tradeoff",
        revalidate_at_dispatch=True,
        round_trip="terminal",
    )


def _compute_completion_receipts_gate(for_date: Optional[str]) -> dict[str, Any]:
    try:
        common_dir = _resolve_repo_common_dir_for_ceremony()
        if common_dir is None:
            return {"error": "could not resolve the invoking repo's git common dir"}
        return receipts_for_day(
            main_worktree_root(common_dir), for_date or date.today().isoformat()
        )
    except Exception as exc:  # noqa: BLE001 - never fail the brief
        return {"error": str(exc)}


def _open_day_goals_present(open_day_goals: dict[str, Any]) -> bool:
    return bool(open_day_goals.get("today")) or bool(open_day_goals.get("stale"))


def _main_worktree_root_for_directive() -> str:
    common_dir = _resolve_repo_common_dir_for_ceremony()
    if common_dir is None:
        return "."
    try:
        return str(main_worktree_root(common_dir))
    except Exception:  # noqa: BLE001 - never fail the ceremony
        return "."


# AC10 fix (2026-07-25 conformance-test sweep): `workday-complete-step2_5-
# dirty-tree` was a CONSUMES_MANIFEST entry with no directive anywhere ever
# naming it, AND `jp_step2_5_dirty_tree_ambiguous` was unconditionally
# emitted every run (gating `d_step3_consolidate` behind an EM ask even on
# a clean tree) — the manifest/emission contract test this AC required
# caught both. Fixed by giving Step 2.5 the same "compute the real
# condition, emit only when it's live" shape as C4's day-goal judgment
# point: a directive that always runs the script's own auto-disposition
# (clear-wins committed/gitignored unconditionally, same as the pre-
# conversion skill body), plus a read-only DRY-RUN probe here that decides
# whether ambiguous/source-tree paths remain and therefore whether the ask
# is even live.
def _compute_dirty_tree_verdict() -> dict[str, Any]:
    """Read-only probe of Step 2.5's typed, mutation-free classification
    (`coordinator_core.ops.workday_complete_step2_5_dirty_tree.
    classify_dirty_tree`) — determines whether SOURCE-TREE/AMBIGUOUS paths
    remain (`needs_pm`) WITHOUT performing any git mutation and WITHOUT
    reading `main()`'s print/exit-code contract. Never raises and never
    fails the ceremony (mirrors `_compute_open_day_goals`'s degradation
    posture) — but degrades toward `ambiguous=True` on any probe failure
    (a raise, or the classification's own `error` set), i.e. toward asking
    the EM, never toward silently skipping a genuine ask (fail-toward-
    asking posture, a deliberate HEAD behaviour change: HEAD's exit-1 hard
    error read as not-ambiguous)."""
    try:
        classification = classify_dirty_tree()
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        return {
            "ambiguous": True,
            "evidence": (
                "workday-complete-step2_5-dirty-tree classify_dirty_tree() probe raised "
                f"{exc!r}, degrading to ambiguous=True (fail toward asking)"
            ),
        }
    if classification.error is not None:
        return {"ambiguous": True, "evidence": classification.error}
    evidence = "\n".join(classification.evidence_lines()).strip()
    return {
        "ambiguous": classification.needs_pm,
        "evidence": evidence or "workday-complete-step2_5-dirty-tree: no output",
    }


def _directive(
    id: str,
    *,
    cli: str,
    args: list[str],
    depends_on: Optional[str] = None,
    already_satisfied: bool = False,
    stdin_from: Optional[str] = None,
) -> dict[str, Any]:
    """One `directives[]` entry. `cli` MUST be a member of `CONSUMES_MANIFEST`
    (enforced by `_build_directives`'s own assertion, not here — this helper
    stays a pure constructor so a unit test can build a deliberately-invalid
    entry to exercise that assertion).

    `stdin_from` (2026-07-26 backfill-leg stdin-wiring fix): the `id` of
    ANOTHER directive in this same `directives[]` list whose captured stdout
    `apply._execute_directives` must feed to this directive's stdin. `None`
    (the default) means this directive consumes no stdin at all — `apply.py`
    never touches `sys.stdin` for it, so it can never block on one. General
    to any multi-stage directive pipeline in this module, not special-cased
    to the two backfill directives that motivated it; a directive named as a
    `stdin_from` producer need not itself declare a `stdin_from` (a fan-in of
    depth >1 chains naturally, since `apply.py` records every landed
    directive's captured stdout by id, not just producers explicitly named
    today)."""
    return {
        "id": id,
        "cli": cli,
        "args": list(args),
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
        "stdin_from": stdin_from,
    }


def _build_directives(
    decisions: dict[str, Any],
    open_day_goals: dict[str, Any],
    dirty_tree_verdict: dict[str, Any],
    for_date: Optional[str] = None,
    only_mode: bool = False,
    scope_summary: Optional[str] = None,
    backfill_cap_live: bool = True,
    catch_up: bool = False,
) -> list[dict[str, Any]]:
    """Tier-1 (mechanical, no open question) directive entries — one per
    collapsed workday-complete.md step: 1/1.5/2.5/2.6/2.65/3/3.5-PhaseA0/4a/6/
    7.5/8/9/10, plus the conditional C4 day-goal close-out directive. Every
    `cli` value is a literal member of `CONSUMES_MANIFEST`.

    `d_step2_5_dirty_tree_scan` (AC10 fix) fires UNCONDITIONALLY — it runs
    the script's own auto-disposition for clear-wins (EOL-phantom/submodule/
    leave-alone/orphan-tmp/auto-gitignore/auto-commit), same as the
    pre-conversion skill body did every run, never gated on any judgment
    point itself.

    `depends_on` wires the directives whose real-world ordering is gated
    on an open judgment point per the halt contract (§ module docstring's
    `apply.py` counterpart): branch consolidation (Step 3) waits on the
    Step 2.5 ambiguous-dirty-tree ask ONLY when `dirty_tree_verdict`
    (computed once in `brief()`, threaded to both this function and
    `_build_judgment_points`) found the tree genuinely ambiguous — a clean
    tree leaves `d_step3_consolidate` ungated (`depends_on=None`), since
    there is no live ask for it to wait on. The backfill Phase-B dispatch
    (part of Step 3.5) waits on the backfill-cap PM ask only while
    `backfill_cap_live` (`_backfill_cap_live`: the point is emitted), and `d_goal_close_day` waits on
    `jp_day_goal_closeout`. Every other directive is `depends_on: None` —
    it fires unconditionally, per the halt contract's "a mechanical
    directive not gated on any judgment point always fires" rule.

    `decisions` (follows the same `decisions`-as-caller-input pattern
    `workstream_complete.build_directives(gate, decisions)` established,
    though the parameter shapes differ) supplies the one caller-provided
    input this module cannot read off disk: `decisions["day_goal_closeout"]`,
    a free-form
    `{goal_id: "done"|"dropped"}` map threaded verbatim into
    `d_goal_close_day`'s `--decisions` argv as a JSON blob — never encoded
    as `jp_day_goal_closeout`'s own disposition value (C4, DEC-1/DEC-2/DEC-3).

    `open_day_goals` (C2's `collect_open_day_goals` read, computed once in
    `brief()` and threaded to both this function and
    `_build_judgment_points`) gates whether `d_goal_close_day` is appended
    at all: unlike every directive above, this one is emitted ONLY when at
    least one open day row exists — a repo with nothing to close never
    carries a permanently-blocked directive naming an absent judgment
    point (see `_open_day_goals_present`).

    `for_date`/`only_mode` (threaded verbatim from `brief()`'s own
    same-named kwargs — see that function's docstring) date-scope
    `d_step3_5_backfill_phase_b` ONLY: with `for_date` set, its args gain
    `--for-date <for_date>` (matching `workday-complete-close.py`'s
    `backfill-dispatch-rows` subcommand's real flag spelling); `--only-mode`
    is appended ONLY when BOTH `for_date` is set AND `only_mode` is true —
    `only_mode` alone (no `for_date`) is intentionally a no-op rather than
    threading a bare `--only-mode` with no `--for-date`, which would make
    `cmd_backfill_dispatch_rows`'s `date != args.for_date` guard compare
    every row against `None` and silently skip the entire backfill. Every
    other directive in this list is untouched by `for_date` — but
    `only_mode` ALSO threads `--only-mode` into `d_step9_changelog`, which
    is a separate concern from date-scoping and easy to miss:
    `cmd_step9_dispatch` skips itself entirely under `--only-mode` ("the
    targeted block was already committed via Step 3.5 Phase B"), and
    without the flag it never learns to. Omitting it made a targeted wrap
    write a today-scoped changelog block alongside the backfilled one — a
    real 2026-07-28 incident, where a `--for-date 2026-07-27 --only` run
    produced both a 2026-07-27 block and a spurious 2026-07-28 one. Once
    `scope_summary` is also threaded, the same omission additionally
    misattributes the user's `$FOR_DATE` prose onto today's block.

    `scope_summary` (the user's free-form day-summary prose, threaded
    verbatim from `brief()`'s own same-named kwarg) reaches TWO directives,
    asymmetrically, because the two consuming CLIs accept it differently:

    - `d_step9_changelog` (`workday-complete-close step9-dispatch`) takes it
      as a BARE POSITIONAL (`p_step9.add_argument("scope_summary",
      nargs="?", ...)`) — appended UNCONDITIONALLY whenever `scope_summary`
      is truthy, independent of `for_date`/`only_mode`, since this is the
      default (non-backfill) route's own changelog dispatch.
    - `d_step3_5_backfill_phase_b` (`backfill-dispatch-rows`) takes it as
      `--scope-summary` (`p_backfill.add_argument("--scope-summary", ...)`)
      — appended ONLY when BOTH `for_date` AND `scope_summary` are set,
      mirroring the existing `only_mode`-gating rationale immediately
      above: `cmd_backfill_dispatch_rows` only ever applies
      `args.scope_summary` to the one row matching `args.for_date`
      (`scope_arg = args.scope_summary if (date == args.for_date and
      args.scope_summary) else None`), so a `scope_summary` with no
      `for_date` would just be silently inert on this leg — omitting the
      flag entirely in that case keeps the directive's `args` free of dead
      weight rather than passing a flag that can never take effect.

    LEADING-DASH HAZARD: a `scope_summary` beginning with `-` (e.g. a user
    typing `"-- wrapped the refactor"`) would be misparsed by argparse if
    appended as a naked positional token or a two-token `--flag value`
    pair — argparse treats a token starting with `-` as a new option
    unless told otherwise. Two different, deliberate fixes are used here:
    the positional form inserts an explicit `--` end-of-options separator
    immediately before the value (`["step9-dispatch", "--", scope_summary]`
    — argparse's own convention for "everything after this is positional,
    verbatim"); the flag form uses the single-token `--scope-summary=VALUE`
    spelling (never the two-token `--scope-summary`, `VALUE` split) so
    argparse never has to classify `VALUE` as a candidate option at all.
    Neither approach mangles or strips the user's prose.

    `catch_up` drops every `CONCLUDING_DIRECTIVES["workday-complete"]` id from the list: the
    in-progress day stays open, so nothing concluding is emitted, and none is self-skipping.
    """
    directives = [
        _directive(
            "d_step1_validate",
            cli="workday-complete-args-and-validate",
            args=["run-step1"],
        ),
        _directive(
            "d_step1_5_cruft_sweep",
            cli="workday-complete-reconcile",
            args=["cruft-sweep"],
        ),
        _directive(
            "d_step2_5_dirty_tree_scan",
            cli="workday-complete-step2_5-dirty-tree",
            args=[],
        ),
        _directive(
            "d_step2_67_handoff_housekeeping",
            cli="handoff-housekeeping",
            args=[],
        ),
        _directive(
            "d_step3_consolidate",
            cli="workday-complete-step3-consolidate",
            args=[],
            depends_on=(
                "jp_step2_5_dirty_tree_ambiguous"
                if dirty_tree_verdict.get("ambiguous")
                else None
            ),
        ),
        _directive(
            "d_step3_5_backfill_scan",
            cli="workday-complete-backfill-scan",
            args=[
                "--lookback",
                str(LOOKBACK_DAYS),
                "--repo-root",
                _main_worktree_root_for_directive(),
            ],
        ),
        _directive(
            "d_step3_5_backfill_anchor_a0",
            cli="workday-complete-backfill-anchor",
            # `run` declares a REQUIRED `root` positional (bin's
            # `_cmd_run` parser) — omitting it always exited 2
            # ("the following arguments are required: root"), so Phase-A0
            # backfill anchoring never ran. `_main_worktree_root_for_directive`
            # resolves the MAIN worktree root (never `Path.cwd()`/
            # `--show-toplevel`) so this directive's write leg matches every
            # other ceremony read/write pairing in this module.
            # `--allow-empty`: `stdin_from` below already proves the scan
            # landed this pass, so empty gap rows here genuinely mean a gapless
            # window. Without the flag the CLI now refuses empty stdin rather
            # than exiting 0 having anchored nothing — the silent-success shape
            # this whole leg was reported for.
            args=["run", _main_worktree_root_for_directive(), "--allow-empty"],
            stdin_from="d_step3_5_backfill_scan",
        ),
        _directive(
            "d_step3_5_backfill_phase_b",
            cli="workday-complete-close",
            args=(
                ["backfill-dispatch-rows", "--allow-empty", "--for-date", for_date]
                if for_date
                else ["backfill-dispatch-rows", "--allow-empty"]
            )
            + (["--only-mode"] if for_date and only_mode else [])
            + ([f"--scope-summary={scope_summary}"] if for_date and scope_summary else []),
            depends_on="jp_step3_5_backfill_cap" if backfill_cap_live else None,
            stdin_from="d_step3_5_backfill_scan",
        ),
        _directive(
            "d_step4a_inventory",
            cli="standup",
            args=[],
        ),
        _directive(
            "d_step6_archive_audit_completions",
            cli="query-completions",
            args=["--format", "json"],
        ),
        _directive(
            "d_step7_5_prune_closed_bugs",
            cli="prune-closed-bugs",
            args=["--repo-root", _main_worktree_root_for_directive()],
        ),
        _directive(
            "d_step8_improvement_queue_nudge",
            cli="workday-start-advisory-counters",
            args=[
                "improvement-queue",
                "--repo-root",
                _main_worktree_root_for_directive(),
            ],
        ),
        _directive(
            "d_step9_changelog",
            cli="workday-complete-close",
            args=["step9-dispatch"]
            + (["--only-mode"] if only_mode else [])
            + (["--", scope_summary] if scope_summary else []),
        ),
        _directive(
            "d_step10_weekly_staleness",
            cli="check-weekly-staleness",
            args=[],
        ),
    ]
    if _open_day_goals_present(open_day_goals):
        directives.append(
            _directive(
                "d_goal_close_day",
                cli="goal-close-day",
                args=[
                    "--decisions",
                    json.dumps(
                        (decisions or {}).get("day_goal_closeout", {}), sort_keys=True
                    ),
                ],
                depends_on="jp_day_goal_closeout",
            )
        )
    directives += [
        *build_ceremony_close_tail(
            post_command_hook_id="d_step10_5_post_command_hook",
            ceremony_name="workday-complete",
        ),
    ]
    if catch_up:
        directives = [d for d in directives if d["id"] not in _CONCLUDING_IDS]
    for entry in directives:
        assert entry["cli"] in CONSUMES_MANIFEST, (
            f"_build_directives: directive {entry['id']!r} names {entry['cli']!r}, "
            "not a member of CONSUMES_MANIFEST (AC15c: no phantom verbs)"
        )
    return directives


_GOAL_PREVIEW_LIMIT = 10
_GOAL_TEXT_CUT = 80


def _goal_preview(rows: list[dict[str, Any]]) -> str:
    """First `_GOAL_PREVIEW_LIMIT` rows as `<goal_id>: <text cut>`, then a
    `+K more` tail. The JP carries a preview; `goal.close_day` returns all."""
    if not rows:
        return "none"
    lines = [
        f"{row.get('goal_id', '?')}: {str(row.get('text', '?'))[:_GOAL_TEXT_CUT]}"
        for row in rows[:_GOAL_PREVIEW_LIMIT]
    ]
    if len(rows) > _GOAL_PREVIEW_LIMIT:
        lines.append(f"+{len(rows) - _GOAL_PREVIEW_LIMIT} more")
    return "; ".join(lines)


def _build_day_goal_closeout_judgment_point(open_day_goals: dict[str, Any]) -> dict[str, Any]:
    """C4's conditional judgment point — see `_build_directives`'s docstring
    for why this one (unlike every point below) is only ever constructed
    when `_open_day_goals_present(open_day_goals)` is true. Presents today
    and stale rows DISTINCTLY in both `question` and `evidence` (C2
    partitions them; C4 must not flatten the distinction back out).

    # Row access uses `.get(...,
    # "?")` rather than bracket access so a malformed row (missing
    # goal_id/text) degrades to a placeholder instead of raising a KeyError
    # out of `brief()`'s top-level backstop.
    """
    today_rows = open_day_goals.get("today") or []
    stale_rows = open_day_goals.get("stale") or []
    today_desc = _goal_preview(today_rows)
    stale_desc = _goal_preview(stale_rows)
    return build_judgment_point(
        None,
        id="jp_day_goal_closeout",
        question=(
            f"{len(today_rows)} day goal(s) due today and {len(stale_rows)} "
            "stale day goal(s) from prior days are open on the goals wire. "
            "Record a done/dropped outcome for each in the top-level key "
            "decisions['day_goal_closeout'] = {goal_id: 'done'|'dropped'} "
            "(any value other than exactly 'done' closes 'dropped' — DEC-1), "
            "or skip for now?"
        ),
        dispositions=[
            build_disposition("record", resolves=["d_goal_close_day"]),
            build_disposition("skip", resolves=[]),
        ],
        evidence=(
            f"today={len(today_rows)} stale={len(stale_rows)}; "
            f"today=[{today_desc}] stale=[{stale_desc}]; "
            "source: goal.close_day (open day goals, today and stale)"
        ),
        reason="insufficient-evidence",
        revalidate_at_dispatch=True,
        round_trip="terminal",
    )


def _build_judgment_points(
    open_day_goals: dict[str, Any],
    dirty_tree_verdict: dict[str, Any],
    for_date: Optional[str] = None,
    register_stall: Optional[StallReport] = None,
    backfill_scan_result: Optional[GapScan] = None,
    catch_up: bool = False,
) -> list[dict[str, Any]]:
    """Tier-2/3 (open-question) judgment-point entries.

    `catch_up` drops the concluding points (`CONCLUDING_DIRECTIVES["workday-complete"]`) and
    `jp_day_goal_closeout`, whose only disposition target is the absent `d_goal_close_day`.
    The gap days' summaries are the `backfill-summary` agent group over the scan rows, so no
    gap-day analyst point is re-emitted.

    `backfill_scan_result` is `brief()`'s one pre-A0 scan: `jp_step3_5_backfill_cap` is
    emitted only when it holds more than `BACKFILL_CAP_ROWS` rows or any older gap. `None`
    (not probed) keeps the point with generic evidence.

    - `jp_requirement_register_stall` — reportable, appended only when
      `register_stall` is non-empty.

    - `jp_day_goal_closeout` — C4, conditional (see
      `_build_day_goal_closeout_judgment_point`): emitted only when C2
      reports at least one open day row. Insufficient-evidence, same shape
      as `jp_step2_5_dirty_tree_ambiguous` — whether a stated priority
      actually landed is a human judgment this assembler must never infer
      (C4 Anti-scope).
    - `jp_step2_5_dirty_tree_ambiguous` — tier-3, insufficient-evidence,
      ALSO conditional (AC10 fix, 2026-07-25): emitted only when
      `_compute_dirty_tree_verdict`'s read-only `--dry-run` probe found
      SOURCE-TREE/AMBIGUOUS paths remaining (`dirty_tree_verdict
      ["ambiguous"]`) — a clean tree carries no ask, and
      `d_step3_consolidate` stays ungated in that case (see
      `_build_directives`). The ambiguous-file listing is sourced from the
      engine's own `git status --porcelain` read (via that same probe),
      never attacker-influenceable content, so this is NOT an
      untrusted-gate construct (the Staff Engineer F3) — it is built via
      `build_judgment_point` with an explicit `recommendation=None` and
      `reason="insufficient-evidence"`, not via
      `build_untrusted_gate_judgment_point`.
    - `jp_step3_5_backfill_cap` — tier-2, recommendation required (the
      backfill-cap PM ask: auto-fan the full wave, or a bounded subset),
      conditional on the pre-A0 scan (see `backfill_scan_result`).
    - `jp_step4b_analyst_dispatch` / `jp_step4_5_clustering_dispatch` — tier-2, recommendation required
      (Sonnet-dispatch decisions).
    - `jp_step4e_health_ledger_new_rows` — tier-2, recommendation required
      (confirm which touched systems get a new unaudited `?` row; never
      touches audit clocks or existing grades, per Step 4e's own
      negative-spec).

    `for_date` (threaded verbatim from `brief()`'s own validated same-named
    kwarg, default `None`) scopes `jp_step4_5_clustering_dispatch`'s evidence
    string's `query-completions --where created=<date>` reference to the
    ceremony's own target day — `for_date` when set, else today's local
    calendar date — never a literal `<today>` placeholder left uninterpolated.

    2026-08-08 evidence-wording correction (falsifier search: neither this
    module, the Step 4b dispatched-worker body (`docs/commands/
    workday-complete.md` Step 4, coordinator-content-repo), nor any other producer in this
    repo computes a "zero new commits AND no agent-driven changes" boolean —
    a grep across `coordinator_core/` returns only this module's own string
    literal and a review-trail diff, and the skill prose at that doc's Step 4
    section is text a dispatched Sonnet worker READS AND JUDGES, not a
    computed predicate threaded back to this assembler) — `jp_step4b_
    analyst_dispatch`'s `evidence` no longer asserts that condition as an
    observed fact; it now says plainly that no producer computes it and that the Step 4 skip is a disposition
    the EM/dispatched worker judges, not a fact this module observed. This
    module still computes exactly two facts (`_compute_open_day_goals`,
    `_compute_dirty_tree_verdict`) — the Step 4 skip condition is
    deliberately NOT a third; no git-log/session-record producer is wired
    here to feed it, so no such predicate is invented.

    Follow-up correction (2026-08-14): the 2026-08-08 pass missed a sibling
    instance of the same defect on `jp_step4e_health_ledger_new_rows` — its
    `evidence` named a "today's touched-systems set" that, like the Step 4
    skip boolean above, no producer in this repo computes. Same remedy
    applied: the evidence string now states plainly that no producer
    computes the set and that which systems get a new row is a disposition,
    not an observed fact — no touched-systems producer was built, matching
    the narrow-the-evidence precedent over building a new producer.

    round_trip/revalidate_at_dispatch classification (AC14 step 5): the two
    ask-shaped points (`jp_step2_5_dirty_tree_ambiguous`,
    `jp_step3_5_backfill_cap`) are `round_trip="terminal"` with
    `revalidate_at_dispatch=True` — the underlying git/scan state can change
    between brief-time and apply-time and must be re-read, not trusted stale.
    The two Sonnet-dispatch points are `round_trip="round_trip"` with
    `revalidate_at_dispatch=False` — a dispatch decision, once made, is not
    re-litigated against fresh disk state before firing.
    """
    points: list[dict[str, Any]] = []
    if _open_day_goals_present(open_day_goals):
        points.append(_build_day_goal_closeout_judgment_point(open_day_goals))
    if dirty_tree_verdict.get("ambiguous"):
        points.append(
            build_judgment_point(
                None,
                id="jp_step2_5_dirty_tree_ambiguous",
                question=(
                    "Step 2.5 found source-tree edits without attribution or "
                    "other ambiguous dirty-tree paths (git status --porcelain). "
                    "Adopt-commit (mine, forgot to attribute), discard "
                    "(abandoned), or attribute to another session?"
                ),
                dispositions=[
                    build_disposition(
                        "adopt_commit", resolves=["d_step3_consolidate"]
                    ),
                    build_disposition("discard", resolves=["d_step3_consolidate"]),
                    build_disposition(
                        "attribute_to_session", resolves=["d_step3_consolidate"]
                    ),
                ],
                evidence=dirty_tree_verdict.get(
                    "evidence",
                    "workday-complete-step2_5-dirty-tree: ambiguous paths remain",
                ),
                reason="insufficient-evidence",
                revalidate_at_dispatch=True,
                round_trip="terminal",
            )
        )
    if _backfill_cap_live(backfill_scan_result):
        points.append(_backfill_cap_judgment_point(backfill_scan_result))
    points += [
        build_judgment_point(
            {
                "disposition": "dispatch",
                "rationale": (
                    "Step 4b's Sonnet analyst is the primary Work Completed "
                    "source; skip only on the zero-new-commits skip condition."
                ),
            },
            id="jp_step4b_analyst_dispatch",
            question="Dispatch the Step 4b Sonnet daily-summary analyst for today?",
            dispositions=[
                build_disposition("dispatch"),
                build_disposition("skip_no_new_work"),
            ],
            evidence=(
                "no producer computes this condition; Step 4 skip is a "
                "disposition, not an observed fact"
            ),
            reason="dispatch-decision",
            revalidate_at_dispatch=False,
            round_trip="round_trip",
            reportable=False,
        ),
        build_judgment_point(
            {
                "disposition": "dispatch_if_multi_entry_chains",
                "rationale": (
                    "Only chains with >=2 completion entries need a narrative "
                    "synthesis worker; single-entry chains skip by construction."
                ),
            },
            id="jp_step4_5_clustering_dispatch",
            question=(
                "Dispatch a Step 4.5 completion-log clustering worker per "
                "multi-entry chain found in today's completions?"
            ),
            dispositions=[
                build_disposition("dispatch_if_multi_entry_chains"),
                build_disposition("skip_only_mode"),
            ],
            evidence=(
                f"query-completions --where created={for_date or date.today().isoformat()} "
                "grouped by chain"
            ),
            reason="dispatch-decision",
            revalidate_at_dispatch=False,
            round_trip="round_trip",
            reportable=False,
        ),
        build_judgment_point(
            {
                "disposition": "add_unaudited_rows",
                "rationale": (
                    "Step 4e never writes a grade or touches audit clocks — "
                    "only confirms which newly-touched systems get a fresh "
                    "'?' row in state/health-ledger.md."
                ),
            },
            id="jp_step4e_health_ledger_new_rows",
            question=(
                "Add unaudited '?' health-ledger rows for systems touched "
                "today with no existing row?"
            ),
            dispositions=[
                build_disposition("add_unaudited_rows"),
                build_disposition("skip_no_new_systems"),
            ],
            evidence=(
                "no producer computes today's touched-systems set; which "
                "systems get a new unaudited row is a disposition, not an "
                "observed fact"
            ),
            reason="dispatch-decision",
            revalidate_at_dispatch=True,
            round_trip="terminal",
            reportable=False,
        ),
    ]
    if register_stall:
        points.append(stall_judgment_point(register_stall, id="jp_requirement_register_stall"))
    if catch_up:
        dropped = _CONCLUDING_IDS | {"jp_day_goal_closeout"}
        points = [p for p in points if p["id"] not in dropped]
    return points


def _reported_narration_suffix(reported_judgment_points: list[dict[str, Any]]) -> str:
    if not reported_judgment_points:
        return ""
    reported_bits = []
    for point in reported_judgment_points:
        recommendation = point.get("recommendation") or {}
        rationale = recommendation.get("rationale")
        bit = f"{point.get('id')} ({point.get('question')})"
        if rationale:
            bit += f" -- {rationale}"
        reported_bits.append(bit)
    return (
        f"{len(reported_judgment_points)} point(s) gate nothing on this run and are reported, "
        f"not asked: {'; '.join(reported_bits)}."
    )


def _resolve_day_period_mode(
    scan: GapScan,
    now_local: datetime,
    *,
    catch_up: bool,
    conclude: bool,
    single_date: bool,
) -> PeriodMode:
    """Resolve the day's mode, reading `missed` off the scan rows (a day absent from them is concluded).

    A `--for-date`/`--only` run keeps its single-date semantics: it always concludes."""
    gap_days: set[str] = set()
    for row in scan.rows:
        try:
            date.fromisoformat(row.date)
        except ValueError:
            continue
        gap_days.add(row.date)
    today = now_local.date()
    since = date.fromisoformat(min(gap_days)) if gap_days else None
    concluded: list[str] = []
    if since is not None:
        d = since
        while d < today:
            if d.isoformat() not in gap_days:
                concluded.append(d.isoformat())
            d += timedelta(days=1)
    explicit = "conclude" if single_date else "catch_up" if catch_up else "conclude" if conclude else None
    resolved = resolve_period_mode(
        "day", now_local, explicit, week_anchor=today, concluded=concluded, since=since
    )
    if single_date:
        resolved = replace(resolved, reason="single-date run (--for-date/--only) concludes the named day")
    return resolved


def _catch_up_narration_lead(period: PeriodMode) -> str:
    missed = ", ".join(period.missed) or "no missed days"
    return f"{NARRATION_LEAD_PREFIX}{missed} backfilled; {period.in_progress} left open"


def brief(
    *,
    decisions: Optional[dict[str, Any]] = None,
    env: Optional[dict[str, str]] = None,
    for_date: Optional[str] = None,
    only_mode: bool = False,
    scope_summary: Optional[str] = None,
    catch_up: bool = False,
    conclude: bool = False,
    now_local: Optional[datetime] = None,
) -> tuple[int, dict[str, Any]]:
    """Compute the workday-complete decision object. Read-only — never
    mutates disk/git state itself; every mutation is a named `directives[]`
    entry the apply half (`coordinator_core.workday_complete.apply`)
    executes. `decisions` (an EM-supplied `{judgment_point_id: {disposition,
    ...}}` map) is accepted and threaded through unchanged in the returned
    envelope's `decisions` key — this module does not resolve it itself.

    `for_date`/`only_mode` (default `None`/`False` — reproduces prior
    behavior byte-for-byte when omitted) date-scope the Step 3.5 Phase-B
    backfill dispatch (`d_step3_5_backfill_phase_b`) ONLY — see
    `_build_directives`'s docstring for the exact args shape. This module
    stays a pure compute function: no environment variable is read here to
    obtain either value (mirrors this module's own no-env-read purity,
    module docstring) — the caller (this module's own `main(argv)`, or an
    EM-side caller importing `brief()` directly) supplies them as explicit
    keyword arguments. `for_date`, when given, is validated against a strict
    `YYYY-MM-DD` calendar-date shape before use (regex-gate then
    `date.fromisoformat`, mirroring `workday_complete_backfill_scan.py`'s
    own `--today` validation idiom) — a malformed value is a usage error
    (`WorkdayExitCode.USAGE`), never a directive silently built with a bad
    string that later corrupts the Phase-B dispatch loop's row-matching.

    `scope_summary` (default `None` — reproduces prior behavior
    byte-for-byte when omitted) is the user's free-form day-summary prose;
    threaded verbatim (never validated or length-capped — it is arbitrary
    user prose bound only for an argv list, never a shell, so there is no
    injection surface to defend against here, and length is the changelog
    writer's own concern, not this assembler's) into BOTH
    `d_step9_changelog` and, when `for_date` is also set,
    `d_step3_5_backfill_phase_b` — see `_build_directives`'s docstring for
    the exact (deliberately asymmetric) args shape each directive gets and
    the leading-dash handling used for each.

    `catch_up`/`conclude` pick the period mode explicitly; both off lets the clock decide
    (`resolve_period_mode`, `now_local` defaulting to the coordinator's local now). Under
    `catch_up` the concluding directives are absent and the envelope's `narration` leads with
    the catch-up line. The resolved mode rides the envelope's top-level `period_mode` key.

    Returns `(exit_code, envelope)` using `WorkdayExitCode` (0-3, brief-side
    only — see module docstring).
    """
    conflict = period_flag_error(
        catch_up=catch_up, conclude=conclude, only_mode=only_mode, for_date=for_date
    )
    if conflict is not None:
        return int(WorkdayExitCode.USAGE), {"error": conflict}
    if for_date is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", for_date):
            return (
                int(WorkdayExitCode.USAGE),
                {"error": f"--for-date must be YYYY-MM-DD (got '{for_date}')"},
            )
        try:
            date.fromisoformat(for_date)
        except ValueError:
            return (
                int(WorkdayExitCode.USAGE),
                {"error": f"--for-date must be a valid YYYY-MM-DD date (got '{for_date}')"},
            )

    # Trap: d_step3_consolidate refuses these checkouts, but no committing
    # directive depends on it, so without this refusal apply() committed and
    # pushed straight to main (example-game-repo 2026-09-22). Refusing here runs nothing.
    branch = head_branch(Path.cwd())
    if branch in (None, "HEAD", "main", "master"):
        shown = "detached HEAD" if branch in (None, "HEAD") else f"'{branch}'"
        return (
            int(WorkdayExitCode.BUSINESS_FAIL),
            {
                "error": (
                    f"workday-complete refuses a {shown} checkout: its directives commit "
                    "and push. Run it from a workstream branch."
                )
            },
        )

    try:
        resolve_operator_config(env=env)
    except Exception as exc:  # noqa: BLE001 - mirrors pickup_assemble.brief's own backstop
        return int(WorkdayExitCode.TRANSPORT_FAIL), {"error": str(exc)}

    try:
        open_day_goals = _compute_open_day_goals()
        dirty_tree_verdict = _compute_dirty_tree_verdict()
        backfill_scan_result, backfill_scan_note = _compute_backfill_scan()
        period = _resolve_day_period_mode(
            backfill_scan_result,
            now_local if now_local is not None else local_now(),
            catch_up=catch_up,
            conclude=conclude,
            single_date=bool(for_date or only_mode),
        )
        in_catch_up = period.mode == "catch_up"
        directives = _build_directives(
            decisions if decisions is not None else {},
            open_day_goals,
            dirty_tree_verdict,
            for_date=for_date,
            only_mode=only_mode,
            scope_summary=scope_summary,
            backfill_cap_live=_backfill_cap_live(backfill_scan_result),
            catch_up=in_catch_up,
        )
        judgment_points = _build_judgment_points(
            open_day_goals,
            dirty_tree_verdict,
            for_date=for_date,
            register_stall=_compute_register_stall(),
            backfill_scan_result=backfill_scan_result,
            catch_up=in_catch_up,
        )
        recommendation_carrying = [
            point for point in judgment_points if point.get("recommendation") is not None
        ]
        _, reported_judgment_points = partition_reportable(
            recommendation_carrying, directives
        )
        reported_ids = {point.get("id") for point in reported_judgment_points}
        judgment_points = [
            point for point in judgment_points if point.get("id") not in reported_ids
        ]
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        return int(WorkdayExitCode.TRANSPORT_FAIL), {"error": str(exc)}

    narration = (
        "Step 2 (RAG staleness nudge) and Step 5 (plugin validation suite) "
        "have no consumes-manifest CLI and are NOT represented as "
        "directives[] entries; coordinator-queue-append IS a manifest "
        "member but is invoked by the weekly strategic observer, not the "
        "assembler, so it is also never a directives[] entry — see this "
        "module's negative-spec."
    )
    if backfill_scan_note:
        narration = f"{narration} {backfill_scan_note}"
    reported_narration_suffix = _reported_narration_suffix(reported_judgment_points)
    if reported_narration_suffix:
        narration = f"{narration} {reported_narration_suffix}"
    if in_catch_up:
        narration = f"{_catch_up_narration_lead(period)}. {narration}"

    envelope = build_envelope(
        artifact={"kind": "ceremony", "name": "workday-complete"},
        preflight={"consumes_manifest": list(CONSUMES_MANIFEST)},
        gates={
            "cockpit_contract_freshness": compute_cockpit_contract_freshness(),
            "completion_receipts": _compute_completion_receipts_gate(for_date),
        },
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions if decisions is not None else {},
        narration=narration,
        next_move="resolve open judgment_points, then dispatch apply()",
    )
    emit(envelope)
    # Trap: attached after `emit`, whose 8-key check mirrors the DoE schema-of-record and
    # refuses a ninth key; the cadence fragment pins `period_mode` as a top-level brief key.
    envelope[PERIOD_MODE_KEY] = period.to_dict()
    return int(WorkdayExitCode.SUCCESS), envelope


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="workday-complete-assemble brief", add_help=False)
    parser.add_argument("--for-date", dest="for_date", default=None)
    parser.add_argument("--only", dest="only_mode", action="store_true")
    parser.add_argument("--scope-summary", dest="scope_summary", default=None)
    parser.add_argument(CATCH_UP_FLAG, dest="catch_up", action="store_true")
    parser.add_argument(CONCLUDE_FLAG, dest="conclude", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else int(WorkdayExitCode.USAGE)

    period_flags = {k: True for k in ("catch_up", "conclude") if getattr(args, k)}
    exit_code, envelope = brief(
        for_date=args.for_date,
        only_mode=args.only_mode,
        scope_summary=args.scope_summary,
        **period_flags,
    )
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
