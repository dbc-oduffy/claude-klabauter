"""run_token: mint, the atomic admit, redeem, consume, sizing path normalising, and which sizings an in-flight run owns."""

from __future__ import annotations

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
    assert not run_token.bind_sizing(repo, "ask-1", "state/sizings/unrelated.yaml")


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
    return repo / ".git" / "coordinator-warp-run-tokens" / "claimed" / "ask-1.json"


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


def test_a_same_process_claim_is_adopted_once_its_thread_exits(tmp_path):
    import threading

    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    won = []
    holder = threading.Thread(target=lambda: won.append(run_token.redeem(repo, "ask-1", token, "a.mjs")))
    holder.start()
    holder.join()
    assert won == [True]
    assert not run_token.redeem(repo, "ask-1", "f" * 32, "a.mjs")
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
    live = repo / ".git" / "coordinator-warp-run-tokens" / "ask-1.json"
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
    path = repo / ".git" / "coordinator-warp-run-tokens" / "ask-1.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"a.mjs"', "null"), encoding="utf-8")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


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


def test_a_remint_removes_the_receipt(tmp_path):
    repo = _repo(tmp_path)
    token = _landed_run(repo)
    run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.spent_receipt(repo, "ask-1", token, "a.mjs") is None


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
    base = repo / ".git" / "coordinator-warp-run-tokens"
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
    assert (repo / ".git" / "coordinator-warp-run-tokens" / "spent" / "ask-1.json").is_file()
    assert list(run_token.live_records(repo)) == []
    assert run_token.held_records(repo) == []
    assert not run_token.is_live(repo, "ask-1")
    assert run_token.discard(repo, "ask-1") is None


def test_command_params_parse_inline_and_heredoc():
    inline = "coordinator-invoke dispatch.terminal_commit '{\"run_id\": \"a\", \"run_token\": \"t\", \"x\": \"it'\\''s\"}'"
    assert run_token.token_params_from_command(inline)["run_id"] == "a"
    heredoc = "coordinator-invoke dispatch.terminal_commit --params-file - <<'JSON'\n{\"run_id\": \"b\", \"run_token\": \"t\"}\nJSON"
    assert run_token.token_params_from_command(heredoc)["run_id"] == "b"
    land = "coordinator-invoke dispatch.land_run --params-file - <<'JSON'\n{\"run_id\": \"c\", \"run_token\": \"t\"}\nJSON"
    assert run_token.token_params_from_command(land)["run_id"] == "c"
