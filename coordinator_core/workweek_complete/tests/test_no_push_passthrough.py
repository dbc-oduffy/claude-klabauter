"""`--no-push` reaches every pushing workweek-complete directive; the default argv is unchanged."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import date, datetime

import pytest

from coordinator_core.cadence.period_mode import PeriodMode
from coordinator_core.workweek_complete import apply as ww_apply
from coordinator_core.workweek_complete import brief as ww_brief
from coordinator_core.workweek_complete.brief_inputs import BriefInputs

MISSED = ("2026-11-02", "2026-11-09")
OBSERVER_JP = "jp_step7b_observer_dispatch"


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(ww_brief, "resolve_operator_config", lambda env=None: {})
    monkeypatch.setattr(ww_brief, "compute_brief_inputs", lambda: BriefInputs(repo_root=None))
    monkeypatch.setattr(ww_brief, "_daily_gap_summary", lambda _root: "")

    def period(_root, _now, *, catch_up, conclude):
        if catch_up:
            return PeriodMode("catch_up", "flag", "2026-11-16", MISSED, "test"), date(2026, 11, 2)
        return PeriodMode("conclude", "flag", "2026-11-16", (), "test"), None

    monkeypatch.setattr(ww_brief, "_resolve_week_period", period)


def _brief(**kw):
    with redirect_stdout(io.StringIO()):
        code, env = ww_brief.brief(decisions={}, now_local=datetime(2026, 11, 16, 18, 0), **kw)
    assert code == 0
    return env


def _directive(env, directive_id):
    return next(d for d in env["directives"] if d["id"] == directive_id)


def test_conclude_no_push_marks_archive_close(stub):
    assert "--no-push" in _directive(_brief(no_push=True), "d_step13_archive_close")["args"]


def test_default_carries_no_no_push(stub):
    env = _brief()
    assert _directive(env, "d_step13_archive_close")["args"] == []
    assert all(d["id"].startswith(ww_brief.CATCH_UP_ARCHIVE_PREFIX) or "--no-push" not in d["args"]
               for d in env["directives"])


def test_catch_up_no_push_keeps_every_archive_local_and_the_strategic_review(stub):
    env = _brief(catch_up=True, no_push=True)
    archives = [d for d in env["directives"] if d["id"].startswith(ww_brief.CATCH_UP_ARCHIVE_PREFIX)]
    assert len(archives) == len(MISSED)
    assert all("--no-push" in d["args"] for d in archives)
    assert OBSERVER_JP in {p["id"] for p in env["judgment_points"]}


def test_catch_up_without_no_push_is_unchanged_by_the_flag(stub):
    assert _brief(catch_up=True) == _brief(catch_up=True, no_push=False)


def test_brief_cli_accepts_no_push(stub, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return 0, {}

    monkeypatch.setattr(ww_brief, "brief", fake)
    with redirect_stdout(io.StringIO()):
        assert ww_brief.main(["--catch-up", "--no-push"]) == 0
    assert seen == {"catch_up": True, "conclude": False, "no_push": True}
    seen.clear()
    with redirect_stdout(io.StringIO()):
        ww_brief.main(["--catch-up"])
    assert "no_push" not in seen


def test_apply_main_accepts_no_push_and_threads_it_into_the_recompute(stub, monkeypatch, capsys):
    seen = {}

    def fake_brief(**kw):
        seen.update(kw)
        return 1, {"error": "stop after recompute"}

    monkeypatch.setattr(ww_apply, "brief", fake_brief)
    rc = ww_apply.main(["--catch-up", "--no-push"])
    assert rc == int(ww_apply.WorkweekApplyExitCode.TRANSPORT_FAIL)
    assert "unrecognized" not in capsys.readouterr().err
    assert seen["no_push"] is True and seen["catch_up"] is True
    seen.clear()
    ww_apply.main(["--catch-up"])
    assert "no_push" not in seen
