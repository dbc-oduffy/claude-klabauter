"""A terminal commit that lands nothing releases the run token in every mode: the same token is
admitted again after a refusal, a non-landing commit_v2 reply, or an exception; a landed sha
consumes it."""

from __future__ import annotations

import importlib

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, grind_commit_contract, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN,
    _git,
    _verdict,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _call(root, verdict: dict, token: str) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": verdict, "run_id": RUN, "run_token": token,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_ledger_unmet_refusal_releases_the_token_and_the_retry_is_admitted(repo):  # noqa: F811
    token = run_token.mint(repo, RUN, "run.mjs")
    before = _git(repo, "rev-parse", "HEAD")
    refused = _call(repo, _verdict({}), token)
    assert refused["committed"] is False and refused["refused"] == "ledger-unmet"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert run_token.admits(repo, RUN, token, "run.mjs") is True

    retried = _call(repo, _verdict({"essential": False}), token)
    assert retried["committed"] is True, retried
    assert run_token.admits(repo, RUN, token, "run.mjs") is False


def _stub_commit_v2(monkeypatch, behavior):
    real = terminal_commit.reentrant_dispatch

    def fake(op, params, *args, **kwargs):
        if op == "ceremony.commit_v2":
            return behavior()
        return real(op, params, *args, **kwargs)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", fake)


def _raise():
    raise RuntimeError("commit_v2 blew up")


@pytest.mark.parametrize(
    "behavior",
    [
        lambda: {"committed": False, "sha": None, "error": "nothing to commit"},
        lambda: {"committed": True},
        lambda: {"committed": True, "sha": None},
    ],
    ids=["committed-false", "committed-no-sha", "committed-null-sha"],
)
def test_a_commit_v2_reply_with_no_landed_sha_leaves_the_token_admitted(repo, monkeypatch, behavior):  # noqa: F811
    token = run_token.mint(repo, RUN, "run.mjs")
    _stub_commit_v2(monkeypatch, behavior)
    _call(repo, _verdict({"essential": False}), token)
    assert run_token.admits(repo, RUN, token, "run.mjs") is True


def test_an_exception_inside_the_inner_commit_leaves_the_token_admitted(repo, monkeypatch):  # noqa: F811
    token = run_token.mint(repo, RUN, "run.mjs")
    _stub_commit_v2(monkeypatch, _raise)
    with pytest.raises(RuntimeError):
        _call(repo, _verdict({"essential": False}), token)
    assert run_token.admits(repo, RUN, token, "run.mjs") is True


def test_a_landed_sha_consumes_the_token(repo, monkeypatch):  # noqa: F811
    token = run_token.mint(repo, RUN, "run.mjs")
    _stub_commit_v2(monkeypatch, lambda: {"committed": True, "sha": "a" * 40})
    _call(repo, _verdict({"essential": False}), token)
    assert run_token.admits(repo, RUN, token, "run.mjs") is False


_MODE_CASES = {
    "halted": ("coordinator_core.ops.dispatch_emit.halted_record_commit", "commit_halted_record", "_land"),
    "grind": ("coordinator_core.ops.dispatch_emit.land_run_grind", "commit_grind_rows", "_land"),
    "fanout": ("coordinator_core.ops.dispatch_emit.roadmap_fanout_commit", "commit_fanout_record", "_land_and_fan_out"),
}


@pytest.mark.parametrize("mode", sorted(_MODE_CASES))
@pytest.mark.parametrize("outcome", ["not-committed", "raises"])
def test_a_non_landing_mode_commit_leaves_the_token_admitted(repo, monkeypatch, mode, outcome):  # noqa: F811
    module_name, fn_name, land_name = _MODE_CASES[mode]
    module = importlib.import_module(module_name)

    def fake_land(*args, **kwargs):
        if outcome == "raises":
            raise RuntimeError("land blew up")
        return {"committed": False, "sha": None, "error": "nothing landed"}

    monkeypatch.setattr(module, land_name, fake_land)
    token = run_token.mint(repo, RUN, "run.mjs")
    params = {"script_path": "run.mjs", "run_id": RUN, "run_token": token}
    if mode == "halted":
        params[ask_contract.HALTED_RECORD_PARAM] = {}
    elif mode == "grind":
        params[grind_commit_contract.GRIND_COMMIT_PARAM] = {"profile": "p", "rows": []}
    else:
        params[ask_contract.FANOUT_RECORD_PARAM] = {}
    fn = getattr(module, fn_name)
    if outcome == "raises":
        with pytest.raises(RuntimeError):
            fn(repo, repo / ".git", params)
    else:
        assert fn(repo, repo / ".git", params)["committed"] is False
    assert run_token.admits(repo, RUN, token, "run.mjs") is True
