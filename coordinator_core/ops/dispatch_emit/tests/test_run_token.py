"""run_token: mint, the atomic admit, redeem, redeem_claim, holds_claim, consume, sizing path normalising, and which sizings an in-flight run owns."""

from __future__ import annotations

import os
import threading
import time

import pytest

from coordinator_core.ops.dispatch_emit import run_token


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path


def test_normalize_sizing_rel_spellings_agree(tmp_path):
    repo = _repo(tmp_path)
    want = "state/sizings/mine.yaml"
    assert run_token.normalize_sizing_rel(repo, want) == want
    assert run_token.normalize_sizing_rel(repo, "./" + want) == want
    assert run_token.normalize_sizing_rel(repo, r"state\sizings\mine.yaml") == want
    assert run_token.normalize_sizing_rel(repo, str(repo / want)) == want
    assert run_token.normalize_sizing_rel(repo, "") == ""


def test_a_raw_ask_run_owns_only_the_sizing_its_gate_bound(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "scratch/warp/ask-1.workflow.mjs", session_id="s1")
    for name in ("mine.yaml", "unrelated.yaml"):
        (repo / "state" / "sizings" / name).write_text("x", encoding="utf-8")
    assert not run_token.run_owns_sizing(repo, "s1", "state/sizings/unrelated.yaml")
    assert not run_token.run_owns_sizing(repo, "s1", "state/sizings/mine.yaml")
    assert run_token.bind_sizing(repo, "ask-1", "state/sizings/mine.yaml")
    assert run_token.run_owns_sizing(repo, "s1", "state/sizings/mine.yaml")
    assert not run_token.run_owns_sizing(repo, "s1", "state/sizings/unrelated.yaml")
    assert not run_token.run_owns_sizing(repo, "s2", "state/sizings/mine.yaml")
    older = repo / "state" / "sizings" / "unrelated.yaml"
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    assert not run_token.bind_sizing(repo, "ask-1", "state/sizings/unrelated.yaml")
    os.utime(older, ns=(time.time_ns() + 10**10, time.time_ns() + 10**10))
    assert run_token.bind_sizing(repo, "ask-1", "state/sizings/unrelated.yaml")
    assert run_token.run_owns_sizing(repo, "s1", "state/sizings/unrelated.yaml")


