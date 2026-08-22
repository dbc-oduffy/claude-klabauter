"""
coordinator_core.workday_complete.brief — the `workday-complete` computed-skill
engine's READ-ONLY compute half.

Purpose: computes `coordinator/commands/workday-complete.md`'s mechanical
step inventory (Step 1/1.5/2.6/2.65/3/3.5-PhaseA0/4a/6/7.5/8/9/10/10.5/10.6) into the
canonical 8-key decision-object envelope, and surfaces every judgment-shaped
step (Step 2.5 exit-2 ambiguous-dirty-tree ask, Step 3.5 Phase A/B backfill-cap
PM ask, Step 4b/4c/4.5 Sonnet-dispatch decisions, Step 4e health-ledger new-row
confirmation) as an overridable `judgment_points[]` offer rather than deciding
it for the caller.

This module imports its envelope/judgment-point constructors from the shipped
canonical-resolution-engine library (`coordinator_core.resolution.facade`,
`coordinator_core.contract.decision_object.{envelope,judgment}`) rather than
reimplementing them — see the negative-spec below.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b1-ceremony-complete-computed--9ffa54, chunk C2

Consumes-manifest (C1 census, plan § Tasks C1 body) — orchestrates, reimplements
none of the following existing atomic CLIs (every `directives[].cli` value below
is a literal name drawn from exactly this set):
    workday-complete-args-and-validate, workday-complete-reconcile,
    workday-complete-step2_5-dirty-tree, reap-orphaned-in-flight-handoffs,
    workday-complete-step3-consolidate, workday-complete-backfill-scan,
    workday-complete-backfill-anchor, workday-complete-close, standup,
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
      is likewise never a `directives[].cli` value — it is invoked by the
      Step 4c strategic-observer Sonnet worker this module's
      `jp_step4c_observer_dispatch` judgment point recommends dispatching
      (`docs/commands/workday-complete.md` § Step 4c), not by the assembler
      itself. A dispatched-worker's own CLI use is outside what a `directives[]`
      entry can express (a directive is an apply-half instruction, not a
      sub-dispatch's tool belt) — this is the manifest's third documented
      exception, found by the AC10 conformance test (2026-07-25) alongside
      the Step2/Step5 pair above.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

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
from coordinator_core.git.repo_root import git_common_dir
from coordinator_core.ops.emit.envelope import resolve_context
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.goal_close_day import collect_open_day_goals
from coordinator_core.ops.workday_complete_step2_5_dirty_tree import (
    main as _step2_5_dirty_tree_main,
)
from coordinator_core.resolution.facade import resolve_operator_config
from coordinator_core.workday_complete.cockpit_contract_freshness import (
    compute_cockpit_contract_freshness,
)

# ---------------------------------------------------------------------------
# Exit-code contract (brief-side, 0-3) — locally scoped to this compute half,
# NOT shared with the apply half's own 0-4 enumeration (see apply.py's own
# `WorkdayApplyExitCode`; computed-skills.md § Exit-code contract for a
# mutating half requires each half to pin its own set).
# ---------------------------------------------------------------------------
WorkdayExitCode = extend_exit_codes(
    "WorkdayExitCode",
    BUSINESS_FAIL=1,
    USAGE=2,
    TRANSPORT_FAIL=3,
)

#: The C1 consumes-manifest (plan § Tasks C1 body) — the CLOSED set of CLI
#: names any `directives[].cli` value in this module is drawn from. Never
#: extended ad hoc; a new mechanical step needs a manifest update first.
CONSUMES_MANIFEST: tuple[str, ...] = (
    "workday-complete-args-and-validate",
    "workday-complete-reconcile",
    "workday-complete-step2_5-dirty-tree",
    "reap-orphaned-in-flight-handoffs",
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


# Review: code-reviewer (Finding 1, P1) — previously used `--show-toplevel`
# and called `resolve_context(repo_root)` directly, bypassing
# `main_worktree_root`; that scoped the read to the CURRENT worktree while
# the write leg always scopes to the MAIN worktree, so a ceremony invoked
# from a linked worktree could offer a goal_id that isn't an open row where
# close-out actually writes. Now resolves `--git-common-dir` and routes
# through `main_worktree_root`, matching every sibling `resolve_context()`
# call site.
def _resolve_repo_common_dir_for_ceremony(start: Optional[Path] = None) -> Optional[Path]:
    """`git rev-parse --path-format=absolute --git-common-dir` for `start`
    (default cwd) — the invoking repo's git common dir, i.e. `<main-worktree>
    /.git` even when `start` is inside a LINKED worktree (git always resolves
    `--git-common-dir` to the main worktree's `.git`, unlike `--show-toplevel`
    which returns the CURRENT worktree's own root). Callers derive the main
    worktree root via `main_worktree_root(common_dir)` before calling
    `resolve_context` — same convention every other `resolve_context()` call
    site in the tree follows (`ops/emit_cadence.py`, `ops/artifact_emit.py`,
    `ops/goal_append.py`, `ops/goal_close_day.py`, `ops/emit/recorder.py`).
    Returns `None` on any resolution failure — never raises; the caller
    degrades to an empty open-day-goals partition (see
    `_compute_open_day_goals`)."""
    cwd = start or Path.cwd()
    out = git_common_dir(str(cwd))
    return Path(out) if out else None


def _compute_open_day_goals() -> dict[str, Any]:
    """Read-only C2 leg for the day-goal close-out judgment point: open
    `period: day` rows (today + stale), scoped to the invoking repo.

    Never raises and never fails the ceremony (mandatory degradation
    clause, module docstring) — an unresolvable repo root, or any
    unexpected error resolving context/reading the wire, degrades to an
    empty `{"today": [], "stale": [], "unreadable_error": <str>}`
    partition. `collect_open_day_goals` itself already degrades an
    unreadable `central_state_root` the same way; this wrapper only adds
    the repo-root-resolution failure mode on top.

    Resolves the MAIN worktree root via `main_worktree_root(common_dir)`
    before calling `resolve_context` — never a bare `git rev-parse
    --show-toplevel` result — so a `/workday-complete` invocation from a
    linked worktree (a first-class, documented layout in this repo) reads
    the SAME `state/` tree the write leg (`ops/goal_close_day.py`'s
    `goal.close_day_apply` handler) writes to. Using the current worktree's
    own root here would scope the read to a different `state/` shard than
    the write, causing the judgment point to offer a `goal_id` that does not
    exist as an open row where the close-out actually writes.
    """
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


def _open_day_goals_present(open_day_goals: dict[str, Any]) -> bool:
    return bool(open_day_goals.get("today")) or bool(open_day_goals.get("stale"))


def _main_worktree_root_for_directive() -> str:
    """Main worktree root as a `str`, for directives whose CLI takes a `root`
    positional (currently `d_step3_5_backfill_anchor_a0` ->
    `workday-complete-backfill-anchor run <root>`).

    Same resolution ladder as `_compute_open_day_goals`/
    `_resolve_repo_common_dir_for_ceremony`: `git rev-parse --path-format=
    absolute --git-common-dir` then `main_worktree_root(common_dir)` — never
    a bare `Path.cwd()` and never `git rev-parse --show-toplevel`, since a
    ceremony invoked from a LINKED worktree must resolve to the SAME `state/`
    tree the write leg (here, the anchor CLI's own commit-and-write step)
    addresses; `--show-toplevel` would instead scope to the current linked
    worktree. Never raises and never fails the ceremony — degrades to `"."`
    (the CLI's own cwd-relative fallback) on any resolution failure, matching
    the defensive posture `_compute_open_day_goals`/`_compute_dirty_tree_verdict`
    already take."""
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
    """Read-only `--dry-run` probe of Step 2.5's own auto-disposition script
    (`coordinator_core.ops.workday_complete_step2_5_dirty_tree`) — determines
    whether SOURCE-TREE/AMBIGUOUS paths remain (exit 2) WITHOUT performing
    any git mutation; `--dry-run` makes no commits, no `.gitignore` edits, no
    `git rm --cached` (that module's own docstring). Never raises and never
    fails the ceremony (mirrors `_compute_open_day_goals`'s degradation
    posture) — but degrades toward `ambiguous=True` on any probe failure,
    i.e. toward asking the EM, never toward silently skipping a genuine ask."""
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(
            stderr_buf
        ):
            exit_code = _step2_5_dirty_tree_main(["--dry-run"])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - never fail the ceremony
        return {
            "ambiguous": True,
            "evidence": (
                "workday-complete-step2_5-dirty-tree --dry-run probe raised "
                f"{exc!r}, degrading to ambiguous=True (fail toward asking)"
            ),
        }
    evidence = (stdout_buf.getvalue() + stderr_buf.getvalue()).strip()
    return {
        "ambiguous": exit_code == 2,
        "evidence": evidence or "workday-complete-step2_5-dirty-tree --dry-run: no output",
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
    (part of Step 3.5) waits on the backfill-cap PM ask when the gap-row
    count exceeds the cap, and `d_goal_close_day` waits on
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
            "d_step2_6_completion_reconcile",
            cli="workday-complete-reconcile",
            args=["completion-reconcile"],
        ),
        _directive(
            "d_step2_65_reap_orphans",
            cli="reap-orphaned-in-flight-handoffs",
            args=[],
        ),
        _directive(
            "d_step3_consolidate",
            cli="workday-complete-step3-consolidate",
            args=[],
            # Gated ONLY when dirty_tree_verdict found the tree genuinely
            # ambiguous (AC10 fix) — a clean tree has no live ask to wait
            # on, so this directive stays ungated (fires unconditionally)
            # rather than permanently blocking on a JP that would never
            # resolve to anything.
            depends_on=(
                "jp_step2_5_dirty_tree_ambiguous"
                if dirty_tree_verdict.get("ambiguous")
                else None
            ),
        ),
        _directive(
            "d_step3_5_backfill_scan",
            cli="workday-complete-backfill-scan",
            args=["--lookback", "14"],
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
            args=["run", _main_worktree_root_for_directive()],
            # `run` also reads the Phase-A0 gap-row TSV on stdin (2026-07-26
            # stdin-wiring fix) — `apply._execute_directives` feeds it the
            # scan directive's own captured stdout.
            stdin_from="d_step3_5_backfill_scan",
        ),
        _directive(
            "d_step3_5_backfill_phase_b",
            cli="workday-complete-close",
            args=(
                ["backfill-dispatch-rows", "--for-date", for_date]
                if for_date
                else ["backfill-dispatch-rows"]
            )
            + (["--only-mode"] if for_date and only_mode else [])
            + ([f"--scope-summary={scope_summary}"] if for_date and scope_summary else []),
            # Gated: the >10-row backfill-cap PM ask, when live, must resolve
            # before the oldest-first Phase-B wrap dispatches.
            depends_on="jp_step3_5_backfill_cap",
            # `backfill-dispatch-rows` also reads the same gap-row TSV on
            # stdin (2026-07-26 stdin-wiring fix) — same producer as the
            # anchor directive above; both consume the scan's one output.
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
            # This op self-selects AND git-mv's closed bug-backlog entries —
            # the only other data-loss-capable directive in the manifest
            # besides cruft-sweep (arg-mismatch audit, class (d)/highest-
            # severity subset #2). Its previous `args=[]` left it resolving
            # root via `git rev-parse --show-toplevel` from the ceremony
            # process's own cwd; explicit `--repo-root` matches every other
            # write-leg directive in this module.
            args=["--repo-root", _main_worktree_root_for_directive()],
        ),
        _directive(
            "d_step8_improvement_queue_nudge",
            cli="workday-start-advisory-counters",
            # `--repo-root` defaults to `.` (process cwd) — explicit here so
            # a ceremony invoked from a linked worktree reads the SAME
            # `state/` tree every other directive in this module resolves
            # via `_main_worktree_root_for_directive` (arg-mismatch audit,
            # class (d)); the CLI already accepted this flag, it was simply
            # never supplied.
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
                # Gated: jp_day_goal_closeout must resolve to "record" before
                # the close-out append runs — an ungated directive would fire
                # with empty/partial args (see this function's docstring).
                depends_on="jp_day_goal_closeout",
            )
        )
    directives += [
        *build_ceremony_close_tail(
            post_command_hook_id="d_step10_5_post_command_hook",
            ceremony_name="workday-complete",
        ),
    ]
    for entry in directives:
        assert entry["cli"] in CONSUMES_MANIFEST, (
            f"_build_directives: directive {entry['id']!r} names {entry['cli']!r}, "
            "not a member of CONSUMES_MANIFEST (AC15c: no phantom verbs)"
        )
    return directives


def _build_day_goal_closeout_judgment_point(open_day_goals: dict[str, Any]) -> dict[str, Any]:
    """C4's conditional judgment point — see `_build_directives`'s docstring
    for why this one (unlike every point below) is only ever constructed
    when `_open_day_goals_present(open_day_goals)` is true. Presents today
    and stale rows DISTINCTLY in both `question` and `evidence` (C2
    partitions them; C4 must not flatten the distinction back out).

    # Review: code-reviewer (Finding 4, P2) — row access uses `.get(...,
    # "?")` rather than bracket access so a malformed row (missing
    # goal_id/text) degrades to a placeholder instead of raising a KeyError
    # out of `brief()`'s top-level backstop.
    """
    today_rows = open_day_goals.get("today") or []
    stale_rows = open_day_goals.get("stale") or []
    today_desc = (
        "; ".join(f"{row.get('goal_id', '?')}: {row.get('text', '?')}" for row in today_rows)
        or "none"
    )
    stale_desc = (
        "; ".join(f"{row.get('goal_id', '?')}: {row.get('text', '?')}" for row in stale_rows)
        or "none"
    )
    return build_judgment_point(
        None,
        id="jp_day_goal_closeout",
        question=(
            f"{len(today_rows)} day goal(s) due today and {len(stale_rows)} "
            "stale day goal(s) from prior days are open on the goals wire. "
            "Record a done/dropped outcome for each in "
            "decisions['day_goal_closeout'] = {goal_id: 'done'|'dropped'} "
            "(any value other than exactly 'done' closes 'dropped' — DEC-1), "
            "or skip for now?"
        ),
        dispositions=[
            build_disposition("record", resolves=["d_goal_close_day"]),
            build_disposition("skip", resolves=[]),
        ],
        evidence=f"goal.close_day today=[{today_desc}] stale=[{stale_desc}]",
        reason="insufficient-evidence",
        revalidate_at_dispatch=True,
        round_trip="terminal",
    )


def _build_judgment_points(
    open_day_goals: dict[str, Any],
    dirty_tree_verdict: dict[str, Any],
    for_date: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Tier-2/3 (open-question) judgment-point entries.

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
      backfill-cap PM ask: auto-fan the full wave, or a bounded subset).
    - `jp_step4b_analyst_dispatch` / `jp_step4c_observer_dispatch` /
      `jp_step4_5_clustering_dispatch` — tier-2, recommendation required
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
    module, the Step 4b/4c dispatched-worker bodies (`docs/commands/
    workday-complete.md` Step 4, DoE-claude), nor any other producer in this
    repo computes a "zero new commits AND no agent-driven changes" boolean —
    a grep across `coordinator_core/` returns only this module's own string
    literal and a review-trail diff, and the skill prose at that doc's Step 4
    section is text a dispatched Sonnet worker READS AND JUDGES, not a
    computed predicate threaded back to this assembler) — `jp_step4b_
    analyst_dispatch`'s and `jp_step4c_observer_dispatch`'s `evidence` no
    longer assert that condition as an observed fact; both now say plainly
    that no producer computes it and that the Step 4 skip is a disposition
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
    The three Sonnet-dispatch points are `round_trip="round_trip"` with
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
                    "workday-complete-step2_5-dirty-tree exit 2 (ambiguous paths remain)",
                ),
                reason="insufficient-evidence",
                revalidate_at_dispatch=True,
                round_trip="terminal",
            )
        )
    points += [
        build_judgment_point(
            {
                "disposition": "bounded_subset",
                "rationale": (
                    "A 10+-day gap is a signal worth a human glance, not a "
                    "silent 10-agent burst; default to the PM-named bounded "
                    "subset over an unbounded auto-fan."
                ),
            },
            id="jp_step3_5_backfill_cap",
            question=(
                "Backfill scan found more than 10 skipped days (post-Phase-A0 "
                "true gaps). Backfill all, or a bounded subset?"
            ),
            dispositions=[
                build_disposition("backfill_all", resolves=["d_step3_5_backfill_phase_b"]),
                build_disposition(
                    "bounded_subset", resolves=["d_step3_5_backfill_phase_b"]
                ),
            ],
            evidence="workday-complete-backfill-scan --lookback 14 (post-A0 row count > 10)",
            reason="pm-scoped-tradeoff",
            revalidate_at_dispatch=True,
            round_trip="terminal",
        ),
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
            # Action-class, explicitly decided (plan's C1b correction,
            # premise-finding sidecar channel 3): the EM dispatches a Sonnet
            # worker off this answer, with no directive and no gate --
            # demoting it into narration would silence a real dispatch
            # decision. `False`, not left unmarked, so this reads as a
            # deliberate call rather than an oversight the census could
            # otherwise flag.
            reportable=False,
        ),
        build_judgment_point(
            {
                "disposition": "dispatch",
                "rationale": (
                    "The strategic observer leaves a paper trail for weekly "
                    "the Staff Engineer review; dispatch in parallel with 4b whenever 4b "
                    "itself dispatches."
                ),
            },
            id="jp_step4c_observer_dispatch",
            question="Dispatch the Step 4c strategic-observer worker in parallel with 4b?",
            dispositions=[
                build_disposition("dispatch"),
                build_disposition("skip_no_new_work"),
            ],
            evidence=(
                "no producer computes a zero-new-commits/no-agent-driven-"
                "changes condition either; Step 4c's skip is a disposition "
                "paired with 4b's, not an observed fact"
            ),
            reason="dispatch-decision",
            revalidate_at_dispatch=False,
            round_trip="round_trip",
            # Action-class, explicitly decided -- see jp_step4b_analyst_
            # dispatch's comment above.
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
            # Action-class, explicitly decided -- see jp_step4b_analyst_
            # dispatch's comment above.
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
            # Action-class, explicitly decided -- see jp_step4b_analyst_
            # dispatch's comment above: answering this writes a health-
            # ledger row directly, with no directive and no gate.
            reportable=False,
        ),
    ]
    return points


def _reported_narration_suffix(reported_judgment_points: list[dict[str, Any]]) -> str:
    """Spec: docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-
    being-questions.md. A point `partition_reportable`
    classified as `reported` (gates no directive present on this envelope)
    is demoted out of `judgment_points[]` but must not go silent -- its
    question and its recommendation's `rationale` (when it carries one) are
    folded into `narration` instead, so the EM still sees the fact without
    being asked to answer a question that cannot change anything. No
    envelope key is added for this -- `narration` is already free-form.
    Returns `""` when `reported_judgment_points` is empty -- callers must
    join conditionally (see `workweek_complete.brief._reported_narration`,
    same shape).
    """
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


def brief(
    *,
    decisions: Optional[dict[str, Any]] = None,
    env: Optional[dict[str, str]] = None,
    for_date: Optional[str] = None,
    only_mode: bool = False,
    scope_summary: Optional[str] = None,
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

    Returns `(exit_code, envelope)` using `WorkdayExitCode` (0-3, brief-side
    only — see module docstring).
    """
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

    try:
        resolve_operator_config(env=env)
    except Exception as exc:  # noqa: BLE001 - mirrors pickup_assemble.brief's own backstop
        return int(WorkdayExitCode.TRANSPORT_FAIL), {"error": str(exc)}

    # Review: code-reviewer (Finding 4, P2) — _build_directives/
    # _build_judgment_points now consume live, disk-derived open_day_goals
    # rows (previously pure static construction taking no arguments); widen
    # this backstop to cover them so a malformed row can't crash the WHOLE
    # /workday-complete assembly instead of degrading per the module's
    # never-fail-the-ceremony mandate.
    try:
        open_day_goals = _compute_open_day_goals()
        dirty_tree_verdict = _compute_dirty_tree_verdict()
        directives = _build_directives(
            decisions if decisions is not None else {},
            open_day_goals,
            dirty_tree_verdict,
            for_date=for_date,
            only_mode=only_mode,
            scope_summary=scope_summary,
        )
        judgment_points = _build_judgment_points(
            open_day_goals, dirty_tree_verdict, for_date=for_date
        )
        # Scoped to recommendation-carrying points: a Tier-3 point
        # (`recommendation=None`, reason `insufficient-evidence` or
        # `pm-scoped-tradeoff`) is a question the engine deliberately must not
        # answer. Its `resolves` names a directive that is not always emitted,
        # so an unscoped partition would demote it out of `judgment_points[]`
        # on exactly the runs where nothing else raises it.
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
        "member but is invoked by the Step 4c strategic-observer dispatch, "
        "not the assembler, so it is also never a directives[] entry — "
        "see this module's negative-spec."
    )
    reported_narration_suffix = _reported_narration_suffix(reported_judgment_points)
    if reported_narration_suffix:
        narration = f"{narration} {reported_narration_suffix}"

    envelope = build_envelope(
        artifact={"kind": "ceremony", "name": "workday-complete"},
        preflight={"consumes_manifest": list(CONSUMES_MANIFEST)},
        gates={"cockpit_contract_freshness": compute_cockpit_contract_freshness()},
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions if decisions is not None else {},
        narration=narration,
        next_move="resolve open judgment_points, then dispatch apply()",
    )
    emit(envelope)
    return int(WorkdayExitCode.SUCCESS), envelope


