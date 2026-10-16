"""`coordinator/bin/check-bin-forwarders.py` CLI entrypoint: exit 0 on a canonical
forwarder, exit 1 listing the offender when a DoE bin script regrew logic.

The DoE clone and engine bin are tmp_path trees injected by patching the resolvers the
op calls, so no real clone is read.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.plugin_health import doe_bin_forwarders as op

_TOOL = Path(__file__).resolve().parents[1] / "check-bin-forwarders.py"


def _load():
    """Hyphenated filename: load by path, not dotted import."""
    spec = importlib.util.spec_from_file_location("_check_bin_forwarders", _TOOL)
    assert spec and spec.loader, f"unloadable: {_TOOL}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(tmp_path: Path, doe_body: str) -> None:
    doe_bin = tmp_path / "doe" / "coordinator" / "bin"
    eng_bin = tmp_path / "engine" / "coordinator" / "bin"
    doe_bin.mkdir(parents=True)
    eng_bin.mkdir(parents=True)
    (eng_bin / "tool.py").write_text("# engine logic\n", encoding="utf-8")
    (doe_bin / "tool.py").write_text(doe_body, encoding="utf-8")


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(_TOOL.parent))  # the tool's `import lib` needs coordinator/bin on the path
    monkeypatch.setattr(op, "_resolve_content_root", lambda: tmp_path / "doe")
    monkeypatch.setattr(op, "_resolve_agent_bin", lambda: tmp_path / "engine" / "coordinator" / "bin")
    return tmp_path


def test_canonical_forwarder_exits_zero(wired, capsys):
    _stage(wired, "\n".join(op.CANONICAL_CODE) + "\n")
    assert _load().main([]) == 0
    assert capsys.readouterr().err == ""


def test_regrown_logic_exits_one_and_names_offender(wired, capsys):
    _stage(wired, "\n".join(op.CANONICAL_CODE) + "\nprint('regrown logic')\n")
    assert _load().main([]) == 1
    err = capsys.readouterr().err
    assert "bin forwarder drift:" in err
    assert "tool.py: carries logic beyond the forwarder body" in err
