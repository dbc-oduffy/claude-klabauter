
from __future__ import annotations

import inspect
import sys
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

    (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)

    return root


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    root = tmp_path / "clean-repo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_orphaned_plans_reports_foreign_root_contents_not_claude_klabauters(foreign_repo):
    result = rht._read_orphaned_plans(repo_root=foreign_repo)

    assert len(result.directives) == 1
    detail = result.directives[0]["detail"]
    assert "zzz-foreign-orphan-plan.md" in detail
    assert "zzz-foreign-authorizer" in detail


def test_orphaned_plans_silent_zero_on_clean_root(clean_repo):
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
    with mock.patch.object(rht, "_cmd_ready", return_value=0), mock.patch.object(
        rht, "_cmd_awaiting_gate", return_value=0
    ):
        result = rht.collect("day", repo_root=str(foreign_repo))

    details = [d["detail"] for d in result.directives if d["id"] == "d-plan-orphan-tiers"]
    assert details, "expected a d-plan-orphan-tiers directive from the foreign root"
    assert "zzz-foreign-orphan-plan.md" in details[0]


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
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        rhr._read_reaper_dry_run(str(foreign_repo))
        rhr._read_reaper_dry_run(str(clean_repo))

    called_roots = [call.args[0] for call in survey_mock.call_args_list]
    assert called_roots == [str(foreign_repo), str(clean_repo)]
    assert called_roots[0] != called_roots[1]


def test_collect_day_cadence_threads_repo_root_into_reaper(foreign_repo):
    fake_result = SurveyResult(would_release=1, would_reclaim=0, dispositions=[])
    with mock.patch.object(
        rhr, "_reap_survey", return_value=fake_result
    ) as survey_mock, mock.patch.object(
        rhr, "_cmd_working_repo_registration", return_value=0
    ):
        rhr.collect("day", repo_root=str(foreign_repo))

    survey_mock.assert_called_once_with(str(foreign_repo))


def test_read_ready_forwards_repo_root_onto_the_cmd_namespace(foreign_repo):
    with mock.patch.object(rht, "_cmd_ready", return_value=0) as cmd_mock:
        rht._read_ready(repo_root=foreign_repo)

    called_args = cmd_mock.call_args.args[0]
    assert called_args.repo_root == str(foreign_repo)


def test_read_ready_namespace_repo_root_is_none_when_none_given():
    with mock.patch.object(rht, "_cmd_ready", return_value=0) as cmd_mock:
        rht._read_ready()

    called_args = cmd_mock.call_args.args[0]
    assert called_args.repo_root is None


def test_read_awaiting_gate_forwards_repo_root_onto_the_cmd_namespace(foreign_repo):
    with mock.patch.object(rht, "_cmd_awaiting_gate", return_value=0) as cmd_mock:
        rht._read_awaiting_gate(repo_root=foreign_repo)

    called_args = cmd_mock.call_args.args[0]
    assert called_args.repo_root == str(foreign_repo)


def test_read_awaiting_gate_namespace_repo_root_is_none_when_none_given():
    with mock.patch.object(rht, "_cmd_awaiting_gate", return_value=0) as cmd_mock:
        rht._read_awaiting_gate()

    called_args = cmd_mock.call_args.args[0]
    assert called_args.repo_root is None


def test_collect_threads_repo_root_into_ready_and_awaiting_gate(foreign_repo):
    with mock.patch.object(rht, "_cmd_ready", return_value=0) as ready_mock, mock.patch.object(
        rht, "_cmd_awaiting_gate", return_value=0
    ) as gate_mock:
        rht.collect("day", repo_root=str(foreign_repo))

    assert ready_mock.call_args.args[0].repo_root == str(foreign_repo)
    assert gate_mock.call_args.args[0].repo_root == str(foreign_repo)


def test_cmd_ready_forwards_namespace_repo_root_to_query_records_as_explicit_root(foreign_repo):
    """One level lower than the Namespace-seam tests above: proves
    `_cmd_ready` itself (workday-start-handoff-triage.py) reads `args.
    repo_root` and forwards it to `query_records(..., explicit_root=...)` —
    the other half of the echo-field gap. `_cmd_ready` imports `query_
    records` via a DEFERRED `from records_query import query_records`
    inside its own body (not a module-level attribute on the loaded
    `_handoff_triage` module), so a fake `records_query` module is injected
    into `sys.modules` for the duration of the call — the only seam that
    reaches a deferred same-call import — rather than patching an attribute
    that does not exist until the function actually runs. Sidesteps this
    environment's bare `import records_query` gap (see this section's own
    header note) entirely, since the deferred import consults `sys.modules`
    first.

    Asserts on the RECORDS RETURNED (the printed output), not only the
    `explicit_root` kwarg having arrived (Review: code-reviewer — Finding
    1): the fake `query_records` returns a foreign-root-distinguishing
    marker, and the test proves `_cmd_ready` actually threads that returned
    content through to its own output — the "parameter arrived" assertion
    alone would still pass if `_cmd_ready` forwarded `explicit_root` but
    then discarded or ignored what `query_records` gave back."""
    import argparse
    import contextlib
    import io

    fake_module = mock.MagicMock()
    fake_module.query_records = mock.MagicMock(
        return_value="- [ZZZ Foreign Ready Handoff](zzz-foreign-ready.md) — ready\n"
    )
    buf = io.StringIO()
    with mock.patch.dict(sys.modules, {"records_query": fake_module}):
        with contextlib.redirect_stdout(buf):
            rht._cmd_ready(argparse.Namespace(repo_root=str(foreign_repo)))

    assert fake_module.query_records.call_args.kwargs.get("explicit_root") == str(foreign_repo)
    assert "ZZZ Foreign Ready Handoff" in buf.getvalue()


def test_cmd_awaiting_gate_forwards_namespace_repo_root_to_query_records_as_explicit_root(foreign_repo):
    import argparse
    import contextlib
    import io

    fake_module = mock.MagicMock()
    fake_module.query_records = mock.MagicMock(
        side_effect=[
            "- [ZZZ Foreign Full Listing](zzz-foreign-full.md) — awaiting_gate\n",
            "- [ZZZ Foreign Stale Subset](zzz-foreign-stale.md) — awaiting_gate\n",
        ]
    )
    buf = io.StringIO()
    with mock.patch.dict(sys.modules, {"records_query": fake_module}):
        with contextlib.redirect_stdout(buf):
            rht._cmd_awaiting_gate(argparse.Namespace(repo_root=str(foreign_repo)))

    assert fake_module.query_records.call_count == 2
    for call in fake_module.query_records.call_args_list:
        assert call.kwargs.get("explicit_root") == str(foreign_repo)
    output = buf.getvalue()
    assert "ZZZ Foreign Full Listing" in output
    assert "ZZZ Foreign Stale Subset" in output


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
