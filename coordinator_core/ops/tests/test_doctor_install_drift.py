"""Doctor layer: the content root's install script reports manifest/step drift, imported not spawned."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import doctor

_SCRIPT = '''
def check_manifest_drift():
    return {problems!r}
'''


@pytest.fixture
def content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "content"
    (root / "coordinator" / "lib" / "install").mkdir(parents=True)
    (root / "coordinator" / "hooks").mkdir(parents=True)
    monkeypatch.setattr("coordinator_core.content_root.read_content_root", lambda: str(root))
    return root


def _write(content: Path, problems: list[str]) -> None:
    (content / "coordinator" / "lib" / "install" / "coordinator_install.py").write_text(
        _SCRIPT.format(problems=problems)
    )


def test_drift_is_a_broken_finding(content: Path):
    _write(content, ["manifest entry x has no implementing step"])
    layer = doctor._check_install_drift()
    assert layer.status == "broken"
    assert "no implementing step" in layer.findings[0].message


def test_no_drift_is_ok_and_silent(content: Path):
    _write(content, [])
    layer = doctor._check_install_drift()
    assert (layer.status, layer.findings) == ("ok", [])


def test_absent_script_is_ok(content: Path):
    layer = doctor._check_install_drift()
    assert (layer.status, layer.findings) == ("ok", [])


def test_raising_script_is_reported(content: Path):
    (content / "coordinator" / "lib" / "install" / "coordinator_install.py").write_text("raise RuntimeError('x')")
    assert doctor._check_install_drift().status == "broken"


def test_dataclass_script_with_future_annotations_loads_ok(content: Path):
    import sys

    (content / "coordinator" / "lib" / "install" / "coordinator_install.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Step:\n"
        "    name: str\n\n"
        "def check_manifest_drift():\n"
        "    return []\n"
    )
    layer = doctor._check_install_drift()
    assert (layer.status, layer.findings) == ("ok", [])
    assert "_doctor_coordinator_install" not in sys.modules
