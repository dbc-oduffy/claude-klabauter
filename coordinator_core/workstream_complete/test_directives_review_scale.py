"""test_directives_review_scale — direct unit coverage of
`coordinator_core.workstream_complete.directives_review.decide_review_scale`
(the diff-shape row-selection table, SKILL.md:413-426), plus (below,
`# --- C5 ---`) the C5 disk-measurement wiring
(`coordinator_core.workstream_complete._measure_session_review_scale_inputs`
/ `brief()`'s backfill of `gross_loc`/`commit_count`/`surface_count`).

Spec backlink: pln-chain-end-review-scale-wire-de-23a81a,
chunk C1(b). Pins the tri-state-input contract that chunk introduces: every
row-4/5/6 input (`gross_loc`, `code_loc`, `commit_count`, `surface_count`,
`executor_dispatched`, `shared_schema_touched`, `chain_partition_verdict`)
is independently representable as "not yet resolved" (`None`), distinct
from a resolved falsy value — and an unresolved input that could still
change the selected row must yield the explicit unresolved outcome
(`resolved=False`, `row=None`, `partition_mandatory=False`), never a
silent per-session row. This is the regression pin for the defect the
plan's Problem section names: `chain_diff_trivial=None` used to resolve
toward LESS review on a chain terminal while its own docstring called that
"conservative" — the opposite of true.

C4 (docs/plans/2026-08-03-chain-end-review-scale-wiring.md) owns the
end-to-end coverage through `wsc.brief()`; this file only exercises the
pure function directly, per that plan's own chunk split.

C5 (docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md)
adds coverage for the disk-measurement half: `gross_loc`/`commit_count`/
`surface_count` were pure `decisions.get()` passthrough at the
`decide_review_scale` call site inside `brief()`, which is why
`jp-review-scale` fired unresolved on a normal close. See
`coordinator_core.workstream_complete._measure_session_review_scale_inputs`'s
own docstring for the range-resolution shape (mirrors
`backlog_grind_assemble.readers_mise._measure_range`, the plan's named
reference implementation).
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony.wsc_disposition import (
    LEGACY_PREDECESSOR_CONSUMED,
    PREDECESSOR_CONSUMED,
    SINGLE_SESSION,
)
from coordinator_core.workstream_complete import directives_review, judgments
from coordinator_core.workstream_complete.directives_review import (
    _CHAIN_VERDICT_PARTITION_MANDATORY,
    _CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    decide_review_scale,
)

_SINGLE_SESSION = SINGLE_SESSION
_CHAIN_TERMINAL = PREDECESSOR_CONSUMED
_LEGACY_CHAIN_TERMINAL = LEGACY_PREDECESSOR_CONSUMED

# A fully-resolved, small/non-triggering baseline for every row-3/4 input —
# individual tests override only the field(s) under test.
_RESOLVED_SMALL = dict(
    gross_loc=10,
    code_loc=10,
    commit_count=1,
    surface_count=1,
    executor_dispatched=False,
    shared_schema_touched=False,
)


# ---------------------------------------------------------------------------
# Row 1/2 — per-session, no chain-end, no brightline, no row-3 trigger
# ---------------------------------------------------------------------------


def test_row1_doc_only_no_code_touched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row == 1
    assert decision.scale == "none"
    assert decision.partition_mandatory is False


def test_row2_small_fix_under_ceiling():
    decision = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION)
    assert decision.resolved is True
    assert decision.row == 2
    assert decision.scale == "none"
    assert decision.commit_message_names_change is True


# ---------------------------------------------------------------------------
# Row 3 — executor dispatched / >50 LOC code / shared schema touched
# ---------------------------------------------------------------------------


def test_row3_executor_dispatched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_row3_code_loc_over_small_fix_ceiling():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 51},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3


def test_row3_shared_schema_touched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "shared_schema_touched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3


# ---------------------------------------------------------------------------
# Row 4 — session-scoped big-diff brightline, fires regardless of chain
# disposition, and outranks row 3.
# ---------------------------------------------------------------------------


def test_row4_gross_loc_over_brightline():
    """C5, 2026-08-11: row 4 reads `code_loc`, not `gross_loc` (C2) -- a
    `gross_loc` bump alone (code_loc still small) must NOT trip row 4."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 2

    decision_code_loc = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 500},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision_code_loc.row == 4
    assert decision_code_loc.scale == "partitioned"
    assert decision_code_loc.partition_mandatory is True


def test_row4_commit_count_over_brightline():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "commit_count": 5},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


