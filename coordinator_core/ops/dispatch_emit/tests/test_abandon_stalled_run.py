"""dispatch.emit abandon_run: a stalled own-session run is closed and its token released; caller-repo path anchoring."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, op, run_token

RUN = "ask-20261029T000000-aaaaaa"
SIZING = "state/sizings/s.yaml"


def _stalled(tmp_path, session="s1"):
    (tmp_path / ".git").mkdir()
    rdir = tmp_path / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    run_token.mint(tmp_path, RUN, "scratch/w.mjs", session_id=session, sizing_rel=SIZING)
    return rdir


def _abandon(tmp_path, **extra):
    return op._abandon_run({"abandon_run": RUN, "_caller_cwd": str(tmp_path / "sub"), **extra}, None)


def test_own_session_stalled_run_is_closed_and_token_released(tmp_path):
    rdir = _stalled(tmp_path)
    (tmp_path / "sub").mkdir()
    out = _abandon(tmp_path, session_id="s1")
    assert out["abandoned"] is True
    assert json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))["status"] == halted_run.STATUS_CLOSED
    assert not run_token.is_live(tmp_path, RUN)


def test_foreign_session_run_is_untouched(tmp_path):
    rdir = _stalled(tmp_path, session="other")
    (tmp_path / "sub").mkdir()
    out = _abandon(tmp_path, session_id="s1")
    assert out["abandoned"] is False
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert run_token.is_live(tmp_path, RUN)


def test_no_root_raises(tmp_path):
    with pytest.raises(ValueError):
        op._abandon_run({"abandon_run": RUN, "session_id": "s1"}, None)


def test_relative_plan_path_anchors_to_caller_cwd(tmp_path):
    out = op._anchor_relative_paths({"plan_path": "docs/p.md", "_caller_cwd": str(tmp_path)})
    assert out["plan_path"] == str(tmp_path / "docs/p.md")


def test_relative_plan_path_anchors_to_repo_root_without_stamp(tmp_path):
    out = op._anchor_relative_paths({"plan_path": "docs/p.md"}, tmp_path)
    assert out["plan_path"] == str(tmp_path / "docs/p.md")


def test_absolute_path_and_missing_cwd_unchanged(tmp_path):
    absolute = str(tmp_path / "p.md")
    assert op._anchor_relative_paths({"plan_path": absolute, "_caller_cwd": "/x"})["plan_path"] == absolute
    assert op._anchor_relative_paths({"plan_path": "docs/p.md"})["plan_path"] == "docs/p.md"
