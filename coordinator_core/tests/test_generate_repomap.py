"""Characterization tests for coordinator_core.ops.generate_repomap.

Built from the golden-oracle capture of Port of: generate-repomap.sh
(example-doctrine-repo b5a4192c, 2026-07-20) run against real corpora — trust-guard fail-loud
path, generator-not-found 3-tier resolution, default-args and
passthrough-args invocation shapes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops.generate_repomap import _resolve_python_cmd, _trusted_root, main
from coordinator_core.win_portability import no_console_creationflags


# ---------------------------------------------------------------------------
# _trusted_root — trust-core boundary cases (mirrors the bash trust-core)
# ---------------------------------------------------------------------------

def test_trusted_root_under_claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    (tmp_path / ".claude").mkdir()
    assert _trusted_root(str(tmp_path / ".claude" / "plugins" / "x")) is True


def test_trusted_root_under_doe_root_sentinel(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    doe = tmp_path / "doe-clone"
    doe.mkdir()
    (home / ".claude" / ".doe-root").write_text(str(doe) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    assert _trusted_root(str(doe / "coordinator")) is True


def test_untrusted_root_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    assert _trusted_root("/tmp/fake-plugin-root-xyz") is False


def test_trusted_root_opt_out(monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    assert _trusted_root("/tmp/anything") is True


def test_trusted_root_rejects_dotdot_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    (tmp_path / ".claude").mkdir()
    bad = str(tmp_path / ".claude" / "..") + "/evil"
    assert _trusted_root(bad) is False


# ---------------------------------------------------------------------------
# main — fail-loud paths
# ---------------------------------------------------------------------------

def test_main_untrusted_root_exits_1(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    rc = main([], plugin_root="/tmp/fake-plugin-root-xyz")
    assert rc == 1
    assert "outside trusted prefix" in capsys.readouterr().err


def test_main_generator_not_found_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "nonexistent-home"))
    rc = main([], plugin_root=str(tmp_path / "no-such-plugin-root"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "generate-repomap.py not found" in err
    assert "Expected (tier 1)" in err
    assert "Fallback (tier 2, legacy)" in err
    assert "Fallback  (tier 3)" in err


# ---------------------------------------------------------------------------
# main — happy paths (subprocess mocked, resolution + arg-building verified)
# ---------------------------------------------------------------------------

def _make_generator(plugin_root: Path) -> Path:
    gen_dir = plugin_root / "bin" / "repomap"
    gen_dir.mkdir(parents=True)
    gen = gen_dir / "generate-repomap.py"
    gen.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return gen


def test_main_default_args_when_no_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    gen = _make_generator(tmp_path)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)

    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("coordinator_core.ops.generate_repomap.subprocess.run", _fake_run)
    rc = main([], plugin_root=str(tmp_path))
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[-7:] == [str(gen), "--project-root", ".", "--budget", "4000", "--profile", "balanced"]
    assert captured["kwargs"] == no_console_creationflags()


def test_main_passthrough_args_take_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    gen = _make_generator(tmp_path)

    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class _Result:
            returncode = 3

        return _Result()

    monkeypatch.setattr("coordinator_core.ops.generate_repomap.subprocess.run", _fake_run)
    rc = main(["--task", "foo", "--budget", "999"], plugin_root=str(tmp_path))
    assert rc == 3
    cmd = captured["cmd"]
    assert cmd[-5:] == [str(gen), "--task", "foo", "--budget", "999"]


def test_main_no_interpreter_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    _make_generator(tmp_path)
    monkeypatch.setattr("coordinator_core.ops.generate_repomap.shutil.which", lambda _: None)
    monkeypatch.delenv("PYTHON", raising=False)
    rc = main([], plugin_root=str(tmp_path))
    assert rc == 1
    assert "no python interpreter found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _resolve_python_cmd — interpreter probe order + override
# ---------------------------------------------------------------------------

def test_resolve_python_cmd_override(monkeypatch):
    monkeypatch.setenv("PYTHON", "/usr/bin/python3.11")
    assert _resolve_python_cmd() == ["/usr/bin/python3.11"]


def test_resolve_python_cmd_prefers_python3(monkeypatch):
    monkeypatch.delenv("PYTHON", raising=False)
    monkeypatch.setattr(
        "coordinator_core.ops.generate_repomap.shutil.which",
        lambda name: "/usr/bin/python3" if name == "python3" else None,
    )
    assert _resolve_python_cmd() == ["python3"]
