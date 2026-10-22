"""nlm-run-gate / nlm-preflight: install-map derivation and trampoline passthrough."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from coordinator_core.install.substrate import _derive_agent_helper_target_map

_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
_NAMES = ("nlm-run-gate", "nlm-preflight")


@pytest.mark.parametrize("name", _NAMES)
def test_install_map_derives_name_to_py(name):
    assert _derive_agent_helper_target_map(_BIN).get(name) == f"{name}.py"


@pytest.mark.parametrize("name", _NAMES)
def test_trampoline_forwards_argv_and_exit_code(name, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "path", [str(_BIN / "lib"), *sys.path])
    import coordinator_data_root

    root = tmp_path / "pipelines"
    stub = root / "deep-research" / "notebooklm" / f"{name}.py"
    stub.parent.mkdir(parents=True)
    stub.write_text(
        "import sys\nprint('ARGV0', sys.argv[0])\nprint('ARGV', *sys.argv[1:])\nsys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(coordinator_data_root, "data_root", lambda _n: root)
    monkeypatch.setattr(sys, "argv", [name, "claims", "--x", "y"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_BIN / f"{name}.py"), run_name="__main__")

    assert exc.value.code == 3
    out = capsys.readouterr().out
    assert "ARGV claims --x y" in out
    assert f"ARGV0 {stub.resolve()}" in out


@pytest.mark.parametrize("name", _NAMES)
def test_trampoline_exits_127_when_target_missing(name, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", [str(_BIN / "lib"), *sys.path])
    import coordinator_data_root

    monkeypatch.setattr(coordinator_data_root, "data_root", lambda _n: tmp_path)
    monkeypatch.setattr(sys, "argv", [name])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_BIN / f"{name}.py"), run_name="__main__")

    assert exc.value.code == 127