def test_row4_surface_count_over_brightline():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "surface_count": 4},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


def test_row4_outranks_row3_even_when_row3_would_also_fire():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 500, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


# ---------------------------------------------------------------------------
# Row 5/6 — chain-terminal rows, keyed on the already-emitted chain
# brightline-gate verdict (chain_partition_verdict), not raw chain metrics.
# ---------------------------------------------------------------------------


def test_row6_chain_terminal_partition_mandatory_verdict():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.resolved is True
    assert decision.row == 6
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row6_fires_on_legacy_chain_terminal_spelling():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_LEGACY_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6


def test_row6_outranks_session_scoped_row4_brightline():
    """Chain-scoped mandatory partition wins even when the closing
    session's own diff is small — this is the exact gap the plan's
    Problem section describes: a big diff accumulated across many
    individually-small sessions, no single session ever tripping the
    session-scoped brightline."""
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6


def test_row6_outranks_row4_when_both_fire_on_the_same_call():
    """(review-integrator finding 1) The one adversarial combination the
    dispatch brief asked to be challenged hardest: chain_partition_verdict
    is PARTITION-MANDATORY (row 6 would fire) AND the session's own diff
    is simultaneously over the row-4 brightline. Precedence is 6 > 4
    (SKILL.md:426) -- the chain-terminal MANDATORY branch returns
    unconditionally before `brightline_known_true` is ever evaluated. Pins
    that precedence so a future refactor hoisting the brightline check
    above the chain-terminal check cannot silently flip it."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row4_outranks_row6_when_session_diff_itself_is_big():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 500},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.row == 4


def test_row5_chain_terminal_resolved_non_mandatory_verdict():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is True
    assert decision.row == 5
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_single_session_disposition_never_reaches_row5_or_6():
    """A non-chain-terminal disposition must never select rows 5/6 even
    when a chain_partition_verdict is (incorrectly) supplied — the
    predicate is gated on disposition first."""
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_SINGLE_SESSION,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row not in (5, 6)


# ---------------------------------------------------------------------------
# Row 4 must be evaluable on a chain terminal whose chain_partition_verdict
# is None/unrecognized — the fix for the defect where such a close silently
# under-reported to unresolved/partition_mandatory=False even when the
# session's own diff emphatically hit the row-4 brightline. Precedence
# stays 6 > 4 > (chain-terminal-unresolved) > 5 > 3 > 1 > 2.
# ---------------------------------------------------------------------------


def test_row4_fires_on_chain_terminal_with_unresolved_verdict_and_brightline_hit():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 4263, "commit_count": 13, "surface_count": 7},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=None,
    )
    assert decision.resolved is True
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_chain_terminal_unresolved_verdict_and_brightline_not_hit_stays_unresolved():
    """All three row-4 inputs are resolved and below brightline, so row 4
    is ruled out — the chain-terminal rows-5/6 unresolved outcome applies,
    same as before this fix."""
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=None,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.scale == "unresolved"
    assert decision.partition_mandatory is False


def test_row6_still_outranks_row4_on_chain_terminal_with_verdict_and_brightline_both_hit():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 4263, "commit_count": 13, "surface_count": 7},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row4_fires_on_chain_terminal_with_unrecognized_verdict_and_brightline_hit():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 4263, "commit_count": 13, "surface_count": 7},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict="garbage-not-a-real-verdict",
    )
    assert decision.resolved is True
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_non_chain_terminal_row_selections_unchanged_by_hoist():
    """Regression guard: hoisting the brightline computation above the
    is_chain_terminal branch must not change any non-chain-terminal row
    selection (rows 1/2/3/4)."""
    row1 = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 0, "executor_dispatched": False, "shared_schema_touched": False},
        chain_disposition=_SINGLE_SESSION,
    )
    assert row1.row == 1
    row2 = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION)
    assert row2.row == 2
    row3 = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert row3.row == 3
    row4 = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 500},
        chain_disposition=_SINGLE_SESSION,
    )
    assert row4.row == 4


# ---------------------------------------------------------------------------
# Unresolved outcome — the silent-lenient-fallback kill switch. Every
# assertion below must yield resolved=False, row=None, scale="unresolved",
# partition_mandatory=False — NEVER a silent per-session row.
# ---------------------------------------------------------------------------


def test_unresolved_when_chain_terminal_and_verdict_is_none():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=None,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.scale == "unresolved"
    assert decision.partition_mandatory is False


def test_unresolved_when_chain_terminal_and_verdict_is_unrecognized_string():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict="garbage-not-a-real-verdict",
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.partition_mandatory is False


def test_unresolved_never_defaults_to_row5_staff_eng_finding_3():
    """The exact staff-engineer finding-3 case: the chain-scoped verdict HAS
    resolved (and is non-mandatory) but a row-4 input (code_loc, C5's
    row-4 input since C2) has not — row 4 outranks row 5 and cannot be
    ruled out, so the outcome must be unresolved, never row 5."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.row != 5
    assert decision.partition_mandatory is False


