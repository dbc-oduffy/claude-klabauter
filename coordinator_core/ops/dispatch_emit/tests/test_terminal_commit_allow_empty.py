"""dispatch.terminal_commit honours allow_empty from the params file so an already-landed run closes.

The fixture's run-bookkeeping files always carry a delta, so the product ``ceremony.commit_v2`` call
is narrowed to the already-landed path: the real op then answers ``nothing_to_commit`` without
``allow_empty`` and lands one tree-equal marker commit with it.
"""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def landed(tmp_path, monkeypatch):
    """A run whose declared write (a.py) is already committed at HEAD; returns (repo, script, seen)."""
    repo, script = driver.build_fixture(tmp_path)
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "hand-committed delivery")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    noop = tmp_path / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    real = terminal_commit.reentrant_dispatch
    seen: list = []

    def no_delta_commit_v2(op, params, repo_root=None):
        if op != "ceremony.commit_v2" or params["message"].startswith("mark "):
            return real(op, params, repo_root=repo_root)
        seen.append(params)
        only_landed = {
            k: v for k, v in params.items() if k not in ("force_ignored", "expected_content_sha256")
        }
        only_landed["paths"] = ["a.py"]
        return real(op, only_landed, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", no_delta_commit_v2)
    return repo, script, seen


def _params_file(tmp_path, script: str, **extra) -> str:
    params = {
        "script_path": script,
        "incomplete_chunks": [],
        "session_id": driver.SESSION_ID,
        "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        **extra,
    }
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return str(path)


def _fire(repo, tmp_path, script, **extra) -> dict:
    return terminal_commit._handler(
        {"task_output_path": _params_file(tmp_path, script, **extra)}, repo_root=repo / ".git"
    )


def test_params_file_allow_empty_closes_an_already_landed_run(landed, tmp_path):
    repo, script, seen = landed
    head = _git(repo, "rev-parse", "HEAD")
    out = _fire(repo, tmp_path, script, allow_empty=True)
    assert seen[0]["allow_empty"] is True
    assert out.get("committed") is True and out.get("sha")
    assert "refused" not in out
    assert out["sha"] != head
    assert _git(repo, "rev-parse", f"{out['sha']}^{{tree}}") == _git(
        repo, "rev-parse", f"{head}^{{tree}}"
    )
    assert out["review_stamp"] == "minted"
    assert out.get("rows_coded") and any(out["rows_coded"].values()), json.dumps(out, default=str)


def test_without_allow_empty_the_same_run_replies_nothing_to_commit(landed, tmp_path):
    repo, script, seen = landed
    out = _fire(repo, tmp_path, script)
    assert "allow_empty" not in seen[0]
    assert out.get("committed") is False
    assert out.get("nothing_to_commit") is True


def test_non_boolean_allow_empty_is_refused_before_any_write(landed, tmp_path):
    repo, script, seen = landed
    head = _git(repo, "rev-parse", "HEAD")
    out = _fire(repo, tmp_path, script, allow_empty="yes")
    assert "allow_empty" in out.get("error", "")
    assert not seen
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain", "--", "a.py", "docs") == ""
