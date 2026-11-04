"""dispatch.minted_run: start hands back fire_args, abandon halts and reaps, both refuse a run past minted."""

from __future__ import annotations

import json
import time

from coordinator_core.ops.dispatch_emit import abandoned_runs, halted_run, minted_run, run_token

RUN = "ask-20261102T120000-abcdef"


def _repo(tmp_path, stage="minted", script=True):
    (tmp_path / ".git").mkdir(exist_ok=True)
    rdir = tmp_path / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    if stage is not None:
        (rdir / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    if script:
        scr = tmp_path / "scratch" / "warp" / f"{RUN}.workflow.mjs"
        scr.parent.mkdir(parents=True)
        scr.write_text("// script\n", encoding="utf-8")
    return rdir


def _call(tmp_path, action, **extra):
    return minted_run._handler({"run_id": RUN, "action": action, **extra}, tmp_path)


def test_start_returns_fire_args_and_escrowed_token(tmp_path):
    _repo(tmp_path)
    token = run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    assert run_token.escrow(tmp_path, RUN, token)
    out = _call(tmp_path, "start")
    assert out["ok"] is True and out["refusal"] is None
    assert out["fire_args"] == {"repoRoot": tmp_path.as_posix(), "runToken": token}
    assert out["script_path"] == f"scratch/warp/{RUN}.workflow.mjs"


def test_start_without_escrow_still_returns_repo_root(tmp_path):
    _repo(tmp_path)
    out = _call(tmp_path, "start")
    assert out["ok"] is True
    assert out["fire_args"] == {"repoRoot": tmp_path.as_posix()}


def test_start_with_no_stage_file_is_allowed(tmp_path):
    _repo(tmp_path, stage=None)
    assert _call(tmp_path, "start")["ok"] is True


def test_start_refuses_when_the_script_is_missing(tmp_path):
    _repo(tmp_path, script=False)
    out = _call(tmp_path, "start")
    assert out["ok"] is False and out["fire_args"] is None
    assert out["refusal"].startswith(minted_run.REFUSAL_NO_SCRIPT)


def test_start_writes_nothing(tmp_path):
    rdir = _repo(tmp_path)
    before = sorted(p.name for p in rdir.iterdir())
    _call(tmp_path, "start")
    assert sorted(p.name for p in rdir.iterdir()) == before


def test_both_actions_refuse_a_run_past_minted(tmp_path):
    rdir = _repo(tmp_path, stage="staged")
    for action in ("start", "abandon"):
        out = _call(tmp_path, action)
        assert out["ok"] is False
        assert out["refusal"].startswith(minted_run.REFUSAL_PAST_MINTED)
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_both_actions_refuse_a_halt_marked_run(tmp_path):
    rdir = _repo(tmp_path)
    (rdir / halted_run.HALTED_FILE).write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    for action in ("start", "abandon"):
        assert _call(tmp_path, action)["refusal"].startswith(minted_run.REFUSAL_PAST_MINTED)


def test_unknown_run_is_a_named_refusal(tmp_path):
    (tmp_path / ".git").mkdir()
    out = minted_run._handler({"run_id": "nope", "action": "start"}, tmp_path)
    assert out["ok"] is False and out["refusal"].startswith(minted_run.REFUSAL_NOT_FOUND)


def test_abandon_clears_a_token_with_no_run_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    token = run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    assert run_token.escrow(tmp_path, RUN, token)
    assert run_token.is_live(tmp_path, RUN)
    out = _call(tmp_path, "abandon")
    assert out["ok"] is True and out["refusal"] is None
    assert not run_token.is_live(tmp_path, RUN)
    assert run_token._read_rec(tmp_path, RUN, escrow=True) is None
    assert not (tmp_path / "state" / "warp" / RUN).exists()
    assert _call(tmp_path, "abandon")["refusal"].startswith(minted_run.REFUSAL_NOT_FOUND)


def test_start_on_a_token_with_no_run_dir_still_refuses(tmp_path):
    (tmp_path / ".git").mkdir()
    run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    assert _call(tmp_path, "start")["refusal"].startswith(minted_run.REFUSAL_NOT_FOUND)
    assert run_token.is_live(tmp_path, RUN)


def test_abandon_dry_run_keeps_a_dirless_token(tmp_path):
    (tmp_path / ".git").mkdir()
    run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    assert _call(tmp_path, "abandon", dry_run=True)["ok"] is True
    assert run_token.is_live(tmp_path, RUN)


def test_abandon_with_neither_token_nor_dir_refuses(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _call(tmp_path, "abandon")["refusal"].startswith(minted_run.REFUSAL_NOT_FOUND)


def test_start_given_the_common_dir_key_finds_the_run_from_another_cwd(tmp_path, monkeypatch):
    """The engine hands a common_dir-scoped op `<repo>/.git`, not the worktree root; cwd is elsewhere."""
    from coordinator_core.ipc import resolve_op_repo_key

    _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    key = resolve_op_repo_key(minted_run.OP_NAME, tmp_path)
    out = minted_run._handler({"run_id": RUN, "action": "start"}, key)
    assert out["ok"] is True, out
    assert out["fire_args"] == {"repoRoot": tmp_path.as_posix()}
    assert out["script_path"] == f"scratch/warp/{RUN}.workflow.mjs"


def test_unknown_run_is_still_refused_given_the_common_dir_key(tmp_path):
    from coordinator_core.ipc import resolve_op_repo_key

    (tmp_path / ".git").mkdir()
    key = resolve_op_repo_key(minted_run.OP_NAME, tmp_path)
    out = minted_run._handler({"run_id": "nope", "action": "start"}, key)
    assert out["ok"] is False and out["refusal"].startswith(minted_run.REFUSAL_NOT_FOUND)


def test_bad_params_are_named_refusals(tmp_path):
    assert minted_run._handler({"run_id": RUN, "action": "x"}, tmp_path)["refusal"].startswith(minted_run.REFUSAL_BAD_PARAMS)
    assert minted_run._handler({"run_id": "../x", "action": "start"}, tmp_path)["refusal"].startswith(minted_run.REFUSAL_BAD_PARAMS)
    assert minted_run._handler({"run_id": RUN, "action": "start"}, None)["refusal"].startswith(minted_run.REFUSAL_NO_REPO)


def test_abandon_halts_discards_token_and_reaps_only_this_run(tmp_path, monkeypatch):
    rdir = _repo(tmp_path)
    run_token.mint(tmp_path, RUN, f"scratch/warp/{RUN}.workflow.mjs")
    seen = []

    def fake_reap(params, repo_root=None):
        seen.append((params, repo_root))
        return {"ok": True, "reaped": [RUN], "skipped": [], "committed_sha": "a" * 40}

    monkeypatch.setattr(abandoned_runs, "_handler", fake_reap)
    out = _call(tmp_path, "abandon")
    assert out["ok"] is True and out["landed_sha"] == "a" * 40
    assert seen == [({"run_ids": [RUN]}, tmp_path)]
    marker = json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == "halted" and marker["reason"] == minted_run.ABANDON_REASON
    assert not run_token.is_live(tmp_path, RUN)
    assert not minted_run.is_minted_only(rdir)
    assert _call(tmp_path, "start")["refusal"].startswith(minted_run.REFUSAL_PAST_MINTED)


def test_abandon_the_reap_marker_is_what_the_reaper_reads(tmp_path):
    rdir = _repo(tmp_path)
    old = time.time() - abandoned_runs.QUIET_SECONDS - 5
    _call(tmp_path, "abandon", dry_run=True)
    assert not (rdir / halted_run.HALTED_FILE).exists()
    halted_run.write_halt_marker(rdir, halted="abandoned", sizing_rel=None, reason=minted_run.ABANDON_REASON)
    assert abandoned_runs._halt_marked(tmp_path, RUN) is True
    got = abandoned_runs.abandoned_run_ids(
        {f"state/warp/{RUN}/{halted_run.HALTED_FILE}"}, set(), "state/warp", {RUN: old}, time.time(), halted={RUN},
    )
    assert got == [RUN]


def test_abandon_dry_run_writes_nothing(tmp_path, monkeypatch):
    rdir = _repo(tmp_path)
    monkeypatch.setattr(abandoned_runs, "_handler", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reaped")))
    out = _call(tmp_path, "abandon", dry_run=True)
    assert out["ok"] is True and out["landed_sha"] is None
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_abandon_reports_a_skipped_reap_without_failing(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setattr(
        abandoned_runs, "_handler",
        lambda p, r=None: {"ok": True, "reaped": [], "skipped": [{"run_id": RUN, "reason": "live-or-recent"}], "committed_sha": None},
    )
    out = _call(tmp_path, "abandon")
    assert out["ok"] is True and out["landed_sha"] is None
    assert "live-or-recent" in out["note"]


def test_abandon_surfaces_a_failed_reap(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setattr(abandoned_runs, "_handler", lambda p, r=None: {"ok": False, "error": "git status failed"})
    out = _call(tmp_path, "abandon")
    assert out["ok"] is False and out["refusal"].startswith(minted_run.REFUSAL_REAP_FAILED)


def test_start_process_time_is_under_the_brightline(tmp_path):
    _repo(tmp_path)
    t0 = time.process_time()
    _call(tmp_path, "start")
    assert time.process_time() - t0 < 0.5
