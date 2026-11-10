"""The terminal_commit reply ends with its outcome keys, after any long undeclared_dirty list."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "wave-review.md").write_text("# review\n", encoding="utf-8")
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def test_landed_reply_ends_with_committed_then_sha_after_undeclared_dirty(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    for i in range(5):
        (repo / f"stray{i}.py").write_text("x\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert out.get("undeclared_dirty"), out
    assert list(out)[-2:] == ["committed", "sha"]
    assert list(out).index("undeclared_dirty") < list(out).index("committed")


def test_outcome_keys_move_last_and_values_are_unchanged():
    reply = {"committed": True, "sha": "abc", "undeclared_dirty": ["x"], "stranded": {}}
    out = terminal_commit._outcome_keys_last(reply)
    assert list(out) == ["undeclared_dirty", "stranded", "committed", "sha"]
    assert out == reply


def test_refused_and_nothing_to_commit_replies_end_with_outcome_keys():
    refused = terminal_commit._outcome_keys_last({"committed": False, "refused": "x", "error": "e"})
    assert list(refused)[-1] == "committed"
    empty = terminal_commit._outcome_keys_last(
        {"committed": False, "nothing_to_commit": True, "sha": "s", "stranded": {}}
    )
    assert list(empty) == ["stranded", "nothing_to_commit", "committed", "sha"]


def test_a_non_dict_reply_passes_through():
    assert terminal_commit._outcome_keys_last(None) is None
