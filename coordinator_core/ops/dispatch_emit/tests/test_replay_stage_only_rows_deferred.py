"""Replay wf_8c9e7fb5 (ea2e26 halt 02e429c8c8): the stage reads a deferred row named in only_rows as no work, never as a spine miss."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import _plan, _row, _stage


def _deferred_plan(tmp_path):
    _plan(
        tmp_path,
        _row("C1") + _row("C10", "  deferred: true\n") + _row("C11", "  deferred_until: 2099-01-01\n"),
    )


def test_deferred_rows_named_in_only_rows_are_never_staged_and_never_refused(tmp_path):
    _deferred_plan(tmp_path)
    reply = _stage(tmp_path, ["C1", "C10", "C11"])
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["C1"]


def test_a_brief_naming_only_deferred_rows_refuses_for_no_work_not_for_a_missing_id(tmp_path):
    _deferred_plan(tmp_path)
    error = _stage(tmp_path, ["C10", "C11"])["error"]
    assert "zero dispatchable rows" in error and "not in the plan's spine" not in error


def test_an_id_the_plan_never_carried_still_refuses(tmp_path):
    _deferred_plan(tmp_path)
    error = _stage(tmp_path, ["C1", "C99"])["error"]
    assert "not in the plan's spine" in error and "C99" in error