def test_unresolved_never_defaults_to_row5_when_commit_count_absent():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "commit_count": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row != 5


def test_unresolved_never_defaults_to_row5_when_surface_count_absent():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "surface_count": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row != 5


def test_known_true_brightline_input_still_resolves_row4_even_with_other_inputs_absent():
    """A single input that is ALREADY known to trip the brightline
    (code_loc >= threshold, C5's row-4 input since C2) resolves row 4 even
    though a sibling input (surface_count) is unresolved — short-circuit on
    a known-true OR branch, not a blanket "any None anywhere means
    unresolved" rule."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 500, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row == 4


def test_unresolved_when_row4_metrics_absent_on_single_session():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": None, "commit_count": None, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False
    assert decision.row is None


def test_unresolved_when_row3_inputs_absent_after_row4_ruled_out():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False
    assert decision.row is None


def test_known_true_row3_input_absent_code_loc_now_blocks_on_row4_c5_finding():
    """C5, 2026-08-11 FINDING, restored 2026-08-11 (C7's reorder reverted per
    reviewer finding P1): since C2 pointed row 4's brightline at `code_loc`
    (previously `gross_loc`), an unresolved `code_loc` blocks row 4's own
    resolution check -- which runs BEFORE row 3 is ever evaluated -- even
    when `executor_dispatched=True` is independently known and would
    resolve row 3 outright. This is deliberate, not the C5-era regression it
    first looked like: row 3 is a strictly smaller review obligation than
    row 4, so resolving to row 3 while row 4's metrics are genuinely
    unmeasured would fail toward LESS review -- exactly the direction this
    module's negative-spec forbids. C7 briefly reordered this to fail open
    toward row 3; that reorder is reverted. An unresolved code_loc leaves
    the whole decision unresolved (asks), never falls through to row 3."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": True, "code_loc": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert "code_loc" in decision.reason


def test_unresolved_reason_names_the_unresolved_inputs():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": None, "commit_count": None, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert "code_loc" in decision.reason
    assert "commit_count" in decision.reason
    assert "surface_count" in decision.reason


# ---------------------------------------------------------------------------
# `baton_count` — 2026-08-04 sizing (`state/sizings/2026-08-04-mise-run-
# record-should-carry-baton-count.yaml`), source memo
# `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-
# mandatory-does-not-halt.md`. `None` (every existing caller's implicit
# default) must leave every row selection above byte-identical; a resolved
# `>= 2` is a MULTIPLIER on the row-4 brightline, never a forced partition,
# and FLOORS the outcome away from the no-review rows (1/2).
# shell-doc-ok: the backticked comparison above is a Python boolean
# expression, not a shell version constraint.
# ---------------------------------------------------------------------------


def test_baton_count_none_leaves_row1_unchanged():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row == 1
    assert decision.scale == "none"


def test_baton_count_none_leaves_row2_unchanged():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row == 2
    assert decision.scale == "none"


def test_baton_count_none_leaves_row4_brightline_unchanged():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 499},
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row != 4, "499 alone must not trip the brightline with no multiplier"


def test_baton_count_one_behaves_identically_to_none():
    with_none = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION, baton_count=None)
    with_one = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION, baton_count=1)
    assert with_none.row == with_one.row
    assert with_none.scale == with_one.scale


def test_baton_count_multiplier_trips_brightline_that_raw_metrics_do_not():
    # 260 * 2 = 520 >= 500 -- would NOT trip on its own.
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 260},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True
    assert "baton_count=2" in decision.reason


def test_baton_count_multiplier_does_not_force_partition_on_a_trivial_run():
    # Multiplier, not force-partition: a genuinely tiny 2-baton run must
    # still be able to land below the brightline once multiplied.
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row != 4, "a trivial 2-baton run must not be force-partitioned by the multiplier alone"


def test_baton_count_floors_row1_to_code_reviewer():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row not in (1, 2), "a resolved baton_count>=2 must never resolve to a no-review row"
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_baton_count_floors_row2_to_code_reviewer():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1},
        chain_disposition=_SINGLE_SESSION,
        baton_count=3,
    )
    assert decision.row not in (1, 2)
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert "baton_count=3" in decision.reason