def test_admits_checks_without_claiming(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "b.mjs")
    assert not run_token.admits(repo, "../x", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.is_live(repo, "ask-1")


def test_a_guard_admit_with_no_op_run_leaves_the_retry_admissible(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_release_returns_a_claimed_token_to_live_once(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.release(repo, "ask-1", "f" * 32, "a.mjs")
    assert run_token.release(repo, "ask-1", token, "a.mjs")
    assert not run_token.release(repo, "ask-1", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")


def _claimed_path(repo):
    return run_token._record_path(repo, "ask-1", claimed=True)


def test_a_live_in_budget_holder_keeps_its_claim(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_a_dead_holder_claim_is_adopted_by_the_refire(tmp_path, monkeypatch):
    import json

    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    path = _claimed_path(repo)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["pid"] > 0 and record["claimed_at"] > 0
    path.write_text(json.dumps({**record, "pid": record["pid"] + 1}), encoding="utf-8")  # another process
    monkeypatch.setattr(run_token, "_holder_alive", lambda pid: False)
    assert not run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")  # the adopter is this live thread


def test_an_over_age_claim_whose_holder_lives_is_kept(tmp_path, monkeypatch):
    """group-em-7d: adopting a live, overdue holder lands a second commit over one still running."""
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + run_token.CLAIM_MAX_AGE_SECONDS + 5)
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_a_same_process_claim_is_adopted_once_its_thread_exits(tmp_path, monkeypatch):
    import threading

    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    won = []
    holder = threading.Thread(target=lambda: won.append(run_token.redeem(repo, "ask-1", token, "a.mjs")))
    holder.start()
    holder.join()
    assert won == [True]
    assert not run_token.redeem(repo, "ask-1", "f" * 32, "a.mjs")
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + run_token.THREAD_EXIT_GRACE_SECONDS + 1)
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_a_same_process_claim_is_kept_while_its_thread_runs(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")  # this thread holds it and is alive
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_a_legacy_claim_without_pid_ages_by_file_mtime(tmp_path):
    import json
    import os

    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    live = run_token._record_path(repo, "ask-1")
    record = json.loads(live.read_text(encoding="utf-8"))
    path = _claimed_path(repo)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    live.unlink()
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    old = time.time() - run_token.CLAIM_MAX_AGE_SECONDS - 5
    os.utime(path, (old, old))
    assert not run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_consume_during_a_claim_leaves_nothing_to_release(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    run_token.consume(repo, "ask-1")
    assert not run_token.release(repo, "ask-1", token, "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert _tokens_dir_files(repo) == ["retired/ask-1.json"]
    assert run_token.halt_admits(repo, "ask-1", token, "a.mjs")


def test_release_never_overwrites_a_fresh_mint(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", first, "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.release(repo, "ask-1", first, "a.mjs")
    assert run_token.admits(repo, "ask-1", second, "a.mjs")
    assert not run_token.admits(repo, "ask-1", first, "a.mjs")


@pytest.mark.parametrize("script", [None, "", 7])
def test_script_path_is_required_to_mint_and_to_admit(tmp_path, script):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        run_token.mint(repo, "ask-1", script)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, script)


def test_wrong_token_does_not_burn_a_live_record(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.redeem(repo, "ask-1", "f" * 32, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_record_with_a_null_script_is_never_admitted(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    path = run_token._record_path(repo, "ask-1")
    path.write_text(path.read_text(encoding="utf-8").replace('"a.mjs"', "null"), encoding="utf-8")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


def test_a_cancelled_gate_before_the_write_mints_nothing(tmp_path):
    repo = _repo(tmp_path)
    gate = run_token.WriteGate()
    assert gate.cancel() == "idle"
    reset = run_token.WRITE_GATE.set(gate)
    try:
        with pytest.raises(run_token.RecordWriteCancelled):
            run_token.mint(repo, "ask-1", "a.mjs")
    finally:
        run_token.WRITE_GATE.reset(reset)
    assert not list(run_token._record_path(repo, "x").parent.glob("*"))


def test_a_gate_cancelled_during_the_write_publishes_nothing_and_removes_its_temp_file(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    gate = run_token.WriteGate()
    monkeypatch.setattr(run_token.os, "fdopen", _cancel_then(gate, run_token.os.fdopen))
    reset = run_token.WRITE_GATE.set(gate)
    try:
        with pytest.raises(run_token.RecordWriteCancelled):
            run_token.mint(repo, "ask-1", "a.mjs")
    finally:
        run_token.WRITE_GATE.reset(reset)
    assert not list(run_token._record_path(repo, "x").parent.glob("*"))


def _cancel_then(gate, real_fdopen):
    def fdopen(*args, **kwargs):
        assert gate.cancel() == "writing"  # the record write is in flight when the caller gives up
        return real_fdopen(*args, **kwargs)

    return fdopen


def test_a_cancel_after_the_commit_does_not_take_and_the_record_stands(tmp_path):
    repo = _repo(tmp_path)
    gate = run_token.WriteGate()
    reset = run_token.WRITE_GATE.set(gate)
    try:
        token = run_token.mint(repo, "ask-1", "a.mjs")
    finally:
        run_token.WRITE_GATE.reset(reset)
    assert gate.cancel() == "committed"
    assert run_token.admits(repo, "ask-1", token, "a.mjs")


def test_remint_after_redeem_redeems_once_more(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", first, "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.redeem(repo, "ask-1", first, "a.mjs")
    assert run_token.redeem(repo, "ask-1", second, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", second, "a.mjs")


def test_redeem_claims_exactly_once_and_consume_spends_the_claim(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.redeem(repo, "ask-1", "f" * 32, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    run_token.consume(repo, "ask-1")
    assert _tokens_dir_files(repo) == ["retired/ask-1.json"]


def test_an_expired_record_admits_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    later = time.time() + run_token.TTL_SECONDS + 5
    monkeypatch.setattr(run_token.time, "time", lambda: later)
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.is_live(repo, "ask-1")


def test_consume_makes_the_token_inert(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.is_live(repo, "ask-1")


def test_live_records_skip_claimed_and_expired(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.mint(repo, "ask-2", "b.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert [r["run_id"] for r in run_token.live_records(repo)] == ["ask-2"]


def test_held_records_list_live_and_claimed_but_not_expired(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.mint(repo, "ask-2", "b.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert sorted(r["run_id"] for r in run_token.held_records(repo)) == ["ask-1", "ask-2"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + run_token.TTL_SECONDS + 5)
    assert [r["run_id"] for r in run_token.held_records(repo)] == ["ask-1"]


def test_racing_redeems_yield_exactly_one_true(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[bool] = []

    def attempt():
        barrier.wait()
        results.append(run_token.redeem(repo, "ask-1", token, "a.mjs"))

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == workers


def test_a_just_exited_thread_claim_is_not_adopted_inside_the_grace(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    won: list = []
    holder = threading.Thread(target=lambda: won.append(run_token.redeem(repo, "ask-1", token, "a.mjs")))
    holder.start()
    holder.join()
    assert won == [True]
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")  # inside the grace: a racer, not a takeover
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + run_token.THREAD_EXIT_GRACE_SECONDS + 1)
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")  # past it the dead holder is taken over


_LANDED = {"sha": "abc123", "rows": [{"row_id": "r1", "outcome": "committed", "sha": "abc123"}], "paths": ["a.py"]}


def _landed_run(repo, run_id="ask-1", script="a.mjs"):
    token = run_token.mint(repo, run_id, script)
    assert run_token.redeem(repo, run_id, token, script)
    run_token.consume(repo, run_id, landed=_LANDED)
    return token


def test_spent_receipt_returns_landed_for_the_matching_token_and_script(tmp_path):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") == _LANDED
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_spent_receipt_refuses_wrong_token_script_and_malformed(tmp_path):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    assert run_token.spent_receipt(repo, "ask-1", "f" * 32, "a.mjs") is None
    assert run_token.spent_receipt(repo, "ask-1", token, "b.mjs") is None
    assert run_token.spent_receipt(repo, "ask-2", token, "a.mjs") is None
    assert run_token.spent_receipt(repo, "../x", token, "a.mjs") is None
    assert run_token.spent_receipt(repo, "ask-1", "short", "a.mjs") is None


def test_an_expired_receipt_answers_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    later = time.time() + run_token.TTL_SECONDS + 5
    monkeypatch.setattr(run_token.time, "time", lambda: later)
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") is None


def test_a_remint_keeps_the_receipt_for_the_landed_token_only(tmp_path):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    fresh = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") is not None
    assert run_token.spent_receipt(repo, "ask-1", fresh, "a.mjs") is None


def test_consume_without_landed_or_without_a_record_writes_no_receipt(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") is None
    run_token.consume(repo, "ask-9", landed=_LANDED)
    run_token.consume(repo, "ask-9")
    assert _tokens_dir_files(repo) == ["retired/ask-1.json"]


def test_a_landed_consume_leaves_a_spent_receipt_and_no_retired_one(tmp_path):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    assert _tokens_dir_files(repo) == ["spent/ask-1.json"]
    assert not run_token.halt_admits(repo, "ask-1", token, "a.mjs")


def _tokens_dir_files(repo):
    base = run_token._record_path(repo, "x").parent
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*.json"))


def test_halt_admits_survives_discard_consume_and_remint(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.discard(repo, "ask-1") is not None
    assert not run_token.admits(repo, "ask-1", first, "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", first, "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", second, "a.mjs")
    third = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", second, "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", third, "a.mjs")
    assert run_token.redeem(repo, "ask-1", third, "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", third, "a.mjs")
    run_token.consume(repo, "ask-1")
    assert run_token.halt_admits(repo, "ask-1", third, "a.mjs")


def test_the_retired_receipt_keeps_every_earlier_held_digest(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs")
    third = run_token.mint(repo, "ask-1", "a.mjs", carry_prior=True)
    fourth = run_token.mint(repo, "ask-1", "a.mjs")
    for held in (first, second, third, fourth):
        assert run_token.halt_admits(repo, "ask-1", held, "a.mjs")
    assert not run_token.admits(repo, "ask-1", third, "a.mjs")
    run_token.discard(repo, "ask-1")
    for held in (first, second, third, fourth):
        assert run_token.halt_admits(repo, "ask-1", held, "a.mjs")


def test_the_retired_chain_is_bounded_and_script_scoped(tmp_path):
    repo = _repo(tmp_path)
    tokens = [run_token.mint(repo, "ask-1", "a.mjs") for _ in range(run_token.MAX_PRIOR + 3)]
    assert run_token.halt_admits(repo, "ask-1", tokens[-1], "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", tokens[-2], "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", tokens[0], "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", tokens[-2], "b.mjs")
    run_token.mint(repo, "ask-2", "a.mjs")
    other = run_token.mint(repo, "ask-2", "b.mjs")
    assert not run_token.halt_admits(repo, "ask-2", other, "a.mjs")


def test_a_carry_prior_remint_keeps_the_params_file_and_a_plain_one_drops_it(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "a.mjs")
    params = chan.params_path(repo, "ask-1")
    params.parent.mkdir(parents=True, exist_ok=True)
    params.write_text("{}", encoding="utf-8")
    run_token.mint(repo, "ask-1", "a.mjs", carry_prior=True)
    assert params.exists()
    run_token.mint(repo, "ask-1", "a.mjs")
    assert not params.exists()
    params.write_text("{}", encoding="utf-8")
    run_token.mint(repo, "ask-1", "b.mjs", carry_prior=True)
    assert not params.exists()


def test_a_plain_remint_removes_stale_land_params(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "a.mjs")
    land = chan.land_params_path(repo, "ask-1")
    land.parent.mkdir(parents=True, exist_ok=True)
    land.write_text("{}", encoding="utf-8")
    run_token.mint(repo, "ask-1", "a.mjs", carry_prior=True)
    assert land.exists()
    run_token.mint(repo, "ask-1", "a.mjs")
    assert not land.exists()


def test_readmit_restores_a_retired_token_that_then_redeems_and_consumes(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.readmit_reviewed(repo, "ask-1", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-1", token, "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    run_token.consume(repo, "ask-1", landed={"sha": "abc"})
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") == {"sha": "abc"}
    assert not run_token.readmit_reviewed(repo, "ask-1", token, "a.mjs")
    assert not run_token.is_live(repo, "ask-1")


def test_readmit_keeps_a_carried_prior_digest_admitted(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs", carry_prior=True)
    run_token.consume(repo, "ask-1")
    assert run_token.readmit_reviewed(repo, "ask-1", first, "a.mjs")
    assert run_token.redeem(repo, "ask-1", first, "a.mjs")
    assert second != first


def test_readmit_refuses_when_a_live_claimed_or_spent_record_exists(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    other = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-1", token, "a.mjs")
    assert run_token.readmit_reviewed(repo, "ask-1", other, "a.mjs") is False
    assert run_token.redeem(repo, "ask-1", other, "a.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-1", other, "a.mjs")
    run_token.consume(repo, "ask-1", landed={"sha": "abc"})
    assert not run_token.readmit_reviewed(repo, "ask-1", other, "a.mjs")


def test_readmit_refuses_stale_mismatched_and_malformed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert not run_token.readmit_reviewed(repo, "ask-1", "0" * 32, "a.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-1", token, "b.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-1", "short", "a.mjs")
    assert not run_token.readmit_reviewed(repo, "../x", token, "a.mjs")
    assert not run_token.readmit_reviewed(repo, "ask-2", token, "a.mjs")
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + run_token.TTL_SECONDS + 5)
    assert not run_token.readmit_reviewed(repo, "ask-1", token, "a.mjs")
    assert not run_token.is_live(repo, "ask-1")


def test_a_first_mint_clears_a_stale_retired_receipt(tmp_path):
    repo = _repo(tmp_path)
    old = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert run_token.halt_admits(repo, "ask-1", old, "a.mjs")
    new = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", old, "a.mjs")
    assert _tokens_dir_files(repo) == ["ask-1.json"]
    assert run_token.halt_admits(repo, "ask-1", new, "a.mjs")


def test_halt_admits_refuses_wrong_token_script_malformed_and_expired(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.discard(repo, "ask-1")
    assert run_token.halt_admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", "f" * 32, "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", token, "b.mjs")
    assert not run_token.halt_admits(repo, "ask-2", token, "a.mjs")
    assert not run_token.halt_admits(repo, "../x", token, "a.mjs")
    assert not run_token.halt_admits(repo, "ask-1", "short", "a.mjs")
    later = time.time() + run_token.TTL_SECONDS + 5
    monkeypatch.setattr(run_token.time, "time", lambda: later)
    assert not run_token.halt_admits(repo, "ask-1", token, "a.mjs")


def test_halt_admits_reads_the_claimed_record_and_claims_nothing(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", token, "a.mjs")
    assert run_token.release(repo, "ask-1", token, "a.mjs")
    assert run_token.halt_admits(repo, "ask-1", token, "a.mjs")


def test_spent_receipts_are_never_listed_as_held(tmp_path):
    repo = _repo(tmp_path)
    _landed_run(repo)
    assert run_token._record_path(repo, "ask-1", spent=True).is_file()
    assert list(run_token.live_records(repo)) == []
    assert run_token.held_records(repo) == []
    assert not run_token.is_live(repo, "ask-1")
    assert run_token.discard(repo, "ask-1") is None


def _plant_legacy(repo, run_id, token, script="a.mjs", **kind):
    import json

    path = run_token._legacy_path(repo, run_id, **kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": run_id, "sha256": run_token._digest(token), "script_path": script, "minted_at": time.time()}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_mint_writes_under_the_settings_home_and_nothing_under_git(tmp_path):
    from coordinator_core.ops.dispatch_emit.engine_record_home import engine_record_dir

    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token._record_path(repo, "ask-1").is_file()
    assert engine_record_dir(repo) in run_token._record_path(repo, "ask-1").parents
    assert not (repo / ".git" / "coordinator-warp-run-tokens").exists()


def test_a_legacy_only_record_is_read_and_redeem_removes_it(tmp_path):
    repo = _repo(tmp_path)
    token = "a" * 32
    legacy = _plant_legacy(repo, "ask-1", token)
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert run_token.is_live(repo, "ask-1")
    assert [r["run_id"] for r in run_token.live_records(repo)] == ["ask-1"]
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert not legacy.exists()
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.redeem(repo, "ask-1", token, "a.mjs")


def test_the_new_root_wins_over_a_legacy_copy_and_a_mint_drops_the_legacy_one(tmp_path):
    repo = _repo(tmp_path)
    old = "b" * 32
    legacy = _plant_legacy(repo, "ask-1", old)
    fresh = run_token.mint(repo, "ask-1", "a.mjs")
    assert not legacy.exists()
    assert run_token.admits(repo, "ask-1", fresh, "a.mjs")
    assert not run_token.admits(repo, "ask-1", old, "a.mjs")


def test_a_stalled_legacy_read_reads_as_absent(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    repo = _repo(tmp_path)
    token = "c" * 32
    _plant_legacy(repo, "ask-1", token)
    monkeypatch.setattr(run_token, "bounded_read", lambda path, deadline_ms: STALLED)
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


def test_command_params_parse_inline_and_heredoc():
    inline = "coordinator-invoke dispatch.terminal_commit '{\"run_id\": \"a\", \"run_token\": \"t\", \"x\": \"it'\\''s\"}'"
    assert run_token.token_params_from_command(inline)["run_id"] == "a"
    heredoc = "coordinator-invoke dispatch.terminal_commit --params-file - <<'JSON'\n{\"run_id\": \"b\", \"run_token\": \"t\"}\nJSON"
    assert run_token.token_params_from_command(heredoc)["run_id"] == "b"
    land = "coordinator-invoke dispatch.land_run --params-file - <<'JSON'\n{\"run_id\": \"c\", \"run_token\": \"t\"}\nJSON"
    assert run_token.token_params_from_command(land)["run_id"] == "c"


def test_redeem_claim_returns_the_identity_to_exactly_one_caller(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.redeem_claim(repo, "ask-1", "f" * 32, "a.mjs") is None
    identity = run_token.redeem_claim(repo, "ask-1", token, "a.mjs")
    assert set(identity) == {"claimed_at", "pid", "thread"}
    assert identity["pid"] == os.getpid() and identity["thread"] == threading.get_ident()
    assert run_token.redeem_claim(repo, "ask-1", token, "a.mjs") is None
    assert run_token.holds_claim(repo, "ask-1", token, "a.mjs", identity)


def test_redeem_claim_adopts_a_dead_holder_and_holds_claim_follows_the_adopter(tmp_path, monkeypatch):
    import json

    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    first = run_token.redeem_claim(repo, "ask-1", token, "a.mjs")
    path = _claimed_path(repo)
    record = json.loads(path.read_text(encoding="utf-8"))
    other = {**first, "pid": first["pid"] + 1}
    path.write_text(json.dumps({**record, **other}), encoding="utf-8")
    assert run_token.holds_claim(repo, "ask-1", token, "a.mjs", other)
    monkeypatch.setattr(run_token, "_holder_alive", lambda pid: False)
    mine = run_token.redeem_claim(repo, "ask-1", token, "a.mjs")
    assert mine is not None and mine["pid"] == os.getpid()
    assert run_token.holds_claim(repo, "ask-1", token, "a.mjs", mine)
    assert not run_token.holds_claim(repo, "ask-1", token, "a.mjs", other)


def test_holds_claim_is_false_without_a_claim_or_for_other_credentials(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    ghost = {"claimed_at": 1.0, "pid": 1, "thread": 1}
    assert not run_token.holds_claim(repo, "ask-1", token, "a.mjs", ghost)
    identity = run_token.redeem_claim(repo, "ask-1", token, "a.mjs")
    assert not run_token.holds_claim(repo, "ask-1", "f" * 32, "a.mjs", identity)
    assert not run_token.holds_claim(repo, "ask-1", token, "b.mjs", identity)
    assert not run_token.holds_claim(repo, "ask-1", token, "a.mjs", None)
    run_token.consume(repo, "ask-1")
    assert not run_token.holds_claim(repo, "ask-1", token, "a.mjs", identity)


def test_a_live_holder_past_its_recorded_deadline_is_adopted(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    first = run_token.redeem_claim(repo, "ask-1", token, "a.mjs", budget_secs=30)
    assert first is not None and first["deadline"] == pytest.approx(first["claimed_at"] + 30)
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")  # in budget: still held
    assert run_token.redeem_claim(repo, "ask-1", token, "a.mjs", budget_secs=30) is None
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + 31)
    assert not run_token.claim_pending(repo, "ask-1", token, "a.mjs")
    second = run_token.redeem_claim(repo, "ask-1", token, "a.mjs", budget_secs=30)
    assert second is not None and second["deadline"] > first["deadline"]
    assert not run_token.holds_claim(repo, "ask-1", token, "a.mjs", first)
    assert run_token.holds_claim(repo, "ask-1", token, "a.mjs", second)


def test_a_claim_without_a_deadline_is_not_expired_by_age(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    claim = run_token.redeem_claim(repo, "ask-1", token, "a.mjs")
    assert "deadline" not in claim
    real = time.time()
    monkeypatch.setattr(run_token.time, "time", lambda: real + 10_000)
    assert run_token.claim_pending(repo, "ask-1", token, "a.mjs")
