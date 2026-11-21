"""
coordinator_core.ops.tests.test_runs_withheld_overlap_op -- tests for "runs.withheld_overlap":
registration quad, holders from the halted index and kept.json, dirty from one git status spawn.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git import run as git_run
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import runs_withheld_overlap as op
from coordinator_core.ops.dispatch_emit import halted_withheld_index


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _kept(root: Path, run_id: str, rel: str) -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {rel: "ab" * 20}}}}))


def test_registration_quad() -> None:
    key = "runs.withheld_overlap"
    assert ipc._REGISTRY.get(key) is not None
    assert _registry_map.OP_MODULE_MAP[key] == "coordinator_core.ops.runs_withheld_overlap"
    assert OP_KEY_SCOPE[key] == "show_top"
    assert OP_CLASSIFICATION[key] is OpClass.COMPUTE_ONLY


def test_invalid_params() -> None:
    assert "error" in op._runs_withheld_overlap({})
    assert "error" in op._runs_withheld_overlap({"repo": "rel"})
    assert "error" in op._runs_withheld_overlap({"repo": str(Path.cwd().anchor)})


def test_clean_one_spawn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    out = op._runs_withheld_overlap({"repo": str(repo), "paths": ["a.py", "b.py"]})
    assert out == {"overlaps": [], "clean": True}
    assert len(calls) == 1 and "status" in calls[0] and "-z" in calls[0]


def test_withheld_and_kept_holders_and_dirty(repo: Path) -> None:
    halted_withheld_index.add_run(repo, "run-halted", ["a.py"])
    _kept(repo, "run-halted", "a.py")
    halted_withheld_index.add_run(repo, "run-other", ["b.py"])
    (repo / "c.py").write_text("dirty\n")
    out = op._runs_withheld_overlap({"repo": str(repo), "paths": ["a.py", "b.py", "c.py"]})
    assert out["clean"] is False
    assert out["overlaps"] == [
        {"path": "a.py", "holder": "run-halted", "state": "kept"},
        {"path": "a.py", "holder": "run-halted", "state": "withheld"},
        {"path": "b.py", "holder": "run-other", "state": "withheld"},
        {"path": "c.py", "holder": "unattributed", "state": "dirty"},
    ]


def test_run_id_uses_manifest_writes_and_excludes_self(repo: Path) -> None:
    halted_withheld_index.add_run(repo, "run-peer", ["a.py"])
    halted_withheld_index.add_run(repo, "run-self", ["b.py"])
    d = repo / "state" / "warp" / "run-self"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": ["a.py", "b.py"]}]}))
    out = op._runs_withheld_overlap({"repo": str(repo), "run_id": "run-self"})
    assert out["overlaps"] == [{"path": "a.py", "holder": "run-peer", "state": "withheld"}]


@pytest.mark.spawns_process
def test_clean_one_spawn_attributed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._runs_withheld_overlap({"repo": str(repo), "paths": ["a.py", "b.py"]})
    assert out["clean"] is True
    assert len(spawns) == 1
    assert spawns[0].origin == "run_git"
    with _count_spawns_attributed(monkeypatch) as none:
        op._runs_withheld_overlap({"repo": str(repo), "paths": []})
    assert none == []


def test_halted_run_whose_halt_commit_failed_keeps_its_declared_paths(repo: Path) -> None:
    # Declared footprint recorded, nothing withheld: the halt-commit never landed its withheld paths.
    halted_withheld_index.record_declared(repo, "run-halted", ["a.py", "c.py"])
    (repo / "c.py").write_text("dirty\n")
    out = op._runs_withheld_overlap({"repo": str(repo), "paths": ["a.py", "c.py"]})
    assert out["overlaps"] == [
        {"path": "a.py", "holder": "run-halted", "state": "declared"},
        {"path": "c.py", "holder": "run-halted", "state": "declared"},
    ]


def test_closed_run_holds_nothing(repo: Path) -> None:
    # A closed run is terminal and no op can release its index entry: e6ed7b's declared paths blocked a28592 on the Mini.
    halted_withheld_index.add_run(repo, "run-closed", ["a.py"])
    halted_withheld_index.record_declared(repo, "run-closed", ["a.py", "b.py"])
    _kept(repo, "run-closed", "a.py")
    (repo / "state" / "warp" / "run-closed" / "halted.json").write_text(
        json.dumps({"status": "closed", "closed": {"reason": "t", "at": "t"}})
    )
    halted_withheld_index.add_run(repo, "run-halted", ["b.py"])
    out = op._runs_withheld_overlap({"repo": str(repo), "paths": ["a.py", "b.py"]})
    assert out["overlaps"] == [{"path": "b.py", "holder": "run-halted", "state": "withheld"}]
