"""The r10 class (an unchanged importer of a dropped module) is refused, the incremental-call signature is
pinned for the incremental verdict, and the profiler reports the gate's cost."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from coordinator_core.benchmarks import import_closure_gate_profile as profile  # noqa: E402
from percolate import import_closure as ic  # noqa: E402

R10_VIOLATION = ("warm/pair_stop.py", "percolate.round")


def _r10_tree(root: Path, *, with_round: bool = False) -> Path:
    (root / "warm").mkdir(parents=True)
    (root / "percolate").mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "percolate" / "__init__.py").write_text("", encoding="utf-8")
    (root / "warm" / "__init__.py").write_text("", encoding="utf-8")
    (root / "warm" / "pair_stop.py").write_text(
        "from coordinator_core.percolate.round import X\n", encoding="utf-8"
    )
    if with_round:
        (root / "percolate" / "round.py").write_text("X = 1\n", encoding="utf-8")
    return root


def test_r10_pair_stop_importing_a_dropped_round_module_is_refused_on_the_full_walk(tmp_path):
    examined, violations = ic.find_import_closure_violations(_r10_tree(tmp_path / "t"))

    assert examined == 4
    assert violations == [R10_VIOLATION]


def test_r10_is_not_refused_once_the_round_module_ships(tmp_path):
    _, violations = ic.find_import_closure_violations(_r10_tree(tmp_path / "t", with_round=True))

    assert violations == []


@pytest.mark.xfail(strict=True, reason="incremental verdict lands in C2")
def test_find_import_closure_violations_accepts_changed_and_baseline():
    params = inspect.signature(ic.find_import_closure_violations).parameters

    assert params["changed"].default is None
    assert params["baseline"].default is None
    assert params["tests_only"].default is False


def test_profiler_prints_the_gate_cost_and_the_r10_verdict(tmp_path, capsys):
    tree = _r10_tree(tmp_path / "t")

    assert profile.main([str(tree)]) == 0
    row = json.loads(capsys.readouterr().out)

    assert row["mode"] == "full"
    assert row["files_graded"] == row["examined"] == 4
    assert row["files_opened"] == 4
    assert row["spawns"] == 0
    assert row["violations"] == [list(R10_VIOLATION)]
    assert set(row["phase_ms"]) == {"stat", "read", "hash", "parse", "resolve"}
    assert row["process_ms"] >= 0


def test_profiler_diffs_the_tree_against_a_manifest(tmp_path, capsys):
    tree = _r10_tree(tmp_path / "t")
    stale = tmp_path / "manifest.json"
    stale.write_text(
        json.dumps(
            {
                "schema": 1,
                "source_head": "0" * 40,
                "paths": {
                    "pkg/__init__.py": profile._blob_sha1(b""),
                    "pkg/percolate/__init__.py": profile._blob_sha1(b""),
                    "pkg/percolate/round.py": profile._blob_sha1(b"X = 1\n"),
                },
            }
        ),
        encoding="utf-8",
    )

    assert profile.main([str(tree), "--manifest", str(stale), "--prefix", "pkg/", "--no-phases"]) == 0
    row = json.loads(capsys.readouterr().out)

    assert (row["changed"], row["removed"]) == (2, 1)
    assert "phase_ms" not in row
