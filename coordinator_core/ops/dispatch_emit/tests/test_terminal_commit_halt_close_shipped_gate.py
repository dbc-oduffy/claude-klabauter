"""A terminal_commit retry that closes an already-landed halt still gates the delivery note: it lands
nothing when the note claims a file the landing does not carry, and lands whole when every claim is backed."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_token, shipped_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOTE = f"{DELIVERY_NOTE_DIR}/{RUN}.md"


@pytest.fixture
def halt_closed(monkeypatch):
    """The ledger gate answers as it does after a refused first fire: the halt's own landing is closed."""
    real = terminal_commit._ledger_gate

    def gate(*args, **kwargs):
        unmet, ledger, open_rows = real(*args, **kwargs)
        assert unmet is None, unmet
        closed = {"committed": True, "sha": "0" * 40, "already_landed": True, "paths": [], "since_halt": {}}
        return closed, ledger, open_rows

    monkeypatch.setattr(terminal_commit, "_ledger_gate", gate)


def _call(root, token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    if token:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_retry_after_a_shipped_claim_refusal_lands_nothing(repo, halt_closed):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, "ghost.py", "x = 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`\n- `ghost.py`\n")
    token = run_token.mint(repo, RUN, "run.mjs")
    before = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, token)

    assert out["committed"] is False
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED
    assert _git(repo, "rev-parse", "HEAD") == before
    assert NOTE not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_a_retry_whose_note_claims_are_all_backed_lands_the_whole_set(repo, halt_closed):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`\n")

    out = _call(repo)

    assert out["committed"] is True, out
    landed = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "a.py" in landed and NOTE in landed
    assert not out["shipped_claims"]["unbacked"]
