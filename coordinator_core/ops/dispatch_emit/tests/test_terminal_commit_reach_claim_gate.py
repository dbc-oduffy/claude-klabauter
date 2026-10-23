"""dispatch.terminal_commit on an ask run refuses, before commit_v2, a delivery note whose "X reaches Y"
claim the landing tree does not support; a supported or undecidable claim lands and rides the reply."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import reach_claims, run_token, terminal_commit
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
def tree(repo):  # noqa: F811
    _write(repo, "lib_wired.py", "def wired_fn():\n    return 1\n")
    _write(repo, "lib_apart.py", "def apart_fn():\n    return 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "libs")
    _write(repo, "a.py", "from lib_wired import wired_fn\n\n\ndef a_tool():\n    return wired_fn()\n")
    return repo


def _call(root, token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    if token:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_an_unreachable_claim_refuses_with_head_unmoved_and_the_token_released(tree):
    _write(tree, NOTE, "# Delivery\n\n`a_tool` reaches `apart_fn`.\n")
    token = run_token.mint(tree, RUN, "run.mjs")
    before = _git(tree, "rev-parse", "HEAD")

    out = _call(tree, token)

    assert out["committed"] is False
    assert out["refused"] == reach_claims.REACH_CLAIM_REFUSED
    assert out["reach_claims"]["unreachable"] and out["reach_claims"]["checked"] == 1
    assert _git(tree, "rev-parse", "HEAD") == before
    assert run_token.admits(tree, RUN, token, "run.mjs") is True


def test_the_wired_twin_lands_and_lists_the_checked_claim(tree):
    _write(tree, NOTE, "# Delivery\n\n`a_tool` reaches `wired_fn`.\n")

    out = _call(tree)

    assert out["committed"] is True, out
    assert out["reach_claims"] == {"checked": 1, "unreachable": [], "undecidable": []}


def test_an_undecidable_claim_lands_and_is_listed(tree):
    _write(tree, NOTE, "# Delivery\n\n`a_tool` reaches `ghost_fn`.\n")

    out = _call(tree)

    assert out["committed"] is True, out
    assert len(out["reach_claims"]["undecidable"]) == 1 and not out["reach_claims"]["unreachable"]


def test_a_note_with_no_claim_lands_unchanged(tree):
    _write(tree, NOTE, "# Delivery\n\nNothing to check here.\n")

    out = _call(tree)

    assert out["committed"] is True, out
    assert "reach_claims" not in out


def test_the_appended_verdicts_section_is_never_read_as_a_claim(tree):
    _write(tree, NOTE, "# Delivery\n\nplain.\n\n## Verdicts\n\n`a_tool` reaches `apart_fn`.\n")

    out = _call(tree)

    assert out["committed"] is True, out
    assert "reach_claims" not in out
