"""dispatch.emit abandon_run closes a named minted-only run with no live token and refuses a driven one."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halted_run, op, run_token

RUN = "ask-20261101T032019-6ac67b"


def _run(tmp_path, stage="minted", marker=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    rdir = tmp_path / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    if stage is not None:
        (rdir / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    if marker:
        (rdir / halted_run.HALTED_FILE).write_text(json.dumps({"status": marker}), encoding="utf-8")
    return rdir


def _abandon(tmp_path, **extra):
    return op._abandon_run({"abandon_run": RUN, "target_root": str(tmp_path), **extra}, None)


def _marker(rdir):
    return json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))


def test_minted_only_run_without_token_is_closed(tmp_path):
    rdir = _run(tmp_path)
    out = _abandon(tmp_path)
    assert out["abandoned"] is True
    assert _marker(rdir)["status"] == halted_run.STATUS_CLOSED
    assert _marker(rdir)["closed"]["reason"] == "minted-only-run-abandoned"
    assert not halted_run._is_shell(rdir)


def test_run_without_stage_file_is_closed(tmp_path):
    rdir = _run(tmp_path, stage=None)
    assert _abandon(tmp_path)["abandoned"] is True
    assert (rdir / halted_run.HALTED_FILE).exists()


def test_live_token_run_is_untouched(tmp_path):
    rdir = _run(tmp_path)
    run_token.mint(tmp_path, RUN, "scratch/w.mjs", session_id="other", sizing_rel="state/sizings/s.yaml")
    assert _abandon(tmp_path)["abandoned"] is False
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert run_token.is_live(tmp_path, RUN)


def test_launched_workflow_run_is_untouched(tmp_path):
    rdir = _run(tmp_path, stage="staged")
    assert _abandon(tmp_path)["abandoned"] is False
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_halted_run_is_untouched(tmp_path):
    rdir = _run(tmp_path, marker=halted_run.STATUS_HALTED)
    assert _abandon(tmp_path)["abandoned"] is False
    assert _marker(rdir)["status"] == halted_run.STATUS_HALTED


def test_missing_run_dir_writes_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _abandon(tmp_path)["abandoned"] is False
    assert not (tmp_path / "state" / "warp" / RUN).exists()


def test_closed_run_is_skipped_by_shell_detection(tmp_path):
    rdir = _run(tmp_path)
    _abandon(tmp_path)
    assert halted_run._is_shell(rdir) is False
