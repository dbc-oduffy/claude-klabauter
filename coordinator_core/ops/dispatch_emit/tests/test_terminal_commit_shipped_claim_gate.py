"""dispatch.terminal_commit on an ask run refuses, before commit_v2, a delivery note whose "What shipped"
list names a file the landing does not carry; a backed claim lands and rides the reply."""

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


def _call(root, token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    if token:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_shipped_file_the_landing_does_not_carry_refuses_with_head_unmoved(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, "ghost.py", "x = 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `ghost.py`\n")
    token = run_token.mint(repo, RUN, "run.mjs")
    before = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, token)

    assert out["committed"] is False
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED
    assert out["shipped_claims"]["unbacked"]
    assert _git(repo, "rev-parse", "HEAD") == before
    assert run_token.admits(repo, RUN, token, "run.mjs") is True


def test_a_landed_shipped_file_lands_and_lists_the_checked_claim(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`\n")

    out = _call(repo)

    assert out["committed"] is True, out
    assert out["shipped_claims"]["checked"] == 1 and not out["shipped_claims"]["unbacked"]


def test_a_note_with_no_shipped_heading_lands_unchanged(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, NOTE, "# Delivery\n\nSee `a.py`.\n")

    out = _call(repo)

    assert out["committed"] is True, out
    assert "shipped_claims" not in out


def _stamp_withhold(root, path, reason):
    import json

    _write(root, f"state/warp/{RUN}/withhold.jsonl", json.dumps({"path": path, "reason": reason, "at": "t"}) + "\n")


def test_a_halt_record_only_stamped_product_path_lands_and_is_not_refused(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`\n")
    _stamp_withhold(repo, "a.py", "halt-record-only")

    out = _call(repo)

    assert out["committed"] is True, out
    assert "a.py" in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert not out["shipped_claims"]["unbacked"]


def test_a_content_withheld_shipped_path_still_refuses(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, "b.py", "def b_tool():\n    return 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`\n- `b.py`\n")
    _stamp_withhold(repo, "b.py", "peer owns this file")

    out = _call(repo)

    assert out["committed"] is False, out
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED


def test_a_backticked_undeclared_directory_with_untracked_files_lands(repo):  # noqa: F811
    _write(repo, "a.py", "def a_tool():\n    return 1\n")
    _write(repo, "stray/other.yaml", "x: 1\n")
    _write(repo, NOTE, "# Delivery\n\n## What shipped\n\n- `a.py`: a test redirects into `stray`\n")

    out = _call(repo)

    assert out["committed"] is True, out
    assert not out["shipped_claims"]["unbacked"]
