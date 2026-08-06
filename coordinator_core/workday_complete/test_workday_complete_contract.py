"""
coordinator_core.workday_complete.test_workday_complete_contract — per-field
CONSUMES_MANIFEST <-> emitted-directives conformance test for the
`workday-complete` computed-skill engine (AC10, docs/plans/2026-07-24-b1-
ceremony-complete-computed-conversion.md).

Purpose: closes the AC10 gap the 2026-07-25 plan sweep found — the
consumes-manifest half was discharged (`brief.py`'s own module docstring
enumerates it), but no test asserted the manifest and the actually-emitted
`directives[].cli` values stay in lockstep. Drift in either direction is a
real defect this guards against: a directive naming a CLI absent from
CONSUMES_MANIFEST is a phantom verb reaching the apply half unlisted (the
AC15c fail-open-silently trap); a CONSUMES_MANIFEST row no directive ever
names is a dead census entry nobody exercises, silently misleading a future
reader of the manifest into thinking it's load-bearing.

Each manifest row gets its OWN assertion, one per field — no catch-all
`set(...) == set(...)` blob comparison — per AC10's own text ("a per-field
row for every field ... no catch-all gates.* row"): a single dropped or
orphaned CLI name fails on its own line, not folded into one aggregate diff
that hides which entry actually drifted.

This test is NOT vacuous — the FIRST version of this file, run against
disk BEFORE this same commit's fix, failed red: `workday-complete-
step2_5-dirty-tree` was a CONSUMES_MANIFEST row with zero directives ever
naming it (the Step 2.5 auto-disposition script was never wired into
`_build_directives`, and `jp_step2_5_dirty_tree_ambiguous` was emitted
unconditionally every run, permanently gating `d_step3_consolidate` behind
an EM ask even on a clean tree). Fixed in this same commit by adding
`d_step2_5_dirty_tree_scan` and making the judgment point + its gate
conditional on a real `--dry-run` probe (`_compute_dirty_tree_verdict`) —
see `brief.py`'s "AC10 fix" comment block.

Run scoped only:
    python3 -m pytest coordinator_core/workday_complete/test_workday_complete_contract.py -q
Spec backlink: docs/plans/2026-07-24-b1-ceremony-complete-computed-conversion.md § AC10
"""

from __future__ import annotations

import pytest

from coordinator_core.workday_complete import brief as wc_brief

# Documented consumes-manifest members that are never a `directives[].cli`
# value because they are invoked by a DISPATCHED WORKER rather than the
# assembler itself (module docstring's negative-spec, third bullet) — a
# directive is an apply-half instruction, not a sub-dispatch's tool belt.
_DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS = frozenset({"coordinator-queue-append"})

_EMPTY_OPEN_DAY_GOALS = {"today": [], "stale": [], "unreadable_error": None}
_NONEMPTY_OPEN_DAY_GOALS = {
    "today": [{"goal_id": "g_today", "text": "ship the thing"}],
    "stale": [{"goal_id": "g_stale", "text": "stale thing"}],
    "unreadable_error": None,
}
_CLEAN_TREE = {"ambiguous": False, "evidence": "synthetic: clean tree"}
_DIRTY_TREE = {"ambiguous": True, "evidence": "synthetic: ambiguous paths remain"}


def _all_emittable_directive_clis() -> set[str]:
    """Union of every `cli` value `_build_directives` can emit across every
    combination of its two conditional axes (open-day-goals present/absent;
    dirty-tree ambiguous/clean). Each conditional directive
    (`d_goal_close_day`, gated on the day-goal axis) only appears on one
    side of its own axis — a single combined call would under-report the
    true emission surface and make a live-but-conditional CLI name look
    like a dead manifest entry."""
    clis: set[str] = set()
    for open_day_goals in (_EMPTY_OPEN_DAY_GOALS, _NONEMPTY_OPEN_DAY_GOALS):
        for dirty_tree_verdict in (_CLEAN_TREE, _DIRTY_TREE):
            directives = wc_brief._build_directives(
                {}, open_day_goals, dirty_tree_verdict
            )
            clis.update(d["cli"] for d in directives)
    return clis


def test_manifest_has_no_duplicate_entries() -> None:
    manifest = wc_brief.CONSUMES_MANIFEST
    assert len(manifest) == len(set(manifest)), (
        f"CONSUMES_MANIFEST carries a duplicate entry: {manifest!r}"
    )


