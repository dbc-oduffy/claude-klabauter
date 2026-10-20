"""Tests for claude-author's --control-socket flag: consumed, supervisor loaded by path, fail loud."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "claude-author.py"


@pytest.fixture
def ca(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_author_cs_under_test", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    clone = tmp_path / "clone"
    plugin = clone / "coordinator"
    plugin.mkdir(parents=True)
    base = tmp_path / "rt"
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(base))
    for var in ("CLAUDE_AUTHOR_DRY_RUN", "CLAUDE_AUTHOR_PRINT_PLUGIN_DIR", "CLAUDE_AUTHOR_NO_EXEC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod, "_resolve_doe_clone", lambda _cli: str(clone))
    monkeypatch.setattr(mod, "_resolve_plugin_root", lambda _c: str(plugin))
    monkeypatch.setattr(mod, "_resolve_claude_bin", lambda: "claude")
    monkeypatch.setattr(mod, "_claude_exec_argv", lambda b: [b])
    launched: list = []
    monkeypatch.setattr(mod, "_launch", lambda prefix, argv: launched.append(argv) or 0)
    mod._test = type("T", (), {"plugin": plugin, "base": base, "launched": launched})
    yield mod
    for name in [n for n in sys.modules if n.startswith("claude_author_control")]:
        del sys.modules[name]


def _stub_supervisor(plugin: Path, body: str) -> None:
    pkg = plugin / "bin" / "lib" / "claude_author_control"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "supervisor.py").write_text(body, encoding="utf-8")


def test_flag_consumed_and_stub_receives_full_argv(ca, tmp_path):
    out = tmp_path / "got.txt"
    _stub_supervisor(
        ca._test.plugin,
        "def run(argv, plugin_dir, control_base):\n"
        f"    open({str(out)!r}, 'w').write(repr((argv, plugin_dir, control_base)))\n"
        "    return 7\n",
    )
    rc = ca.main(["--control-socket", "--model", "x"])
    assert rc == 7
    argv, plugin_dir, control_base = eval(out.read_text(encoding="utf-8"))
    assert "--control-socket" not in argv
    assert argv == ["claude", "--plugin-dir", str(ca._test.plugin), "--model", "x"]
    assert plugin_dir == str(ca._test.plugin)
    assert control_base == os.path.join(str(ca._test.base), "coordinator", "control-socket")
    assert ca._test.launched == []


def test_missing_package_exits_1_without_launch(ca, capsys):
    rc = ca.main(["--control-socket"])
    assert rc == 1
    assert ca._test.launched == []
    err = capsys.readouterr().err
    assert "supervisor" in err and err.count("\n") == 1


def test_supervisor_exception_exits_1_without_launch(ca, capsys):
    _stub_supervisor(ca._test.plugin, "def run(argv, plugin_dir, control_base):\n    raise RuntimeError('boom')\n")
    assert ca.main(["--control-socket"]) == 1
    assert ca._test.launched == []
    assert "boom" in capsys.readouterr().err


def test_vanilla_with_flag_exits_2(ca, capsys):
    assert ca.main(["--vanilla", "--control-socket"]) == 2
    assert "not available with --vanilla" in capsys.readouterr().err
    assert ca._test.launched == []


def test_dry_run_supervise_lines(ca, capsys):
    assert ca.main(["--dry-run", "--control-socket"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"supervise claude --plugin-dir {ca._test.plugin}"
    assert lines[1] == "control-socket: " + os.path.join(str(ca._test.base), "coordinator", "control-socket")
    assert lines[2] == "forwarded-flags: "
    assert "--control-socket" not in "\n".join(lines[2:])


def test_no_flag_dry_run_unchanged(ca, capsys):
    assert ca.main(["--dry-run"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"exec claude --plugin-dir {ca._test.plugin}", "forwarded-flags: "]


def test_no_flag_launches_plain(ca):
    assert ca.main(["--model", "x"]) == 0
    assert ca._test.launched == [["claude", "--plugin-dir", str(ca._test.plugin), "--model", "x"]]