def main(argv: list[str]) -> int:
    """`main()`'s `brief` dispatch — `--for-date DATE`, `--only`, and
    `--scope-summary TEXT` are the only three options (mirrors
    `pickup_assemble.brief`'s CLI shape at its simplest, extended with the
    flags the DoE-side `workday-complete.md` Step 2 invocation now threads
    through for a targeted wrap and for the user's day-summary prose);
    prints the envelope as JSON. A malformed `--for-date` is a parser-level
    usage error here (argparse's own `error()` -> exit 2), same effective
    exit code as `brief()`'s own `WorkdayExitCode.USAGE` -- this front door
    additionally catches it before ever calling `brief()` so the usage
    message names the CLI flag, not the keyword argument.

    `--scope-summary` is safe against a leading-dash value ONLY when the
    caller supplies it as a single `--scope-summary=VALUE` token (the form
    `workday-complete.md` Step 2 uses) -- a split `--scope-summary`, `VALUE`
    two-token invocation is subject to the same argparse-misclassifies-a-
    dash-prefixed-value hazard `_build_directives`'s docstring documents for
    the directives this value is threaded into."""
    parser = argparse.ArgumentParser(prog="workday-complete-assemble brief", add_help=False)
    parser.add_argument("--for-date", dest="for_date", default=None)
    parser.add_argument("--only", dest="only_mode", action="store_true")
    parser.add_argument("--scope-summary", dest="scope_summary", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else int(WorkdayExitCode.USAGE)

    exit_code, envelope = brief(
        for_date=args.for_date, only_mode=args.only_mode, scope_summary=args.scope_summary
    )
    print(json.dumps(envelope, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