def test_every_emitted_directive_cli_is_a_manifest_member() -> None:
    """No directive names a CLI CONSUMES_MANIFEST doesn't enumerate."""
    manifest = set(wc_brief.CONSUMES_MANIFEST)
    for cli in sorted(_all_emittable_directive_clis()):
        assert cli in manifest, (
            f"directive emits CLI {cli!r}, which is absent from CONSUMES_MANIFEST "
            "-- a phantom verb reaching the apply half unlisted (AC15c)"
        )


def test_every_manifest_entry_is_named_by_at_least_one_directive() -> None:
    """No CONSUMES_MANIFEST row is a dead census entry no directive
    exercises, UNLESS it's a documented dispatched-worker-only member (one
    assertion per manifest field either way, per AC10's own text -- no
    blanket exemption, each named exception gets checked by name)."""
    emitted = _all_emittable_directive_clis()
    for cli in wc_brief.CONSUMES_MANIFEST:
        if cli in _DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS:
            assert cli not in emitted, (
                f"{cli!r} is documented as dispatched-worker-only but now "
                "also appears as a directives[] cli -- update the negative-"
                "spec/exception set, this is no longer purely worker-invoked"
            )
            continue
        assert cli in emitted, (
            f"CONSUMES_MANIFEST names {cli!r}, but no directive ever emits it "
            "-- either a dead census row or a directive silently dropped"
        )


