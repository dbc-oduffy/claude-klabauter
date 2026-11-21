"""Case 9 replay: example-game-repo b9c1a5-1 halted with a diff that swept in the uncommitted `driver.py` hunks of run
#42 (c35e97-2), still in flight. `own_hunk_candidate.candidate` for the halting run carries only its own hunk."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import own_hunk_candidate as ohc, stranded_sibling
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DRIVER = "Plugins/Example-Game-Repo/Source/driver.py"
RUN_A = "b9c1a5-1"
RUN_B = "c35e97-2"
LINES = 120


def _git(args, cwd: Path, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=str(cwd), input=data, capture_output=True, check=True,
                          **no_console_creationflags()).stdout


def _body(**edits: str) -> bytes:
    rows = [f"step_{i}()" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return ("\n".join(rows) + "\n").encode("utf-8")


def _blob(root: Path, data: bytes) -> str:
    return _git(["hash-object", "-w", "--stdin"], root, data).decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "example-game-repo"
    (root / "Plugins/Example-Game-Repo/Source").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / DRIVER).write_bytes(_body())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def test_a_halting_run_takes_only_its_own_driver_hunk_and_names_the_live_sibling(repo):
    own = _body(l20="step_20(own_b9c1a5)")
    sibling = _body(l90="step_90(live_c35e97)")
    both = _body(l20="step_20(own_b9c1a5)", l90="step_90(live_c35e97)")

    rdir_a = repo / RECORD_DIR_ROOT / RUN_A
    rdir_a.mkdir(parents=True)
    (rdir_a / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": {DRIVER: _blob(repo, own)}}}}), encoding="utf-8")
    stranded_sibling.write(repo / RECORD_DIR_ROOT / RUN_B, {DRIVER: {"base": None, "run": _blob(repo, sibling)}})
    (repo / DRIVER).write_bytes(both)
    head = _git(["show", f"HEAD:{DRIVER}"], repo)

    out = ohc.candidate(repo, RUN_A, DRIVER, head, both, declared=False)

    assert out["excluded"] == [{"lines": "90", "owner": RUN_B}]
    assert out["bytes"] == own
    assert b"live_c35e97" not in out["bytes"] and b"own_b9c1a5" in out["bytes"]
    assert (repo / DRIVER).read_bytes() == both


def test_a_run_with_no_record_of_its_own_is_refused_rather_than_sweeping_the_live_sibling(repo):
    sibling = _body(l90="step_90(live_c35e97)")
    stranded_sibling.write(repo / RECORD_DIR_ROOT / RUN_B, {DRIVER: {"base": None, "run": _blob(repo, sibling)}})
    head = _git(["show", f"HEAD:{DRIVER}"], repo)

    out = ohc.candidate(repo, RUN_A, DRIVER, head, sibling, declared=False)

    assert out["refused"] == ohc.NO_EVIDENCE
    assert out["lines"] == "90" and out["owner"] == RUN_B
    assert "bytes" not in out


def test_a_run_whose_recorded_hunks_are_not_in_the_tree_is_refused_not_landed_as_head(repo):
    own = _body(l20="step_20(own_b9c1a5)")
    sibling = _body(l90="step_90(live_c35e97)")
    rdir_a = repo / RECORD_DIR_ROOT / RUN_A
    rdir_a.mkdir(parents=True)
    (rdir_a / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": {DRIVER: _blob(repo, own)}}}}), encoding="utf-8")
    stranded_sibling.write(repo / RECORD_DIR_ROOT / RUN_B, {DRIVER: {"base": None, "run": _blob(repo, sibling)}})
    (repo / DRIVER).write_bytes(sibling)
    head = _git(["show", f"HEAD:{DRIVER}"], repo)

    out = ohc.candidate(repo, RUN_A, DRIVER, head, sibling, declared=False)

    assert out["refused"] == ohc.NO_EVIDENCE and out["owner"] == RUN_B and "bytes" not in out
