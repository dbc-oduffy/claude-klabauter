"""Pins for the assembled-mirror gate's clean-verdict cache: a hit skips the
collect, a changed tree misses, and a REFUSED verdict is never cached."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import assembled_mirror_gate as gate  # noqa: E402

_CLEAN = SimpleNamespace(returncode=0, stdout="1 test collected in 0.01s\n", stderr="")
_ERRORED = SimpleNamespace(returncode=2, stdout="1/2 tests collected, 1 error in 0.01s\n", stderr="")


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "coordinator_core").mkdir(parents=True)
    (tree / "coordinator_core" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return tree


def test_clean_verdict_is_cached_and_second_run_skips_collect(tmp_path, capsys):
    tree, cache = _tree(tmp_path), tmp_path / "cache"
    with mock.patch.object(gate.subprocess, "run", return_value=_CLEAN) as run:
        first = gate.run_assembled_mirror_gate(tree, cache_dir=cache)
        second = gate.run_assembled_mirror_gate(tree, cache_dir=cache)
    assert first.passed and second.passed
    assert run.call_count == 1
    assert second.stdout_tail == "cached"
    assert "cached" in capsys.readouterr().err


def test_changed_tree_misses_cache(tmp_path):
    tree, cache = _tree(tmp_path), tmp_path / "cache"
    with mock.patch.object(gate.subprocess, "run", return_value=_CLEAN) as run:
        gate.run_assembled_mirror_gate(tree, cache_dir=cache)
        (tree / "coordinator_core" / "mod.py").write_text("x = 2\n", encoding="utf-8")
        gate.run_assembled_mirror_gate(tree, cache_dir=cache)
    assert run.call_count == 2


def test_refused_verdict_is_never_cached_and_always_reruns(tmp_path):
    tree, cache = _tree(tmp_path), tmp_path / "cache"
    with mock.patch.object(gate.subprocess, "run", return_value=_ERRORED) as run:
        first = gate.run_assembled_mirror_gate(tree, cache_dir=cache)
        second = gate.run_assembled_mirror_gate(tree, cache_dir=cache)
    assert not first.passed and not second.passed
    assert run.call_count == 2
    assert not (cache / gate._VERDICT_CACHE_FILE).exists()
