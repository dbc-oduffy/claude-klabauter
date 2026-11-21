"""A closed run and an abandoned-minted closure sweep leave no engine-written state dirty in git status."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.install.runtime_ignore_apply import apply_runtime_ignore
from coordinator_core.ops.dispatch_emit import abandoned_minted_close as amc
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED, MINTED_STALE_SECONDS

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOW = time.time() + 2 * MINTED_STALE_SECONDS
RUN = "ask-20261101T000000-aaaaaa"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _dirt(repo):
    return _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _engine_state(repo):
    """An abandoned-minted run dir plus the receipt and evidence shapes engine runs write."""
    rdir = record_dir(repo, RUN)
    rdir.mkdir(parents=True)
    halted_run.write_halt_marker(rdir, halted=ABANDONED_AT_MINTED, sizing_rel=None, reason="abandoned")
    (rdir / "manifest.json").write_text(json.dumps({"head_sha": "a" * 40}), encoding="utf-8")
    (rdir / "receipts").mkdir()
    (rdir / "receipts" / "r.json").write_text("{}", encoding="utf-8")
    proof = repo / "state" / "warp" / "clean-clone-proofs" / "p.json"
    proof.parent.mkdir(parents=True)
    proof.write_text("{}", encoding="utf-8")
    runs = repo / "state" / "test-runs" / "t.json"
    runs.parent.mkdir(parents=True)
    runs.write_text("{}", encoding="utf-8")
    ev = repo / "state" / "queue-grind" / "q1" / "evidence" / "e.md"
    ev.parent.mkdir(parents=True)
    ev.write_text("e", encoding="utf-8")
    return rdir


def test_closure_sweep_leaves_no_dirt_once_runtime_ignore_applied(repo):
    rdir = _engine_state(repo)
    apply_runtime_ignore(repo)
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    assert amc.close_abandoned(repo, NOW) == [RUN]
    marker = json.loads((rdir / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "closed"
    assert _dirt(repo) == []


def test_same_engine_state_is_dirty_without_the_ignore_rules(repo):
    _engine_state(repo)
    assert amc.close_abandoned(repo, NOW) == [RUN]
    dirt = "\n".join(_dirt(repo))
    for part in ("state/warp/", "state/test-runs/", "state/queue-grind/"):
        assert part in dirt
