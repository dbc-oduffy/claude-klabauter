"""run_token.session_holds_run: whether a session holds a live or claimed run record in a repo."""

from __future__ import annotations

import json
import time

from coordinator_core.ops.dispatch_emit import run_token

SCRIPT = "scratch/warp/r.workflow.mjs"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _age(repo, run_id, *, claimed=False, seconds):
    path = run_token._record_path(repo, run_id, claimed=claimed)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - seconds
    path.write_text(json.dumps(record), encoding="utf-8")


def test_own_live_record_holds(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert run_token.session_holds_run(repo, "s1")


def test_fresh_claimed_record_holds(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert run_token.redeem(repo, "r1", token, SCRIPT)
    assert run_token.session_holds_run(repo, "s1")


def test_claimed_record_past_ttl_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert run_token.redeem(repo, "r1", token, SCRIPT)
    _age(repo, "r1", claimed=True, seconds=run_token.TTL_SECONDS + 60)
    assert not run_token.session_holds_run(repo, "s1")


def test_other_sessions_record_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert not run_token.session_holds_run(repo, "s2")


def test_expired_record_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    _age(repo, "r1", seconds=run_token.TTL_SECONDS + 60)
    assert not run_token.session_holds_run(repo, "s1")


def test_spent_receipt_only_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert run_token.redeem(repo, "r1", token, SCRIPT)
    run_token.consume(repo, "r1", landed={"sha": "abc", "rows": [], "paths": []})
    assert run_token.spent_receipt(repo, "r1", token, SCRIPT) is not None
    assert not run_token.session_holds_run(repo, "s1")


def test_empty_session_id_does_not_hold(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "r1", SCRIPT, session_id="")
    assert not run_token.session_holds_run(repo, "")
    assert not run_token.session_holds_run(repo, None)


def test_non_repo_path_does_not_hold(tmp_path):
    assert not run_token.session_holds_run(tmp_path / "nowhere", "s1")
    assert not run_token.session_holds_run(tmp_path, "s1")
