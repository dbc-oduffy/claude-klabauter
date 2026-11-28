"""dispatch.land_run at default arguments: a call whose run-token claim a later call adopted past the op
budget commits nothing, replies ``refused="run-token-adopted"`` and leaves the adopter's claim standing;
a call that still holds its claim lands and spends the token."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    RUN, _git, _land, repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _adopt_after_redeem(monkeypatch) -> list:
    """After the real `redeem_claim`, rewrite the claimed record as a later call's adoption would."""
    real = run_token.redeem_claim
    seen: list = []

    def redeem(root, run_id, token, script_path, **kw):
        won = real(root, run_id, token, script_path, **kw)
        if won is not None:
            claimed = run_token._record_path(root, run_id, claimed=True)
            record = json.loads(claimed.read_text(encoding="utf-8"))
            record.update(pid=int(record["pid"]) + 1, claimed_at=float(record["claimed_at"]) + 1.0)
            claimed.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            seen.append(record)
        return won

    monkeypatch.setattr(run_token, "redeem_claim", redeem)
    return seen


def test_a_call_whose_claim_was_adopted_commits_nothing_and_leaves_the_adopters_claim(repo, monkeypatch):
    head = _git(repo, "rev-parse", "HEAD").strip()
    seen = _adopt_after_redeem(monkeypatch)

    out = _land(repo, hand_commit=False)

    assert seen, "the claim was never redeemed"
    assert out["committed"] is False, out
    assert out["refused"] == "run-token-adopted", out
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    claimed = run_token._record_path(repo, RUN, claimed=True)
    assert claimed.exists() and json.loads(claimed.read_text(encoding="utf-8")) == seen[0]
    assert not run_token.is_live(repo, RUN)


def test_a_call_that_holds_its_claim_lands_and_spends_the_token(repo):
    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert out.get("refused") is None
    assert not run_token._record_path(repo, RUN, claimed=True).exists()


def test_owner_lands_identical_request_after_claim_denial(repo, monkeypatch):
    from pathlib import Path
    from coordinator_core.ops.dispatch_emit import land_run

    head = _git(repo, "rev-parse", "HEAD").strip()
    handler = land_run._handler
    issued = {}

    def restricted(params, repo_root):
        issued.update(params)
        claim = run_token._record_path(repo, RUN, claimed=True)
        original = run_token.os.open

        def restricted_open(path, *args, **kwargs):
            if Path(path) == claim:
                raise PermissionError(path)
            return original(path, *args, **kwargs)

        with monkeypatch.context() as restriction:
            restriction.setattr(run_token.os, "open", restricted_open)
            return handler(params, repo_root=repo_root)

    with monkeypatch.context() as worker:
        worker.setattr(land_run, "_handler", restricted)
        refusal = _land(repo, hand_commit=False)
    assert refusal["refused"] == "run-token-claim-denied"
    assert head == _git(repo, "rev-parse", "HEAD").strip()
    landed = handler(issued, repo_root=repo / ".git")
    assert landed["committed"] and landed["sha"] == _git(repo, "rev-parse", "HEAD").strip()
    duplicate = handler(issued, repo_root=repo / ".git")
    assert duplicate["sha"] == landed["sha"] and duplicate["already_landed"]
