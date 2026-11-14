"""Fleet test: no unallowlisted junction or symlink sits in this repo tree, so a reparse point the
land-side advisory only reports cannot land silently. The walk is os.scandir only, no spawn."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_REPO = Path(__file__).resolve().parents[2]
_IMPL = _REPO / "coordinator" / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_reparse_fleet", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)

_NT = os.name == "nt"
# Machine-local scratch and run-output trees (gitignored or run artifacts): millions of entries
# that no landed file can sit under.
_SKIP_RELPATHS = frozenset({"scratch", ".coordinator-local", "tmp", "state/scratch", "state/warp"})
_WALK_BUDGET_MS = 200  # min of three process-time runs: one warm pass must fit the bar


def _link_dir(link: Path, target: Path) -> None:
    """A real directory junction on nt, a directory symlink on POSIX."""
    if _NT:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _walk(root: Path, allow: set, skip_relpaths=_SKIP_RELPATHS) -> "list[tuple[str, str]]":
    """The production scan over one root, minus the machine-local trees."""
    return _ml.scan_reparse_points([root], allow, skip_relpaths)


def test_fixture_tree_with_junction_is_flagged(tmp_path):
    root = tmp_path / "repo"
    real = tmp_path / "real"
    root.mkdir()
    real.mkdir()
    (root / "state").mkdir()
    _link_dir(root / "state" / "retired_store", real)
    found = _walk(root, set())
    assert [p for p, _t in found] == [str(root / "state" / "retired_store")]


def test_fixture_tree_allowlisted_junction_passes(tmp_path):
    root = tmp_path / "repo"
    real = tmp_path / "real"
    root.mkdir()
    real.mkdir()
    _link_dir(root / "retired_store", real)
    assert _walk(root, {str(root / "retired_store")}) == []


def test_fixture_tree_does_not_descend_into_a_reparse_point(tmp_path):
    root = tmp_path / "repo"
    real = tmp_path / "real"
    root.mkdir()
    (real / "inner").mkdir(parents=True)
    _link_dir(real / "inner" / "deeper", real)
    _link_dir(root / "outer", real)
    assert [p for p, _t in _walk(root, set())] == [str(root / "outer")]


def test_this_repo_holds_no_unallowlisted_reparse_point():
    allow = _ml._reparse_allowlist(_ml._build_resolution_layers(_ml._registry_dir()))
    spent = []
    found = []
    for _ in range(3):
        started = time.process_time()
        found = _walk(_REPO, allow)
        spent.append((time.process_time() - started) * 1000)
    assert not found, "unallowlisted reparse point(s):\n" + "\n".join(f"reparse\t{p} -> {t}" for p, t in found)
    print(f"reparse walk process time: {min(spent):.0f}ms")
    assert min(spent) < _WALK_BUDGET_MS
