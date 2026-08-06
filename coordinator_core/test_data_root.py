"""Tests for coordinator_core.data_root.

Mirrors the fixture shape of coordinator/bin/lib's own coordinator_data_root.py
tests (co-located rung, example-doctrine-repo-resident rung, both-rungs-fail hard error) but
exercises the coordinator_core-native module, which delegates rung 2 to
coordinator_core.ops.coordinator_doe_root.coordinator_doe_root() instead of
coordinator_registry.doe_root() — see data_root.py's module docstring for why.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core import data_root as dr_mod

_BIN_LIB_DIR = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "lib"


def test_colocated_rung_wins_when_dir_present(tmp_path, monkeypatch):
    (tmp_path / "schemas").mkdir()
    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: tmp_path)
    # Rung 2 must never even be consulted when rung 1 resolves.
    monkeypatch.setattr(
        dr_mod,
        "coordinator_doe_root",
        lambda: (_ for _ in ()).throw(AssertionError("rung 2 should not run")),
    )
    result = dr_mod.data_root("schemas")
    assert result == tmp_path / "schemas"


def test_doe_resident_rung_used_when_colocated_missing(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    doe_root = tmp_path / "doe"
    (doe_root / "coordinator" / "schemas").mkdir(parents=True)

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_doe_root", lambda: str(doe_root))

    result = dr_mod.data_root("schemas")
    assert result == doe_root / "coordinator" / "schemas"


def test_raises_when_doe_root_unresolved(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_doe_root", lambda: None)

    with pytest.raises(RuntimeError, match="cannot resolve data dir 'schemas'"):
        dr_mod.data_root("schemas")


def test_raises_when_doe_root_resolved_but_dir_missing(tmp_path, monkeypatch):
    colocated_base = tmp_path / "colocated"
    colocated_base.mkdir()
    doe_root = tmp_path / "doe"
    doe_root.mkdir()  # no coordinator/schemas under it

    monkeypatch.setattr(dr_mod, "_colocated_root", lambda: colocated_base)
    monkeypatch.setattr(dr_mod, "coordinator_doe_root", lambda: str(doe_root))

    with pytest.raises(RuntimeError, match="cannot resolve data dir 'schemas'"):
        dr_mod.data_root("schemas")


def test_colocated_root_points_at_coordinator_dir():
    # coordinator_core/data_root.py -> parent.parent/"coordinator" should be
    # the coordinator/ directory that sits beside coordinator_core/ in the
    # repo root — the SAME namespace coordinator_data_root.py's own
    # `_colocated_root()` resolves (`<coordinator-root>/<dir_name>`), not the
    # bare claude-klabauter repo root. (Regression: this test previously asserted the
    # bare repo root was correct, which was pinning the two-namespaces bug —
    # see data_root.py's `_colocated_root()` negative-spec.)
    resolved = dr_mod._colocated_root()
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "coordinator_core").is_dir()
    assert resolved == repo_root / "coordinator"


@pytest.mark.parametrize("dir_name", ["docs", "schemas", "snippets", "templates"])
def test_both_data_root_entrypoints_agree(dir_name):
    """The two `data_root()` entrypoints — this module's own, and the bin/lib
    twin (`coordinator/bin/lib/coordinator_data_root.py`) — MUST resolve to
    the SAME path for the same `dir_name` (see both modules' "MUST stay
    behaviorally consistent" cross-references). This is a REAL-base test —
    neither `_colocated_root()` is monkeypatched — because the bug this
    guards against (rung 1 silently probing two DIFFERENT namespaces: the
    bare claude-klabauter repo root vs. `<repo>/coordinator/`) only manifests with the
    genuine, un-mocked base. Every other test in both suites monkeypatches
    `_colocated_root` away, which is exactly why the real base went untested
    and the divergence shipped unnoticed.
    """
    if str(_BIN_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_BIN_LIB_DIR))
    import coordinator_data_root as cdr_mod  # noqa: PLC0415

    core_result = dr_mod.data_root(dir_name)
    bin_result = cdr_mod.data_root(dir_name)
    assert core_result == bin_result, (
        f"data_root({dir_name!r}) diverged: "
        f"coordinator_core.data_root -> {core_result}, "
        f"coordinator_data_root (bin/lib twin) -> {bin_result}"
    )
