"""terminal_commit_reconcile: the HEAD ancestry check, the spent-receipt reply and the clause tokens."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_reconcile as tcr
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENV = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def _git(repo, *args):
    return subprocess.run(
        ["git", *_ENV, *args], cwd=repo, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    a = _commit(tmp_path, "a")
    b = _commit(tmp_path, "b")
    _git(tmp_path, "checkout", "-q", "-b", "side", a)
    side = _commit(tmp_path, "s")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path, a, b, side


def test_landed_on_head_head_ancestor_offbranch_unknown(repo):
    root, a, b, side = repo
    assert tcr.landed_on_head(root, b)
    assert tcr.landed_on_head(root, a)
    assert not tcr.landed_on_head(root, side)
    assert not tcr.landed_on_head(root, "0" * 40)
    assert not tcr.landed_on_head(root, "")


def test_landed_on_head_false_without_head(tmp_path):
    assert not tcr.landed_on_head(tmp_path, "a" * 40)


def _spent(root, sha):
    token = run_token.mint(root, "ask-1", "s.mjs")
    assert run_token.redeem(root, "ask-1", token, "s.mjs")
    run_token.consume(root, "ask-1", landed={"sha": sha, "rows": ["C1"], "paths": ["a"]})
    return token


def test_already_landed_reply_on_head(repo):
    root, a, b, _side = repo
    token = _spent(root, a)
    assert tcr.already_landed_reply(root, "ask-1", token, "s.mjs") == {
        "committed": True, "sha": a, ALREADY_LANDED_KEY: True, "rows": ["C1"], "paths": ["a"],
        "error": None, "refused": None, "close": None,
    }


def test_already_landed_reply_none_off_head(repo):
    root, _a, _b, side = repo
    token = _spent(root, side)
    assert tcr.already_landed_reply(root, "ask-1", token, "s.mjs") is None


def test_already_landed_reply_none_for_wrong_token_or_script(repo):
    root, a, _b, _side = repo
    token = _spent(root, a)
    assert tcr.already_landed_reply(root, "ask-1", "wrong", "s.mjs") is None
    assert tcr.already_landed_reply(root, "ask-1", token, "other.mjs") is None


def test_already_landed_reply_none_without_receipt(repo):
    root, _a, _b, _side = repo
    token = run_token.mint(root, "ask-1", "s.mjs")
    assert tcr.already_landed_reply(root, "ask-1", token, "s.mjs") is None


def test_clause_carries_pinned_tokens():
    for needle in ("-32004", "reconcile_command", "finished", "exactly once", "already_landed"):
        assert needle in tcr.TERMINAL_COMMIT_RECONCILE_CLAUSE


def test_landed_on_head_spawns_nothing(repo, monkeypatch):
    root, a, _b, side = repo
    spawns = []

    def boom(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert tcr.landed_on_head(root, a)
    assert not tcr.landed_on_head(root, side)
    assert spawns == []
