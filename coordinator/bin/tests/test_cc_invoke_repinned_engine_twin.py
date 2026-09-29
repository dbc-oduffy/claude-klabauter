"""`require_dispatch_engine_on_path` must not fail-open the guard plane when a
cloud container's interpreter is bound to the pre-repin engine copy.

The exemption (`_is_repinned_engine_twin`) is keyed on `COORDINATOR_ENGINE_ROOT`
being a live symlink onto the dispatch root; a genuine third tree still raises.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402

pytestmark = pytest.mark.cadence


def _engine(path: Path) -> Path:
    (path / "coordinator_core").mkdir(parents=True)
    (path / "coordinator_core" / "__init__.py").write_text("")
    return path


def _report(bound: Path, dispatch: Path):
    return _mod.ProvenanceReport(
        verdict=_mod.PROVENANCE_DIVERGENT,
        imported_file=str(bound / "coordinator_core" / "__init__.py"),
        engine_root=str(dispatch),
        caller="require_dispatch_engine_on_path",
        axis="dispatch",
    )


def test_frozen_copy_bound_while_link_points_at_fresh_checkout_is_a_twin(tmp_path, monkeypatch):
    frozen = _engine(tmp_path / "frozen")
    fresh = _engine(tmp_path / "fresh")
    link = tmp_path / "engine-current"
    link.symlink_to(fresh)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(link))
    assert _mod._is_repinned_engine_twin(_report(frozen, fresh)) is True


def test_a_third_tree_without_the_engine_link_still_diverges(tmp_path, monkeypatch):
    bound = _engine(tmp_path / "bound")
    dispatch = _engine(tmp_path / "dispatch")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(dispatch))  # a plain dir, not a link
    assert _mod._is_repinned_engine_twin(_report(bound, dispatch)) is False


def test_the_link_naming_a_different_root_than_dispatch_is_refused(tmp_path, monkeypatch):
    bound = _engine(tmp_path / "bound")
    dispatch = _engine(tmp_path / "dispatch")
    other = _engine(tmp_path / "other")
    link = tmp_path / "engine-current"
    link.symlink_to(other)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(link))
    assert _mod._is_repinned_engine_twin(_report(bound, dispatch)) is False


def test_the_twin_is_announced_once_on_stderr(tmp_path, monkeypatch, capsys):
    frozen = _engine(tmp_path / "frozen")
    fresh = _engine(tmp_path / "fresh")
    monkeypatch.setattr(_mod, "_REPINNED_TWIN_ANNOUNCED", False)
    rep = _report(frozen, fresh)
    _mod._announce_repinned_engine_twin(rep)
    _mod._announce_repinned_engine_twin(rep)
    assert capsys.readouterr().err.count("engine split") == 1
