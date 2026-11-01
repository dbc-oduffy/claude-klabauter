"""A halted fan-out slice refires from its own (fully sized) slice sizing; a live one does not."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import parent_record_rel, status_rel

_SIZING = {"em_analysis": {"ask_parent": "p1 r1"}}


def _status(root, status):
    path = root / status_rel("r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": "r1", "status": status}), encoding="utf-8")


def test_a_sized_slice_resolves_its_reserved_run(tmp_path):
    assert op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml") == ("r1", "p1")


def test_a_raw_ask_cannot_take_a_reserved_run(tmp_path):
    with pytest.raises(ValueError, match="not a raw ask"):
        op._reserved_run(tmp_path, "r1", None, None)


def test_a_rebind_keeps_the_first_binds_run_base(tmp_path, monkeypatch):
    import coordinator_core.git.git_state as git_state

    parent = tmp_path / parent_record_rel("p1")
    parent.parent.mkdir(parents=True)
    parent.write_text("{}", encoding="utf-8")
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: next(heads))
    assert op._recorded_run_base(tmp_path, "r1") is None
    op._bind_reserved_run(tmp_path, "r1", "p1")
    _status(tmp_path, "halted")
    op._bind_reserved_run(tmp_path, "r1", "p1")
    assert op._recorded_run_base(tmp_path, "r1") == "a" * 40


def test_a_halted_slice_rebinds_and_a_running_one_is_refused(tmp_path):
    parent = tmp_path / parent_record_rel("p1")
    parent.parent.mkdir(parents=True)
    parent.write_text("{}", encoding="utf-8")
    _status(tmp_path, "halted")
    assert op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml") == ("r1", "p1")
    op._bind_reserved_run(tmp_path, "r1", "p1")
    assert json.loads((tmp_path / status_rel("r1")).read_text())["status"] == "running"
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(tmp_path, "r1", "p1")


def _live(monkeypatch, value):
    from coordinator_core import warp_live

    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda *_a, **_k: value)


def _parent(root):
    parent = root / parent_record_rel("p1")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("{}", encoding="utf-8")


def test_a_stale_running_status_settles_halted_and_rebinds(tmp_path, monkeypatch):
    import subprocess

    _parent(tmp_path)
    _status(tmp_path, "running")
    _live(monkeypatch, None)

    def _no_spawn(*_a, **_k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    assert op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml") == ("r1", "p1")
    assert json.loads((tmp_path / status_rel("r1")).read_text())["status"] == "halted"
    op._bind_reserved_run(tmp_path, "r1", "p1")
    assert json.loads((tmp_path / status_rel("r1")).read_text())["status"] == "running"


def test_a_live_run_is_refused_by_name_not_as_already_bound(tmp_path, monkeypatch):
    _status(tmp_path, "running")
    _live(monkeypatch, "r1")
    with pytest.raises(ValueError, match="Run r1 is already working") as exc:
        op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml")
    assert "already bound" not in str(exc.value)
    assert json.loads((tmp_path / status_rel("r1")).read_text())["status"] == "running"


def test_a_landed_run_is_not_a_refire_target(tmp_path, monkeypatch):
    _live(monkeypatch, None)
    path = tmp_path / status_rel("r1")
    path.parent.mkdir(parents=True)
    body = json.dumps({"status": "landed", "sha": "a" * 40})
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="landed at " + "a" * 40 + ".*new sizing"):
        op._reserved_run(tmp_path, "r1", _SIZING, "state/sizings/s.yaml")
    assert path.read_text() == body


def test_a_second_bind_of_a_just_bound_run_is_refused(tmp_path):
    _parent(tmp_path)
    op._bind_reserved_run(tmp_path, "r1", "p1")
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(tmp_path, "r1", "p1")


_SIZING_REL = "state/sizings/s.yaml"
_SCRIPT = "scratch/warp/x.mjs"


def _real_token_run(tmp_path, monkeypatch, transcript_body):
    """A repo with a real run token for r1 naming the slice sizing, a submission record, and a
    launch record, the transcript holding `transcript_body`; liveness is never monkeypatched."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import run_token

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    run_token.mint(root, "r1", _SCRIPT, session_id="s1", sizing_rel=_SIZING_REL)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(transcript_body, encoding="utf-8")
    warp_live.record_submission(
        root, "r1", session_id="s1", transcript_path=str(transcript), script_path=_SCRIPT,
        sizing_rel=_SIZING_REL, tool_use_id="tu1",
    )
    (managed / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": _SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    _parent(root)
    _status(root, "running")
    return root


def test_e_a_real_token_whose_task_ended_settles_halted_and_rebinds(tmp_path, monkeypatch):
    import subprocess
    import time

    body = "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n"
    root = _real_token_run(tmp_path, monkeypatch, body)

    def _no_spawn(*_a, **_k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    started = time.process_time()
    got = op._reserved_run(root, "r1", _SIZING, _SIZING_REL)
    elapsed = time.process_time() - started
    assert got == ("r1", "p1")
    assert elapsed < 0.2, f"stale-branch _reserved_run took {elapsed * 1000:.1f}ms process time"
    assert json.loads((root / status_rel("r1")).read_text())["status"] == "halted"
    op._bind_reserved_run(root, "r1", "p1")
    assert json.loads((root / status_rel("r1")).read_text())["status"] == "running"


def _timed_out_hook_bind(tmp_path, monkeypatch, *, fired):
    """The timed-out hook's sequence: the emit minted r1's token and bound it (status running);
    `fired` adds the submission record and launch record a started launcher leaves."""
    root = _real_token_run(tmp_path, monkeypatch, "")
    if not fired:
        from coordinator_core import warp_live

        warp_live._submission_path(root, "r1").unlink(missing_ok=True)
        for rec in (tmp_path / "managed").glob("workflow-run-*.json"):
            rec.unlink()
    return root


def test_a_retry_adopts_a_bound_run_no_launcher_ever_started(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    root = _timed_out_hook_bind(tmp_path, monkeypatch, fired=False)
    assert not (warp_live._submission_path(root, "r1")).exists()
    op._bind_reserved_run(root, "r1", "p1", _SIZING_REL)
    assert json.loads((root / status_rel("r1")).read_text())["status"] == "running"
    assert (root / "scratch/warp/r1").is_dir()


def test_a_retry_still_refuses_a_bound_run_whose_launcher_started(tmp_path, monkeypatch):
    root = _timed_out_hook_bind(tmp_path, monkeypatch, fired=True)
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(root, "r1", "p1", _SIZING_REL)


def test_an_unfired_bind_without_the_sizing_or_for_another_run_is_refused(tmp_path, monkeypatch):
    root = _timed_out_hook_bind(tmp_path, monkeypatch, fired=False)
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(root, "r1", "p1")
    (root / status_rel("r1")).write_text(json.dumps({"run_id": "other", "status": "running"}), encoding="utf-8")
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(root, "r1", "p1", _SIZING_REL)


def test_a_landed_bind_is_never_adopted(tmp_path, monkeypatch):
    root = _timed_out_hook_bind(tmp_path, monkeypatch, fired=False)
    _status(root, "landed")
    with pytest.raises(ValueError, match="already bound"):
        op._bind_reserved_run(root, "r1", "p1", _SIZING_REL)


def test_g_a_corrupt_status_with_a_live_task_is_refused_by_name(tmp_path, monkeypatch):
    root = _real_token_run(tmp_path, monkeypatch, "")
    (root / status_rel("r1")).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Run r1 is already working"):
        op._reserved_run(root, "r1", _SIZING, _SIZING_REL)


def test_f_a_real_token_whose_task_is_not_terminal_is_refused_by_name(tmp_path, monkeypatch):
    root = _real_token_run(tmp_path, monkeypatch, "")
    with pytest.raises(ValueError, match="Run r1 is already working") as exc:
        op._reserved_run(root, "r1", _SIZING, _SIZING_REL)
    assert "already bound" not in str(exc.value)
    assert json.loads((root / status_rel("r1")).read_text())["status"] == "running"