def test_baton_count_does_not_alter_an_already_resolved_row3():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_baton_count_does_not_alter_chain_terminal_row6():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
        baton_count=4,
    )
    assert decision.row == 6


# ---------------------------------------------------------------------------
# --- C5 --- gross_loc/commit_count/surface_count are no longer pure
# `decisions.get()` passthrough at `brief()`'s `decide_review_scale` call
# site: `_measure_session_review_scale_inputs` backfills them from disk,
# following `backlog_grind_assemble.readers_mise`'s shipped shape.
# ---------------------------------------------------------------------------


_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run_git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, **_NO_CONSOLE)


#: The repo's own `init` commit is backdated to a fixed point well in the
#: past (rather than left at "now") so tests can anchor `session_start_time`
#: a few minutes ago and reliably exclude it from `git log --since=...` —
#: without backdating, a fast test run's `init` commit and its
#: `session_start_time` land in the same second and the inclusion/exclusion
#: becomes a coin flip.
_PRE_SESSION_COMMIT_DATE = "2000-01-01T00:00:00+00:00"

#: Commits are attributed to a session by `Session-Id` trailer, never by a
#: `--since` window over the branch — on a shared worktree a time window
#: sweeps every concurrent peer's commits into this session's measurement.
_SESSION_ID = "11111111-2222-3333-4444-555555555555"
_PEER_SESSION_ID = "99999999-8888-7777-6666-555555555555"


def _commit_as(root: Path, message: str, session_id: str) -> None:
    _run_git(["commit", "-q", "-m", f"{message}\n\nSession-Id: {session_id}"], str(root))


def _init_git_repo(root: Path) -> None:
    _run_git(["init", "-q"], str(root))
    _run_git(["config", "user.email", "t@example.com"], str(root))
    _run_git(["config", "user.name", "t"], str(root))
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(["add", "a.py"], str(root))
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": _PRE_SESSION_COMMIT_DATE,
            "GIT_COMMITTER_DATE": _PRE_SESSION_COMMIT_DATE,
        },
        **_NO_CONSOLE,
    )


