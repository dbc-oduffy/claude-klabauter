"""run_token escrow: written at mint time by the launcher, peeked read-only, dead with the record."""

from __future__ import annotations

import json
import time

from coordinator_core.ops.dispatch_emit import run_token


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _escrow_file(repo, run_id="ask-1"):
    return repo / ".git" / "coordinator-warp-run-tokens" / "escrow" / f"{run_id}.json"


def test_escrowed_returns_the_token_for_a_fresh_record(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.escrowed(repo, "ask-1") is None
    assert run_token.escrow(repo, "ask-1", token)
    assert run_token.escrowed(repo, "ask-1") == token
    assert run_token.escrowed(repo, "ask-1") == token


def test_escrow_refuses_no_git_dir_and_malformed_id(tmp_path):
    assert not run_token.escrow(tmp_path, "ask-1", "a" * 32)
    repo = _repo(tmp_path)
    assert not run_token.escrow(repo, "../x", "a" * 32)
    assert run_token.escrowed(repo, "../x") is None


def test_claimed_run_does_not_admit_and_release_restores(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.escrow(repo, "ask-1", token)
    assert run_token.redeem(repo, "ask-1", token, "a.mjs")
    assert run_token.escrowed(repo, "ask-1") is None
    assert run_token.release(repo, "ask-1", token, "a.mjs")
    assert run_token.escrowed(repo, "ask-1") == token


def test_consume_and_discard_remove_the_escrow(tmp_path):
    repo = _repo(tmp_path)
    for remover in (lambda: run_token.consume(repo, "ask-1"), lambda: run_token.discard(repo, "ask-1")):
        token = run_token.mint(repo, "ask-1", "a.mjs")
        run_token.escrow(repo, "ask-1", token)
        assert _escrow_file(repo).exists()
        remover()
        assert not _escrow_file(repo).exists()
        assert run_token.escrowed(repo, "ask-1") is None


def test_mint_unlinks_a_stale_escrow(tmp_path):
    repo = _repo(tmp_path)
    old = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.escrow(repo, "ask-1", old)
    new = run_token.mint(repo, "ask-1", "a.mjs")
    assert not _escrow_file(repo).exists()
    assert run_token.escrowed(repo, "ask-1") is None
    run_token.escrow(repo, "ask-1", new)
    assert run_token.escrowed(repo, "ask-1") == new


def test_aged_record_and_digest_mismatch_and_malformed_token_refuse(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.escrow(repo, "ask-1", "b" * 32)
    assert run_token.escrowed(repo, "ask-1") is None
    run_token.escrow(repo, "ask-1", "NOT-A-TOKEN")
    assert run_token.escrowed(repo, "ask-1") is None
    run_token.escrow(repo, "ask-1", token)
    assert run_token.escrowed(repo, "ask-1") == token
    live = repo / ".git" / "coordinator-warp-run-tokens" / "ask-1.json"
    record = json.loads(live.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - run_token.TTL_SECONDS - 10
    live.write_text(json.dumps(record), encoding="utf-8")
    assert run_token.escrowed(repo, "ask-1") is None


def test_escrow_files_are_not_live_or_held_records(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.escrow(repo, "ask-1", token)
    assert [r["run_id"] for r in run_token.live_records(repo)] == ["ask-1"]
    run_token.consume(repo, "ask-1")
    run_token.escrow(repo, "ask-1", token)
    assert list(run_token.live_records(repo)) == []
    assert run_token.held_records(repo) == []


def test_sweep_expired_escrows_unlinks_only_the_aged(tmp_path):
    repo = _repo(tmp_path)
    for rid in ("old", "new"):
        run_token.escrow(repo, rid, "c" * 32)
    now = time.time() + run_token.TTL_SECONDS + 5
    path = _escrow_file(repo, "new")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = now
    path.write_text(json.dumps(record), encoding="utf-8")
    assert run_token.sweep_expired_escrows(repo, now=now) == 1
    assert not _escrow_file(repo, "old").exists()
    assert _escrow_file(repo, "new").exists()
    assert run_token.sweep_expired_escrows(tmp_path / "nowhere") == 0
