"""A refusal that lands nothing releases the run token: a ledger-unmet refusal is followed by a
byte-exact retry with the same token, and the retry is admitted."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
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
