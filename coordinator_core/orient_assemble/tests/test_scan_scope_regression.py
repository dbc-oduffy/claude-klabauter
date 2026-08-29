"""
coordinator_core.orient_assemble.tests.test_scan_scope_regression — the
scan-scope regression suite AC6 of `2026-08-06-orient-assemble-reader-repo-
scope` calls for and that did not exist before this chunk landed.

Purpose: across the plan's seven test files, no test asserted that a reader
actually SCANS the repo root it was given — the exact gap that let a
`_read_reaper_dry_run`-shaped bug (an orient_assemble reader silently
reporting CLAUDE-KLABAUTER's own work-state to a caller invoking it from a different
repo) survive twelve days with the suite green throughout. This module
builds a fixture directory shaped like a foreign repo (`state/handoffs/`,
`docs/plans/`, `cross-repo/inbox/` with known contents) and asserts, for
each `repo_root`-threading reader that has real fixture-shaped contents to
scan, that a reader given a foreign root reports THAT root's contents —
not claude-klabauter's own, and not silence.

Spec backlink: state/dispatch-briefs/2026-08-06-orient-assemble-reader-repo-scope/C9.md

Negative-spec:
    - Does NOT assert a subprocess-cwd contract for `_read_reaper_dry_run` —
      per the chunk's 2026-08-29 enrichment, that reader is post-DR-362
      in-process (`_reap_survey(repo_root)` with no subprocess to pin a cwd
      on); its scan-target assertion here stubs `_reap_survey` and asserts
      the call argument, the same pattern `test_readers_health_reaper.py`'s
      `test_two_integer_contract_produces_expected_directive` already uses,
      extended rather than reinvented.
    - Does NOT drive `_read_reaper_dry_run`'s full `survey()` machinery
      (session liveness, git-log ship-detection) through a real fixture —
      that machinery is exercised by `coordinator_core/ops/tests/
      test_reap_in_flight_claims.py`, not this reader-boundary suite.
    - Does NOT touch the claude-klabauter repo's own `docs/plans/` or
      `state/handoffs/` — every "claude-klabauter-side" assertion in this file uses
      an isolated, empty tmp_path root, never the real repo tree, so this
      suite's pass/fail never depends on this repo's own live corpus
      contents.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.orient_assemble import readers_clean_ops as rco
from coordinator_core.orient_assemble import readers_handoff_triage as rht
from coordinator_core.orient_assemble import readers_health_reaper as rhr
from coordinator_core.ops.reap_in_flight_claims import SurveyResult


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def foreign_repo(tmp_path: Path) -> Path:
    """A tmp directory shaped like a foreign repo: `state/handoffs/`,
    `docs/plans/`, `cross-repo/inbox/` with known, fixture-specific
    contents distinguishable from claude-klabauter's own corpus by construction (the
    plan path and memo title below are nonsense strings that cannot
    coincidentally collide with a real claude-klabauter record)."""
    root = tmp_path / "foreign-repo"

    _write(
        root / "docs" / "plans" / "2099-01-01-zzz-foreign-orphan-plan.md",
        (
            "---\n"
            "status: draft\n"
            "created: 2099-01-01\n"
            "execution_authorized_by: zzz-foreign-authorizer\n"
            "---\n\n"
            "# ZZZ Foreign Orphan Plan\n"
        ),
    )

    _write(
        root / "cross-repo" / "inbox" / "2099-01-01-zzz-foreign-memo.md",
        (
            "---\n"
            "created: 2099-01-01\n"
            "from: zzz-foreign-sender\n"
            "title: ZZZ Foreign Memo Title\n"
            "status: open\n"
            "kind: ask\n"
            "---\n\n"
            "Body.\n"
        ),
    )

    # state/handoffs/ present but empty — exercised by the orphan-plan
    # ownership walk (an empty dir is a legal "no owner found" case) and
    # named in the fixture per the brief's required shape even though this
    # file's tests don't populate it with a claim (that machinery belongs
    # to test_reap_in_flight_claims.py per this module's negative-spec).
    (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)

    return root


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    """An isolated, empty root — the "claude-klabauter clean" half of the silent-zero
    contrast. Never the real claude-klabauter repo tree (this suite must not depend
    on this repo's own live corpus contents)."""
    root = tmp_path / "clean-repo"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# _read_orphaned_plans (readers_handoff_triage) — docs/plans/ scan scope
# ---------------------------------------------------------------------------


def test_orphaned_plans_reports_foreign_root_contents_not_claude_klabauters(foreign_repo):
    result = rht._read_orphaned_plans(repo_root=foreign_repo)

    assert len(result.directives) == 1
    detail = result.directives[0]["detail"]
    assert "zzz-foreign-orphan-plan.md" in detail
    assert "zzz-foreign-authorizer" in detail


def test_orphaned_plans_silent_zero_on_clean_root(clean_repo):
    """The high-value case: an empty root has nothing to report — silence,
    not an error, and never a leak of some OTHER root's contents."""
    result = rht._read_orphaned_plans(repo_root=clean_repo)

    assert result.directives == []
    assert result.judgment_points == []


def test_orphaned_plans_scan_scope_is_the_passed_root_not_ambient(foreign_repo, clean_repo):
    """The exact regression shape: two DIFFERENT roots given to the SAME
    reader in the same process must report DIFFERENT (their own) results —
    proof the root is actually threaded into the scan, not read once from
    an ambient/module-pinned location and cached or ignored."""
    foreign_result = rht._read_orphaned_plans(repo_root=foreign_repo)
    clean_result = rht._read_orphaned_plans(repo_root=clean_repo)

    assert foreign_result.directives != clean_result.directives
    assert clean_result.directives == []


def test_collect_threads_repo_root_into_orphaned_plans(foreign_repo):
    """The `collect()` seam the assembler actually calls, not only the
    private reader — proves `repo_root` survives the `collect()` ->
    `_read_orphaned_plans` handoff (site (a), C4).

    `_cmd_ready` is stubbed out here: its `records_query` import is a
    pre-existing environment gap unrelated to this chunk's scan-scope
    surface (reproduced by running any pre-existing test that reaches
    `_read_ready`, e.g. `test_narration_and_constructor_discipline.py`, in
    isolation) — stubbing it keeps this test on the scan-scope question,
    not that gap."""
    with mock.patch.object(rht, "_cmd_ready", return_value=0), mock.patch.object(
        rht, "_cmd_awaiting_gate", return_value=0
    ):
        result = rht.collect("day", repo_root=str(foreign_repo))

    details = [d["detail"] for d in result.directives if d["id"] == "d-plan-orphan-tiers"]
    assert details, "expected a d-plan-orphan-tiers directive from the foreign root"
    assert "zzz-foreign-orphan-plan.md" in details[0]


# ---------------------------------------------------------------------------
# _read_memo_surface (readers_clean_ops) — cross-repo/inbox/ scan scope
# ---------------------------------------------------------------------------


def test_memo_surface_reports_foreign_root_contents_not_claude_klabauters(foreign_repo):
    result = rco._read_memo_surface("surface", repo_root=str(foreign_repo))

    assert len(result.judgment_points) == 1
    question = result.judgment_points[0]["question"]
    assert "ZZZ Foreign Memo Title" in question
    assert "zzz-foreign-sender" in question


def test_memo_surface_silent_zero_on_clean_root(clean_repo):
    result = rco._read_memo_surface("surface", repo_root=str(clean_repo))

    assert result.directives == []
    assert result.judgment_points == []


def test_memo_surface_scan_scope_is_the_passed_root_not_ambient(foreign_repo, clean_repo):
    foreign_result = rco._read_memo_surface("surface", repo_root=str(foreign_repo))
    clean_result = rco._read_memo_surface("surface", repo_root=str(clean_repo))

    assert foreign_result.judgment_points != clean_result.judgment_points
    assert clean_result.judgment_points == []


def test_collect_threads_repo_root_into_memo_surface(foreign_repo):
    result = rco.collect("day", repo_root=str(foreign_repo))

    questions = [jp["question"] for jp in result.judgment_points if jp["id"].startswith("j-memo-")]
    assert any("ZZZ Foreign Memo Title" in q for q in questions)


# ---------------------------------------------------------------------------
# _read_reaper_dry_run (readers_health_reaper) — scan-target pin, extending
# the existing `_reap_survey`-stub pattern per the 2026-08-29 enrichment
# rather than a subprocess-cwd assertion (no subprocess exists post-DR-362).
# ---------------------------------------------------------------------------


def test_reaper_dry_run_scan_target_is_the_passed_repo_root(foreign_repo):
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        rhr._read_reaper_dry_run(str(foreign_repo))

    survey_mock.assert_called_once_with(str(foreign_repo))


def test_reaper_dry_run_falls_back_to_claude_klabauter_root_when_none_given():
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        rhr._read_reaper_dry_run()

    survey_mock.assert_called_once_with(rhr._CLAUDE_KLABAUTER_ROOT)


def test_reaper_dry_run_two_different_roots_scan_differently(foreign_repo, clean_repo):
    """Same reader, same process, two different threaded roots must reach
    `_reap_survey` with two different arguments — the regression shape,
    asserted at the stub-call seam per this module's negative-spec."""
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        rhr._read_reaper_dry_run(str(foreign_repo))
        rhr._read_reaper_dry_run(str(clean_repo))

    called_roots = [call.args[0] for call in survey_mock.call_args_list]
    assert called_roots == [str(foreign_repo), str(clean_repo)]
    assert called_roots[0] != called_roots[1]


def test_collect_day_cadence_threads_repo_root_into_reaper(foreign_repo):
    """`_cmd_working_repo_registration` is stubbed here too: its
    `cli_shared` import is the same pre-existing environment gap noted on
    `test_collect_threads_repo_root_into_orphaned_plans` (reproduced by
    `test_readers_health_reaper.py`'s own pre-existing failures, unrelated
    to this chunk), so stubbing it keeps this test on the scan-scope
    question the reaper reader actually owns."""
    fake_result = SurveyResult(would_release=1, would_reclaim=0, dispositions=[])
    with mock.patch.object(
        rhr, "_reap_survey", return_value=fake_result
    ) as survey_mock, mock.patch.object(
        rhr, "_cmd_working_repo_registration", return_value=0
    ):
        rhr.collect("day", repo_root=str(foreign_repo))

    survey_mock.assert_called_once_with(str(foreign_repo))


# ---------------------------------------------------------------------------
# AC4's two deliberately-pinned probes ignore the passed root (negative).
# ---------------------------------------------------------------------------


def test_claude_klabauter_bin_sentinel_signature_takes_no_repo_root():
    """`_read_claude_klabauter_bin_sentinel` is the script-location role
    (`_HEALTH_PROBES_PATH`), never the scan-scope role — it has no
    `repo_root` parameter to ignore-or-honor at all, which IS the pin."""
    params = inspect.signature(rhr._read_claude_klabauter_bin_sentinel).parameters
    assert "repo_root" not in params


def test_working_repo_registration_signature_takes_no_repo_root():
    params = inspect.signature(rhr._read_working_repo_registration).parameters
    assert "repo_root" not in params


def test_collect_does_not_forward_repo_root_to_the_two_pinned_probes(foreign_repo):
    """Even when `collect()` is given a foreign `repo_root`, the two
    deliberately-pinned probes are called with their own no-argument form —
    proof `collect()` itself does not attempt to thread scan scope into
    them, matching AC4's "ignore the passed root" contract."""
    with mock.patch.object(
        rhr, "_cmd_claude_klabauter_bin_sentinel", return_value=0
    ) as sentinel_mock, mock.patch.object(
        rhr, "_cmd_working_repo_registration", return_value=0
    ) as registration_mock, mock.patch.object(
        rhr, "_cmd_ceremony_hook", return_value=None
    ), mock.patch.object(
        rhr, "_reap_survey",
        return_value=SurveyResult(would_release=0, would_reclaim=0, dispositions=[]),
    ):
        rhr.collect("day", repo_root=str(foreign_repo))

    sentinel_mock.assert_called_once_with([])
    registration_mock.assert_called_once_with([])