def test_measure_session_review_scale_inputs_resolves_over_uncommitted_diff(tmp_path):
    """(1) A resolved range produces resolved measurements that carry
    `decide_review_scale` to a resolved, non-`jp-review-scale`-firing
    decision — this session's own working-tree diff, no commits yet."""
    _init_git_repo(tmp_path)
    (tmp_path / "new_work.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)

    gross_loc, code_loc, commit_count, surface_count = wsc._measure_session_review_scale_inputs(
        tmp_path, session_start_time, _SESSION_ID
    )
    assert commit_count == 0
    assert gross_loc is not None and gross_loc > 0
    assert surface_count == 1

    decision = decide_review_scale(
        gross_loc=gross_loc,
        code_loc=code_loc,
        commit_count=commit_count,
        surface_count=surface_count,
        executor_dispatched=False,
        shared_schema_touched=False,
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row in (1, 2)
    assert wsc.build_review_scale_judgment_point(decision) is None


def test_measure_session_review_scale_inputs_none_when_session_id_unresolved():
    """(2) An unresolvable measurement (no `session_id`, so no commit can be
    attributed to this session) still surfaces `jp-review-scale` with its
    reason — never a default verdict, never a silent single-reviewer
    fallthrough, and above all never the whole shared worktree scored as
    this session's own diff."""
    gross_loc, code_loc, commit_count, surface_count = wsc._measure_session_review_scale_inputs(
        Path("."), None, ""
    )
    assert (gross_loc, code_loc, commit_count, surface_count) == (None, None, None, None)

    decision = decide_review_scale(
        gross_loc=gross_loc,
        code_loc=code_loc,
        commit_count=commit_count,
        surface_count=surface_count,
        # C7, 2026-08-11: `executor_dispatched=None`, not True. Rows 1-3 are now
        # evaluated before row 4's metrics can block, so a known-true row-3 input
        # would resolve this decision on its own and the test would stop
        # exercising what its docstring claims. Leaving it unknown is what keeps
        # the unresolvable-MEASUREMENT case the thing under test.
        executor_dispatched=None,
        shared_schema_touched=False,
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False

    jp = wsc.build_review_scale_judgment_point(decision)
    assert jp is not None
    assert "review-scale" in jp["id"]
    assert decision.reason in jp.get("evidence", "") or decision.reason


def test_measure_session_review_scale_inputs_counts_committed_and_uncommitted_together():
    """Both halves of this session's change set are counted: its own landed
    commits (summed per-commit, never over a `base..HEAD` range that would
    span interleaved peer commits) AND its still-uncommitted files. Step 6
    asks before `d-run-wsc-tail` commits, so undercounting the uncommitted
    half would reproduce the exact defect this chunk fixes — and untracked
    files must count, since a session whose whole output is new `state/`
    artifacts is invisible to `git diff`."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        (root / "b.py").write_text("z = 3\n", encoding="utf-8")
        _run_git(["add", "b.py"], str(root))
        _commit_as(root, "session commit", _SESSION_ID)
        (root / "c.py").write_text("w = 4\n", encoding="utf-8")

        # (review-integrator finding 3) `uncommitted_paths` is passed
        # explicitly, fixture-derived, rather than left to the default
        # `classify_session_authored_files` derivation — that heuristic is
        # itself under test elsewhere, and letting this assertion depend on
        # it couples `gross_loc == 2` to a module this test does not guard.
        gross_loc, code_loc, commit_count, surface_count = wsc._measure_session_review_scale_inputs(
            root, session_start_time, _SESSION_ID, uncommitted_paths=["c.py"]
        )
        assert commit_count == 1
        assert gross_loc == 2
        assert code_loc == 2
        assert surface_count is not None and surface_count >= 1


def test_measure_session_review_scale_inputs_excludes_peer_work_on_a_shared_worktree():
    """The regression guard for bug `2026-08-10-workstream-complete-measures-
    review-scal-a52c3f9d55d2`: a concurrent peer's commits and its dirty
    files must not land in THIS session's brightline inputs. Measured live,
    the unguarded form scored one session's 96-line diff at 1775 LOC across
    5 surfaces of peers' in-flight work, forcing a spurious
    PARTITION-MANDATORY and inviting a review attestation over changes the
    closing session never authored."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        (root / "peer_committed.py").write_text("\n".join(f"p{i} = {i}" for i in range(40)) + "\n", encoding="utf-8")
        _run_git(["add", "peer_committed.py"], str(root))
        _commit_as(root, "peer commit", _PEER_SESSION_ID)

        (root / "mine.py").write_text("m = 1\n", encoding="utf-8")
        _run_git(["add", "mine.py"], str(root))
        _commit_as(root, "my commit", _SESSION_ID)

        gross_loc, code_loc, commit_count, surface_count = wsc._measure_session_review_scale_inputs(
            root, session_start_time, _SESSION_ID, uncommitted_paths=[]
        )
        assert commit_count == 1
        assert gross_loc == 1
        assert code_loc == 1

        peer_loc, peer_code_loc, peer_commits, _ = wsc._measure_session_review_scale_inputs(
            root, session_start_time, _PEER_SESSION_ID, uncommitted_paths=[]
        )
        assert peer_commits == 1
        assert peer_loc == 40
        assert peer_code_loc == 40


# ---------------------------------------------------------------------------
# (review-integrator, slice B) `_split_tracked`/`_count_lines` direct unit
# coverage, and the `None`-propagation contract slice A introduced: a git or
# file-read failure inside either helper must surface as the FULL
# `(None, None, None, None)` quadruple from
# `_measure_session_review_scale_inputs` (C5, 2026-08-11: extended to
# include `code_loc`), never a partially-populated or zeroed one standing
# in for a failure.
# ---------------------------------------------------------------------------


def test_split_tracked_partitions_mixed_tracked_and_untracked_with_normalization():
    """Direct unit test of `_split_tracked`'s happy path: a mixed
    tracked+untracked list partitions correctly, and a Windows-style
    backslash path is normalized (`git ls-files` itself emits forward
    slashes) before the tracked/untracked comparison."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        (root / "sub").mkdir()
        (root / "sub" / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        _run_git(["add", "sub/tracked.py"], str(root))
        _commit_as(root, "add tracked", _SESSION_ID)
        (root / "untracked.py").write_text("y = 1\n", encoding="utf-8")

        tracked, untracked = wsc._split_tracked(root, ["sub\\tracked.py", "untracked.py"])
        assert tracked == ["sub\\tracked.py"]
        assert untracked == ["untracked.py"]


def test_count_lines_unreadable_path_returns_none(tmp_path):
    """(review-integrator finding 1, half 2) `_count_lines` on a path that
    does not exist on disk returns `None`, never a zero standing in for the
    unreadable file."""
    missing = tmp_path / "does_not_exist.py"
    assert wsc._count_lines(missing) is None


def test_measure_session_review_scale_inputs_propagates_none_on_split_tracked_git_failure():
    """(review-integrator finding 1, half 1 -- slice A P1 regression guard)
    A `git ls-files` failure inside `_split_tracked` must propagate as the
    FULL `(None, None, None)` triple from `_measure_session_review_scale_
    inputs` -- the pre-fix behaviour this pins against silently fell back to
    `(list(paths), [])`, scoring the untracked share of `paths` at zero LOC
    while still returning a real, resolved-looking triple."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        real_run_git_read_only = wsc._run_git_read_only

        def _fail_ls_files(args, cwd):
            if args and args[0] == "ls-files":
                return None
            return real_run_git_read_only(args, cwd)

        original = wsc._run_git_read_only
        wsc._run_git_read_only = _fail_ls_files
        try:
            result = wsc._measure_session_review_scale_inputs(
                root, session_start_time, _SESSION_ID, uncommitted_paths=["ghost.py"]
            )
        finally:
            wsc._run_git_read_only = original

        assert result == (None, None, None, None)


def test_measure_session_review_scale_inputs_propagates_none_on_unreadable_untracked_file():
    """(review-integrator finding 1, half 2) An untracked path that
    `_count_lines` cannot read (here: it does not exist on disk, e.g. a race
    with a peer's write or a staged-for-deletion path) must propagate as the
    FULL `(None, None, None)` triple, never a triple with the unreadable
    file's contribution silently scored as zero."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)
        session_start_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        result = wsc._measure_session_review_scale_inputs(
            root, session_start_time, _SESSION_ID, uncommitted_paths=["missing_untracked.py"]
        )
        assert result == (None, None, None, None)


def test_measure_session_review_scale_inputs_no_owned_commits_and_none_session_start_time():
    """(review-integrator finding 4) `session_id` resolved but owning zero
    commits, combined with `session_start_time=None`, must NOT raise --
    `classify_session_authored_files` handles a `None` start time
    explicitly, degrading every non-keep-listed, non-known-concurrent file
    to `session_authored: False` rather than raising. With no dirty files in
    the fixture, that degrades to an empty uncommitted set, so this is a
    genuinely successful measurement of an empty change set: `(0, 0, 0)`,
    not a `None` triple."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_git_repo(root)

        result = wsc._measure_session_review_scale_inputs(root, None, _SESSION_ID)
        assert result == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# (3) Structural — no threshold constant is redeclared anywhere in this
# package. `_BRIGHTLINE_LOC`/`_BRIGHTLINE_COMMITS`/`_BRIGHTLINE_SURFACES`
# are declared exactly once, in `directives_review.py` — this chunk's own
# `_measure_session_review_scale_inputs` supplies MEASUREMENTS and calls
# `decide_review_scale`; it never redeclares or re-derives the predicate.
# ---------------------------------------------------------------------------

_THRESHOLD_CONSTANT_NAMES = ("_BRIGHTLINE_LOC", "_BRIGHTLINE_COMMITS", "_BRIGHTLINE_SURFACES")


def test_no_threshold_constant_redeclared_anywhere_in_package():
    package_dir = Path(directives_review.__file__).parent
    for name in _THRESHOLD_CONSTANT_NAMES:
        declaring_files = []
        for py_file in package_dir.glob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            if f"\n{name} = " in text or text.startswith(f"{name} = "):
                declaring_files.append(py_file.name)
        assert declaring_files == ["directives_review.py"], (
            f"{name} must be declared exactly once, in directives_review.py; found in {declaring_files}"
        )


# ---------------------------------------------------------------------------
# (4) AC9 regression guard — completion-nature-classification remains a
# judgment point after this chunk's decisions-resolution change, and no
# in-op subagent dispatch is introduced anywhere on this path.
# ---------------------------------------------------------------------------


def test_completion_nature_classification_judgment_point_still_present():
    jp = judgments.build_completion_nature_classification_judgment_point()
    assert jp["id"] == "completion-nature-classification"


def test_no_subagent_dispatch_introduced_on_the_review_scale_measurement_path():
    source = Path(wsc.__file__).read_text(encoding="utf-8")
    forbidden_tokens = ("dispatch_subagent", "Task(", "claude_agent_sdk", "spawn_agent")
    for token in forbidden_tokens:
        assert token not in source, f"{token!r} must not appear on the review-scale measurement path"


# ---------------------------------------------------------------------------
# --- C1 (docs/plans/2026-08-08-the-second-close-re-measures-the-first-c.md)
# --- build_review_brightline_gate_directive range-floor coverage.
#
# The defect: the mid-chain brightline directive emitted `["--session-id",
# session_id]` with NO range, so the gate fell back to its own default
# (`merge-base(origin/main, HEAD)..HEAD`) and re-measured every commit a
# prior close in the SAME session already reviewed. AC5's fixture (multiple
# trail records in one session) below reproduces that live failure and must
# FAIL against pre-fix code (a builder that ignores `trail_records` entirely
# would never floor the range and this test would see the un-floored
# two-element argv instead of a three-element, floored one).
#
# AC2 is the regression that matters most (nearly every close): with no
# trail records supplied — the ordinary single-close path — the emitted
# argv must stay byte-identical to today's `["--session-id", session_id]`.
# That case is tested FIRST, per the dispatch brief.
# ---------------------------------------------------------------------------

from coordinator_core.workstream_complete.directives_review import (
    build_review_brightline_gate_directive,
    resolve_mid_chain_review_scope,
)


def test_no_prior_trail_record_argv_is_byte_identical_to_today():
    """AC2 — the ordinary single-close path (no trail records at all, the
    shape `workstream_complete/__init__.py`'s current call site still uses)
    must emit the exact same two-element argv as before this chunk."""
    directive = build_review_brightline_gate_directive("sess-abc123")
    assert directive["args"] == ["--session-id", "sess-abc123"]
    assert directive["cli"] == "review-brightline-gate"


def test_partial_floor_kwargs_also_falls_back_to_todays_argv():
    """A caller supplying only SOME of the four floor kwargs (not all four)
    must not partially floor the range — the builder requires every one of
    `trail_records`/`chain_tip_sha`/`is_ancestor`/`session_start_sha` before
    it will compute anything, else it degrades to today's argv exactly as
    the no-kwargs case does."""
    directive = build_review_brightline_gate_directive(
        "sess-abc123", chain_tip_sha="deadbeef", session_start_sha="cafef00d"
    )
    assert directive["args"] == ["--session-id", "sess-abc123"]


def test_prior_trail_record_floors_the_range_ac1():
    """AC1 — a session with a prior review-trail record is invoked over a
    range floored at the last-reviewed sha, not the session's start."""
    directive = build_review_brightline_gate_directive(
        "sess-abc123",
        trail_records=[{"sha_range_head": "prior00sha"}],
        chain_tip_sha="tip0000sha",
        is_ancestor=lambda head, tip: True,
        session_start_sha="start00sha",
    )
    assert directive["args"] == ["--session-id", "sess-abc123", "prior00sha..tip0000sha"]


def test_reproduces_the_live_second_close_re_measures_the_first_failure_ac5():
    """AC5 — two trail records in one session; the second close's directive
    must scope to the commits AFTER the first close, i.e. floor at the
    SECOND (most recent qualifying) record's head, never the session start
    and never the first record's head. This is the exact scenario the
    plan's Problem section observed live (a second close re-measuring the
    first close's already-reviewed diff) and must FAIL against pre-fix
    code, which ignores trail_records and always emits the unfloored
    two-element argv."""
    trail_records = [
        {"sha_range_head": "first0closehead"},
        {"sha_range_head": "second0closehead"},
    ]
    directive = build_review_brightline_gate_directive(
        "sess-644e4f3b",
        trail_records=trail_records,
        chain_tip_sha="chaintip",
        is_ancestor=lambda head, tip: True,
        session_start_sha="sessionstart",
    )
    assert directive["args"] == [
        "--session-id",
        "sess-644e4f3b",
        "second0closehead..chaintip",
    ]
    # Never the un-floored default (the pre-fix defect), never the first
    # close's own head, and never the session start.
    assert directive["args"] != ["--session-id", "sess-644e4f3b"]
    assert "first0closehead" not in directive["args"][-1]
    assert "sessionstart" not in directive["args"][-1]


def test_floor_computation_delegates_to_resolve_mid_chain_review_scope_not_reimplemented():
    """The builder must produce the SAME floor `resolve_mid_chain_review_scope`
    itself resolves for the identical inputs — pinning that the builder
    delegates rather than reimplementing the resolution algorithm (the
    dispatch brief's explicit "do not reimplement it" instruction)."""
    trail_records = [{"sha_range_head": "r1"}, {"sha_range_head": "r2"}]
    is_ancestor = lambda head, tip: head in ("r1", "r2")
    expected_floor = resolve_mid_chain_review_scope(trail_records, "tip", is_ancestor, "start")
    directive = build_review_brightline_gate_directive(
        "sess-x",
        trail_records=trail_records,
        chain_tip_sha="tip",
        is_ancestor=is_ancestor,
        session_start_sha="start",
    )
    assert directive["args"][-1] == f"{expected_floor}..tip"


def test_docstring_states_the_new_range_scoping_ac6():
    """AC6 — the docstring must state the new scoping rather than staying
    silent on the range (the pre-fix docstring argued only for
    `--session-id`'s mandatoriness and said nothing about a range floor)."""
    doc = build_review_brightline_gate_directive.__doc__ or ""
    assert "range" in doc.lower()
    assert "floor" in doc.lower()
    assert "resolve_mid_chain_review_scope" in doc


def test_session_id_never_dropped_even_when_a_floor_is_supplied():
    """Anti-scope — `--session-id` is additive-never-replaced: the floor
    bounds WHERE to look, the trailer still decides WHAT counts."""
    directive = build_review_brightline_gate_directive(
        "sess-abc123",
        trail_records=[{"sha_range_head": "prior00sha"}],
        chain_tip_sha="tip0000sha",
        is_ancestor=lambda head, tip: True,
        session_start_sha="start00sha",
    )
    assert directive["args"][0] == "--session-id"
    assert directive["args"][1] == "sess-abc123"


# ---------------------------------------------------------------------------
# AC4 — composition with `aff5b6efd` (the session-aware floor retry inside
# `coordinator_core.ops.review_brightline_gate`) proven by RUNNING the real
# gate over the directive's emitted argv, not by reasoning about it. Two
# legs: (1) a caller-supplied floor that yields zero trailer matches must
# still reach the gate's own session-aware floor retry and recover, and (2)
# a genuinely-empty session (no commits anywhere) must still resolve
# `VERDICT=indeterminate`.
# ---------------------------------------------------------------------------

import contextlib

from coordinator_core.ops import review_brightline_gate

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


def _commit(root: Path, filename: str, content: str, message: str) -> str:
    (root / filename).write_text(content, encoding="utf-8")
    _run_git(["add", filename], str(root))
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_DATE": _PRE_SESSION_COMMIT_DATE, "GIT_COMMITTER_DATE": _PRE_SESSION_COMMIT_DATE},
        **_NO_CONSOLE,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True, **_NO_CONSOLE
    )
    return out.stdout.strip()


@contextlib.contextmanager
def _chdir(path: Path):
    prior = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(prior)


def test_ac4_zero_trailer_match_floor_still_reaches_the_session_aware_floor_retry(tmp_path, capsys):
    _init_git_repo(tmp_path)
    c1 = _commit(tmp_path, "b.py", "b = 1\n", "session commit\n\nSession-Id: sess-retry\n")
    c2 = _commit(tmp_path, "c.py", "c = 1\n", "unrelated noise commit")
    c3 = _commit(tmp_path, "d.py", "d = 1\n", "more unrelated noise")

    # Caller-supplied floor deliberately excludes c1 (the only commit
    # carrying the trailer) -- the directive's own range yields zero
    # trailer matches, so the real gate must fall through to its own
    # unbounded session-aware floor retry to recover c1.
    directive = build_review_brightline_gate_directive(
        "sess-retry",
        trail_records=[{"sha_range_head": c2}],
        chain_tip_sha=c3,
        is_ancestor=lambda head, tip: True,
        session_start_sha=c2,
    )
    assert directive["args"] == ["--session-id", "sess-retry", f"{c2}..{c3}"]

    with _chdir(tmp_path):
        rc = review_brightline_gate.main(directive["args"])
    out = capsys.readouterr()
    assert rc == 0
    assert "recovered via session-aware floor" in out.err
    assert "VERDICT=" in out.out
    assert "VERDICT=indeterminate" not in out.out


def test_ac4_genuinely_empty_session_still_resolves_indeterminate(tmp_path, capsys):
    _init_git_repo(tmp_path)
    c1 = _commit(tmp_path, "b.py", "b = 1\n", "session commit\n\nSession-Id: sess-real\n")

    directive = build_review_brightline_gate_directive(
        "sess-nothing-here",
        trail_records=[{"sha_range_head": c1}],
        chain_tip_sha=c1,
        is_ancestor=lambda head, tip: True,
        session_start_sha=c1,
    )

    with _chdir(tmp_path):
        rc = review_brightline_gate.main(directive["args"])
    out = capsys.readouterr()
    assert rc == 0
    assert "VERDICT=indeterminate" in out.out
