"""scratch.record_run_path and scratch.keep: ledger writes, keep refusals, registration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops import scratch_record as sr

RUN = "ask-test-1"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    rd = tmp_path / "state" / "warp" / RUN
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(json.dumps({"run_dir": str(rd), "rows": []}))
    return tmp_path


def _ledger(root: Path) -> dict:
    return json.loads((root / "state" / "warp" / RUN / "scratch-ledger.json").read_text())


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("scratch record op spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_record_run_path_lists_paths_in_ledger(tmp_path):
    root = _repo(tmp_path)
    out = sr._record_handler(
        {"run_id": RUN, "paths": ["scratch/land-1", "scratch\\probe-2/"]}, repo_root=str(root)
    )
    assert out == {"recorded": ["scratch/land-1", "scratch/probe-2"]}
    assert _ledger(root)["paths"] == ["scratch/land-1", "scratch/probe-2"]


@pytest.mark.parametrize(
    "bad", ["/abs/x", "scratch/../x", "src/a.py", "scratch", "scratch/.deleted/x", "scratch/scratch-hold/x"]
)
def test_record_run_path_refuses_unsafe_path_and_writes_nothing(tmp_path, bad):
    root = _repo(tmp_path)
    out = sr._record_handler({"run_id": RUN, "paths": ["scratch/ok", bad]}, repo_root=str(root))
    assert "error" in out
    assert not (root / "state" / "warp" / RUN / "scratch-ledger.json").exists()


def test_record_run_path_refuses_bad_params(tmp_path):
    root = _repo(tmp_path)
    assert "error" in sr._record_handler({"run_id": "", "paths": ["scratch/a"]}, repo_root=str(root))
    assert "error" in sr._record_handler({"run_id": RUN, "paths": []}, repo_root=str(root))
    assert "error" in sr._record_handler({"run_id": RUN, "paths": "scratch/a"}, repo_root=str(root))


def test_keep_records_entry_and_index(tmp_path):
    root = _repo(tmp_path)
    out = sr._keep_handler(
        {"run_id": RUN, "path": "scratch/hold-a", "purpose": "review replay", "ttl_hours": 6},
        repo_root=str(root),
    )
    assert out["kept"]["purpose"] == "review replay"
    assert _ledger(root)["keeps"][0]["path"] == "scratch/hold-a"
    index = json.loads((root / "scratch" / ".kept-index.json").read_text())
    assert index["scratch/hold-a"]["run_id"] == RUN


@pytest.mark.parametrize(
    "extra",
    [
        {"ttl_hours": 6},
        {"purpose": "", "ttl_hours": 6},
        {"purpose": "x"},
        {"purpose": "x", "ttl_hours": 0},
        {"purpose": "x", "ttl_hours": -1},
        {"purpose": "x", "ttl_hours": True},
        {"purpose": "x", "ttl_hours": 24 * 30 + 1},
    ],
)
def test_keep_without_purpose_or_valid_ttl_is_refused(tmp_path, extra):
    root = _repo(tmp_path)
    out = sr._keep_handler({"run_id": RUN, "path": "scratch/hold-a", **extra}, repo_root=str(root))
    assert "error" in out
    assert not (root / "scratch" / ".kept-index.json").exists()
    assert not (root / "state" / "warp" / RUN / "scratch-ledger.json").exists()


def test_keep_refuses_path_outside_scratch(tmp_path):
    root = _repo(tmp_path)
    out = sr._keep_handler(
        {"run_id": RUN, "path": "src/a", "purpose": "x", "ttl_hours": 1}, repo_root=str(root)
    )
    assert "error" in out


def test_ops_are_registered_classified_and_scoped():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    for op in (sr.RECORD_OP_KEY, sr.KEEP_OP_KEY):
        assert OP_MODULE_MAP[op] == "coordinator_core.ops.scratch_record"
        assert OP_CLASSIFICATION[op] is OpClass.MUTATING
        assert _OP_KEY_SCOPE[op] == "show_top"
