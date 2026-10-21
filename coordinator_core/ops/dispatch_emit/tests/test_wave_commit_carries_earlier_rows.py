"""A wave commit carries the same-run DONE rows an earlier wave failed to land, so a later row's
import of an earlier wave's file is never left uncommitted; a peer's uncommitted import is still
refused by the leak gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION = "9d860631-c114-462a-a6ee-f685cc671ced"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def test_wave_commit_script_carries_uncommitted_rows_forward():
    script = compose_script(
        [[_write_row("C1")], [_write_row("C2", writes=["pkg/other.py"])]],
        name="wf", description="d", session_id=_SESSION, expected_branch="work/run", **REVIEW_KW,
    )

    commit_wave = script.split("async function _commitWave", 1)[1].split("\n  }\n", 1)[0]
    assert "const _carried = [];" in script
    assert "[..._carried, ...done].flatMap((i) => _landed[i].paths)" in commit_wave
    failure, success = commit_wave.split("if (!r || r.outcome !== 'committed') {", 1)[1].split("\n    }\n", 1)
    assert "_carried.push(i)" in failure
    assert "_carried.length = 0;" in success


@pytest.mark.spawns_process
def test_two_wave_commit_lands_both_files_when_the_union_is_passed(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "pkg" / "contract.py").write_text("SYMBOL_USRS_VERSION = 1\n", encoding="utf-8")
    (repo / "pkg" / "persist.py").write_text("from pkg.contract import SYMBOL_USRS_VERSION\n", encoding="utf-8")

    out = commit_v2._handler(
        {"paths": ["pkg/contract.py", "pkg/persist.py"], "message": "wave 3 carrying wave 2\n"},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert {"pkg/contract.py", "pkg/persist.py"} <= set(_git(["ls-files"], repo).split())


@pytest.mark.spawns_process
def test_peer_uncommitted_import_is_still_refused(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "pkg" / "contract.py").write_text("SYMBOL_USRS_VERSION = 1\n", encoding="utf-8")
    (repo / "pkg" / "persist.py").write_text("from pkg.contract import SYMBOL_USRS_VERSION\n", encoding="utf-8")
    before = _git(["rev-parse", "HEAD"], repo)

    out = commit_v2._handler(
        {"paths": ["pkg/persist.py"], "message": "persist alone\n"}, repo_root=repo / ".git"
    )

    assert out["committed"] is False
    assert "import_closure" in out["error"]
    assert "pkg/contract.py is uncommitted in the worktree" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == before
