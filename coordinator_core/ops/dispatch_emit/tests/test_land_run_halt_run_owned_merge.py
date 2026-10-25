"""land_run halted mode over an open merge: a run-owned merge is left open with its routes named."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.ops import merge_branch_into_workstream as mbw
from coordinator_core.ops.dispatch_emit import land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261015T000000-aaaaaa"
HALT = {"halted": "refusal", "reason": "estimate L needs a PM touchpoint"}
REFIRE = {
    "kind": "refire", "op": "engine-ask", "call": "Workflow({})",
    "resume": {"run_id": RUN, "sizing": "s.yaml", "halted": "refusal", "stage": {}},
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "base.txt", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _git(tmp_path, "checkout", "-q", "-b", "theirs")
    _write(tmp_path, "theirs.txt", "theirs\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "theirs")
    _git(tmp_path, "checkout", "-q", "main")
    _write(tmp_path, "ours.txt", "ours\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "ours")
    for name in ("ask.md", "run.json"):
        _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/{name}", f"{name}\n")
    return tmp_path


def _call(root: Path, halted: dict = HALT) -> dict:
    return land_run_halt.land(root, root, {"run_id": RUN, HALTED_RECORD_PARAM: halted})


def _merge_head(root: Path) -> Path:
    return resolve_git_dir(root) / "MERGE_HEAD"


def test_run_owned_merge_halt_is_left_open_with_both_routes(repo):
    mbw.merge_branch_into_workstream("theirs", "r", repo, stage=True, run_id=RUN)
    head = _git(repo, "rev-parse", "HEAD")

    out = _call(repo, {**HALT, "next_action": REFIRE})

    assert out["refused"] == land_run_halt.HALT_ON_RUN_OWNED_MERGE and out["committed"] is False
    assert out["merge_route"]["finish"] == REFIRE["call"]
    assert out["merge_route"]["abort"] == "branch.merge_into_workstream {abort: true}"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _merge_head(repo).exists()
    assert (repo / RECORD_DIR_ROOT / RUN / "halted.json").is_file()


def test_the_abort_route_lets_the_halt_land_single_parent(repo):
    mbw.merge_branch_into_workstream("theirs", "r", repo, stage=True, run_id=RUN)
    assert _call(repo)["refused"] == land_run_halt.HALT_ON_RUN_OWNED_MERGE

    mbw.merge_branch_into_workstream("", "r", repo, abort=True)
    out = _call(repo)

    assert out["committed"] is True, out
    assert not _merge_head(repo).exists()
    assert len(_git(repo, "rev-list", "--parents", "-n1", out["sha"]).split()) == 2


def test_a_foreign_merge_keeps_the_commit_refusal(repo):
    mbw.merge_branch_into_workstream("theirs", "r", repo, stage=True)
    assert merge_owner.owner(repo) is None

    out = _call(repo)

    assert out["refused"] == "commit-refused" and "partway through a merge" in out["error"]
    assert "merge_route" not in out