def test_goal_close_day_only_emitted_when_open_rows_present() -> None:
    """Regression guard for C4's conditional-emission rule (module
    docstring): `goal-close-day` must not appear when there are zero open
    day-goal rows, and must appear when there is at least one."""
    absent_clis = {
        d["cli"]
        for d in wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    }
    present_clis = {
        d["cli"]
        for d in wc_brief._build_directives({}, _NONEMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    }
    assert "goal-close-day" not in absent_clis
    assert "goal-close-day" in present_clis


def test_dirty_tree_scan_directive_always_present_but_gate_is_conditional() -> None:
    """`d_step2_5_dirty_tree_scan` (the auto-disposition run) fires every
    time regardless of verdict; `d_step3_consolidate`'s `depends_on` is
    None on a clean tree and the judgment-point id on an ambiguous one —
    the AC10 fix this file's own docstring narrates."""
    clean = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    dirty = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    clean_by_id = {d["id"]: d for d in clean}
    dirty_by_id = {d["id"]: d for d in dirty}

    assert "d_step2_5_dirty_tree_scan" in clean_by_id
    assert "d_step2_5_dirty_tree_scan" in dirty_by_id
    assert clean_by_id["d_step2_5_dirty_tree_scan"]["depends_on"] is None

    assert clean_by_id["d_step3_consolidate"]["depends_on"] is None
    assert (
        dirty_by_id["d_step3_consolidate"]["depends_on"]
        == "jp_step2_5_dirty_tree_ambiguous"
    )


def test_dirty_tree_judgment_point_conditional_on_verdict() -> None:
    """`jp_step2_5_dirty_tree_ambiguous` is absent on a clean-tree verdict
    and present, carrying the real probe evidence, on an ambiguous one."""
    clean_points = wc_brief._build_judgment_points(_EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    dirty_points = wc_brief._build_judgment_points(_EMPTY_OPEN_DAY_GOALS, _DIRTY_TREE)
    clean_ids = {p["id"] for p in clean_points}
    dirty_by_id = {p["id"]: p for p in dirty_points}

    assert "jp_step2_5_dirty_tree_ambiguous" not in clean_ids
    assert "jp_step2_5_dirty_tree_ambiguous" in dirty_by_id
    assert (
        dirty_by_id["jp_step2_5_dirty_tree_ambiguous"]["evidence"]
        == _DIRTY_TREE["evidence"]
    )


def test_backfill_anchor_directive_carries_root_positional() -> None:
    """Regression guard: `workday-complete-backfill-anchor run` declares a
    REQUIRED `root` positional (its `_cmd_run` argparse parser) -- omitting
    it always exited 2 (`error: the following arguments are required:
    root`), so `d_step3_5_backfill_anchor_a0` never actually ran Phase-A0
    backfill anchoring. Asserts the directive's `args` carry a second
    element (the resolved root) beyond the `run` subcommand token, and that
    it is a non-empty string -- this test fails red against the pre-fix
    `args=["run"]` shape and green once `_main_worktree_root_for_directive()`
    supplies the positional."""
    directives = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    by_id = {d["id"]: d for d in directives}
    anchor = by_id["d_step3_5_backfill_anchor_a0"]
    assert anchor["cli"] == "workday-complete-backfill-anchor"
    assert anchor["args"][0] == "run"
    assert len(anchor["args"]) >= 2, (
        "d_step3_5_backfill_anchor_a0 must pass a root positional after "
        f"'run' -- got args={anchor['args']!r}, which reproduces the "
        "'error: the following arguments are required: root' exit-2 defect"
    )
    assert isinstance(anchor["args"][1], str) and anchor["args"][1], (
        f"root positional must be a non-empty str, got {anchor['args'][1]!r}"
    )


def test_backfill_anchor_and_phase_b_declare_scan_as_stdin_producer() -> None:
    """Regression guard (2026-07-26 stdin-wiring fix): both
    `d_step3_5_backfill_anchor_a0` and `d_step3_5_backfill_phase_b` read the
    scan's gap-row TSV on stdin (`workday-complete-backfill-anchor run` and
    `workday-complete-close backfill-dispatch-rows`, both via
    `sys.stdin.read()`), so both MUST declare `stdin_from=
    "d_step3_5_backfill_scan"` -- the one field `apply._execute_directives`
    reads to wire the pipe. Every OTHER directive in this leg must NOT
    declare a `stdin_from` it doesn't need (the scan itself is the
    producer, not a consumer)."""
    directives = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    by_id = {d["id"]: d for d in directives}

    assert by_id["d_step3_5_backfill_anchor_a0"]["stdin_from"] == "d_step3_5_backfill_scan"
    assert by_id["d_step3_5_backfill_phase_b"]["stdin_from"] == "d_step3_5_backfill_scan"
    assert by_id["d_step3_5_backfill_scan"]["stdin_from"] is None


def test_phase_b_default_call_unchanged() -> None:
    """`for_date`/`only_mode` default to `None`/`False` and must reproduce
    today's exact `d_step3_5_backfill_phase_b` args -- no flag threaded at
    all -- when omitted entirely (both the two-positional-arg call used
    throughout this file's other tests, AND a call with the keywords
    explicitly passed as their own defaults)."""
    directives = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == ["backfill-dispatch-rows"]

    directives_explicit = wc_brief._build_directives(
        {}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE, for_date=None, only_mode=False
    )
    by_id_explicit = {d["id"]: d for d in directives_explicit}
    assert by_id_explicit["d_step3_5_backfill_phase_b"]["args"] == ["backfill-dispatch-rows"]


def test_phase_b_for_date_alone_threads_for_date_flag() -> None:
    """`for_date` set, `only_mode` left at its default `False`: args gain
    `--for-date <date>` but NOT `--only-mode` -- Phase B still processes
    every gap row, only the matching row gets `--for-date`-driven
    SCOPE_SUMMARY forwarding downstream (workday-complete-close.py's own
    concern, not this module's)."""
    directives = wc_brief._build_directives(
        {}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE, for_date="2026-07-20"
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == [
        "backfill-dispatch-rows",
        "--for-date",
        "2026-07-20",
    ]


def test_phase_b_for_date_and_only_mode_threads_both_flags() -> None:
    """`for_date` + `only_mode=True`: args carry BOTH `--for-date <date>`
    and `--only-mode`, matching `workday-complete-close.py`'s real
    `backfill-dispatch-rows` flag spelling exactly."""
    directives = wc_brief._build_directives(
        {}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE, for_date="2026-07-20", only_mode=True
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == [
        "backfill-dispatch-rows",
        "--for-date",
        "2026-07-20",
        "--only-mode",
    ]


def test_phase_b_only_mode_without_for_date_is_a_noop() -> None:
    """`only_mode=True` with NO `for_date`: `--only-mode` must NOT be
    threaded alone -- `cmd_backfill_dispatch_rows`'s own
    `date != args.for_date` guard would compare every row against `None`
    and silently skip the entire backfill, a worse defect than the one
    this whole task fixes."""
    directives = wc_brief._build_directives(
        {}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE, only_mode=True
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == ["backfill-dispatch-rows"]


def test_scope_summary_default_leaves_both_directives_unchanged() -> None:
    """`scope_summary` defaults to `None` and must reproduce today's exact
    `args` for BOTH `d_step9_changelog` and `d_step3_5_backfill_phase_b`
    when omitted -- no `--`/positional/flag threaded at all."""
    directives = wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step9_changelog"]["args"] == ["step9-dispatch"]
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == ["backfill-dispatch-rows"]


def test_scope_summary_alone_reaches_step9_changelog_positionally() -> None:
    """`scope_summary` with no `for_date` reaches `d_step9_changelog` as a
    `--`-separated bare positional (the default, non-backfill route) and
    does NOT reach `d_step3_5_backfill_phase_b` at all -- there is no
    `for_date`-matched row for it to apply to on that leg."""
    directives = wc_brief._build_directives(
        {}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE, scope_summary="shipped the thing"
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step9_changelog"]["args"] == [
        "step9-dispatch",
        "--",
        "shipped the thing",
    ]
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == ["backfill-dispatch-rows"]


def test_scope_summary_and_for_date_reach_both_directives() -> None:
    """`scope_summary` + `for_date` together: `d_step3_5_backfill_phase_b`
    gets `--for-date <date>` plus the single-token `--scope-summary=<value>`
    eq-form flag, AND `d_step9_changelog` still gets the `--`-separated bare
    positional -- both directives carry the prose simultaneously."""
    directives = wc_brief._build_directives(
        {},
        _EMPTY_OPEN_DAY_GOALS,
        _CLEAN_TREE,
        for_date="2026-07-20",
        scope_summary="shipped the thing",
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step9_changelog"]["args"] == [
        "step9-dispatch",
        "--",
        "shipped the thing",
    ]
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == [
        "backfill-dispatch-rows",
        "--for-date",
        "2026-07-20",
        "--scope-summary=shipped the thing",
    ]


def test_only_mode_threads_only_mode_into_step9_changelog() -> None:
    """`only_mode` must reach `d_step9_changelog` as `--only-mode`, not just
    the backfill leg.

    `cmd_step9_dispatch` skips itself entirely under `--only-mode` ("the
    targeted block was already committed via Step 3.5 Phase B") and without
    the flag it never learns to. Omitting it made a targeted wrap write a
    today-scoped changelog block alongside the backfilled one (2026-07-28:
    a `--for-date 2026-07-27 --only` run produced both a 2026-07-27 block
    and a spurious 2026-07-28 one), and once `scope_summary` is threaded it
    additionally misattributes the user's `$FOR_DATE` prose onto today.

    Flag order is load-bearing: `--only-mode` must precede the `--`
    end-of-options separator, or argparse reads it as a positional.
    """
    by_id = {
        d["id"]: d
        for d in wc_brief._build_directives(
            {},
            _EMPTY_OPEN_DAY_GOALS,
            _CLEAN_TREE,
            for_date="2026-07-20",
            only_mode=True,
            scope_summary="targeted note",
        )
    }
    assert by_id["d_step9_changelog"]["args"] == [
        "step9-dispatch",
        "--only-mode",
        "--",
        "targeted note",
    ]

    # ...and without prose, the flag still lands on its own.
    by_id_no_prose = {
        d["id"]: d
        for d in wc_brief._build_directives(
            {},
            _EMPTY_OPEN_DAY_GOALS,
            _CLEAN_TREE,
            for_date="2026-07-20",
            only_mode=True,
        )
    }
    assert by_id_no_prose["d_step9_changelog"]["args"] == [
        "step9-dispatch",
        "--only-mode",
    ]

    # Default route must stay clean — no stray --only-mode.
    by_id_default = {
        d["id"]: d
        for d in wc_brief._build_directives({}, _EMPTY_OPEN_DAY_GOALS, _CLEAN_TREE)
    }
    assert by_id_default["d_step9_changelog"]["args"] == ["step9-dispatch"]


def test_scope_summary_leading_dash_survives_intact() -> None:
    """A `scope_summary` beginning with `-` survives verbatim on BOTH
    directives -- the `--` separator on the positional leg and the
    single-token eq-form on the flag leg each defend against argparse
    misclassifying the value as a new option, without stripping or
    mangling the user's prose."""
    directives = wc_brief._build_directives(
        {},
        _EMPTY_OPEN_DAY_GOALS,
        _CLEAN_TREE,
        for_date="2026-07-20",
        scope_summary="-- wrapped the refactor",
    )
    by_id = {d["id"]: d for d in directives}
    assert by_id["d_step9_changelog"]["args"] == [
        "step9-dispatch",
        "--",
        "-- wrapped the refactor",
    ]
    assert by_id["d_step3_5_backfill_phase_b"]["args"] == [
        "backfill-dispatch-rows",
        "--for-date",
        "2026-07-20",
        "--scope-summary=-- wrapped the refactor",
    ]


def test_dirty_tree_verdict_probe_argv_unchanged_by_c6_claim_awareness(monkeypatch) -> None:
    """C6 (docs/plans/2026-08-05-in-process-writers-declare-their-writes.md)
    decided the Step 2.5 script obtains its session id via
    `coordinator_core.session.core.resolve_session_id`, in-process, rather
    than growing `main()` a session-id parameter/flag -- specifically so
    this repo's own in-process caller
    (`_compute_dirty_tree_verdict`) never needs to change its own call
    shape. Regression guard for that decision: this must keep calling
    `_step2_5_dirty_tree_main` with EXACTLY `["--dry-run"]`, no session
    argument threaded through, byte-identical to pre-C6."""
    captured: dict[str, object] = {}

    def _fake_step2_5_main(argv):
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(wc_brief, "_step2_5_dirty_tree_main", _fake_step2_5_main)
    verdict = wc_brief._compute_dirty_tree_verdict()
    assert captured["argv"] == ["--dry-run"]
    assert verdict["ambiguous"] is False


@pytest.mark.real_home
def test_brief_rejects_malformed_for_date() -> None:
    """A malformed `for_date` fails loud with `WorkdayExitCode.USAGE` and a
    clear `error` key -- never silently threaded through to build a
    directive with a bad date string. Runs before the `real_home`-gated
    `resolve_operator_config` call, so this assertion holds regardless of
    the suite-root HOME quarantine."""
    exit_code, envelope = wc_brief.brief(for_date="not-a-date")
    assert exit_code == int(wc_brief.WorkdayExitCode.USAGE)
    assert "error" in envelope
    assert "--for-date" in envelope["error"]

    exit_code2, envelope2 = wc_brief.brief(for_date="2026-13-40")
    assert exit_code2 == int(wc_brief.WorkdayExitCode.USAGE)
    assert "error" in envelope2


@pytest.mark.real_home
def test_brief_envelope_preflight_consumes_manifest_matches_module_constant() -> None:
    """The 8-key envelope's `preflight.consumes_manifest` field (what the
    surface actually announces it consumes) must be byte-identical to the
    module's own `CONSUMES_MANIFEST` constant -- catches a `brief()` author
    hand-copying a stale list into the envelope instead of deriving it from
    the one source of truth.

    `real_home`: `brief()` calls `resolve_operator_config` +
    `_compute_open_day_goals`/`_compute_dirty_tree_verdict`, which resolve
    the real machine-local registry and the invoking repo's actual git
    common dir -- under the suite-root HOME quarantine (`conftest.py`) those
    resolve against a throwaway tmpdir with no registry, so `brief()` hits
    its outer never-fail-the-ceremony backstop and returns a bare
    `{"error": ...}` envelope with no `preflight` key at all, which is a
    quarantine artifact, not a real assembler defect. This is a live-tree
    parity oracle by design (AC10: does the envelope really carry what the
    module claims it consumes) -- read-only, never fails destructively."""
    _, envelope = wc_brief.brief()
    assert envelope["preflight"]["consumes_manifest"] == list(
        wc_brief.CONSUMES_MANIFEST
    )


def test_main_threads_for_date_and_only_flags_into_brief(monkeypatch, capsys) -> None:
    """`main(argv)`'s `--for-date`/`--only` argparse flags must reach
    `brief()` as its own-named kwargs, unchanged in spelling
    (`for_date`/`only_mode`) -- stubs `brief()` so this stays a pure
    argv-plumbing test, independent of `real_home`/disk state."""
    captured: dict[str, object] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False, scope_summary=None):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        captured["scope_summary"] = scope_summary
        return 0, {"ok": True}

    monkeypatch.setattr(wc_brief, "brief", _fake_brief)
    rc = wc_brief.main(["--for-date", "2026-07-20", "--only"])
    assert rc == 0
    assert captured == {"for_date": "2026-07-20", "only_mode": True, "scope_summary": None}
    capsys.readouterr()


def test_main_no_flags_calls_brief_with_defaults(monkeypatch, capsys) -> None:
    """Bare `workday-complete-assemble brief` (no argv) must still call
    `brief()` with `for_date=None, only_mode=False, scope_summary=None` --
    byte-identical to today's zero-arg call."""
    captured: dict[str, object] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False, scope_summary=None):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        captured["scope_summary"] = scope_summary
        return 0, {"ok": True}

    monkeypatch.setattr(wc_brief, "brief", _fake_brief)
    rc = wc_brief.main([])
    assert rc == 0
    assert captured == {"for_date": None, "only_mode": False, "scope_summary": None}
    capsys.readouterr()


def test_main_threads_scope_summary_eq_form_into_brief(monkeypatch, capsys) -> None:
    """`--scope-summary=VALUE` (single-token eq-form, the spelling
    `workday-complete.md` Step 2 uses) reaches `brief()` verbatim, including
    a leading-dash value that would misparse in the split two-token form."""
    captured: dict[str, object] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False, scope_summary=None):
        captured["scope_summary"] = scope_summary
        return 0, {"ok": True}

    monkeypatch.setattr(wc_brief, "brief", _fake_brief)
    rc = wc_brief.main(["--scope-summary=-- wrapped the refactor"])
    assert rc == 0
    assert captured["scope_summary"] == "-- wrapped the refactor"
    capsys.readouterr()
