"""A percolate round's version-consistency gate child must not read the authoring
checkout (which the publisher bound `coordinator_core` from) as a foreign engine.

Observed: inside a round, `check-version-consistency` ran from the published mirror and died with
"coordinator_core already bound from <claude-klabauter>, diverges from dispatch root <mirror>".
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _BIN_DIR / "lib"
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


@pytest.fixture
def mirror_locator(tmp_path, monkeypatch):
    """Pin this module's own locator root to a mirror, as for a published CLI."""
    mirror = _engine(tmp_path / "mirror")
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: str(mirror))
    return mirror


def test_authoring_checkout_named_by_source_root_env_is_a_twin(tmp_path, monkeypatch, mirror_locator):
    authoring = _engine(tmp_path / "authoring")
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(authoring))
    assert _mod._is_source_twin(_report(authoring, mirror_locator)) is True


def test_a_third_tree_is_still_not_a_twin(tmp_path, monkeypatch, mirror_locator):
    authoring = _engine(tmp_path / "authoring")
    third = _engine(tmp_path / "third")
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(authoring))
    assert _mod._is_source_twin(_report(third, mirror_locator)) is False


def test_without_the_env_the_authoring_checkout_is_not_a_twin(tmp_path, monkeypatch, mirror_locator):
    authoring = _engine(tmp_path / "authoring")
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    assert _mod._is_source_twin(_report(authoring, mirror_locator)) is False


def test_publish_names_its_own_checkout_to_the_gate_child(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("publish_vc_gate_env_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    publish = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = publish
    spec.loader.exec_module(publish)

    source = tmp_path / "src"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin" / "marketplace.json").write_text("{}")
    gate_dir = tmp_path / "bin"
    gate_dir.mkdir()
    (gate_dir / "check-version-consistency.py").write_text("")
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_BIN", raising=False)
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    ok = publish.check_version_consistency(source, gate_dir, "row", out=io.StringIO(), err=io.StringIO())
    assert ok is True
    assert seen["env"]["COORDINATOR_ENGINE_SOURCE_ROOT"] == str(publish._REPO_ROOT)
