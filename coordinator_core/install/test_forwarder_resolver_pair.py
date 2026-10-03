"""Pair invariant: every bare-Python forwarder written into bin_dst imports a
``_resolve_*.py`` module that is present in bin_dst after the run."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from coordinator_core.install import substrate

_RESOLVER_SRC = (
    Path(__file__).resolve().parents[2]
    / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)
_IMPORT_RE = re.compile(r"^from (_resolve_[A-Za-z0-9_]+) import exec_cli", re.MULTILINE)
_TARGETS = {"alpha-tool": "alpha-tool.py", "beta-tool": "beta-tool.py"}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(tmp_path / "locks"))


def test_forwarders_name_the_installed_klabauter_resolver(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    shutil.copyfile(_RESOLVER_SRC, bin_dst / "_resolve_claude_klabauter.py")

    substrate._write_agent_helper_forwarders(
        _TARGETS, bin_dst, False, engine_root=None,
        resolver_module=substrate._installed_resolver_module(bin_dst),
    )

    written = [bin_dst / n for n in _TARGETS]
    assert all(p.is_file() for p in written)
    for p in written:
        modules = _IMPORT_RE.findall(p.read_text(encoding="utf-8"))
        assert modules == ["_resolve_claude_klabauter"]
        assert (bin_dst / f"{modules[0]}.py").is_file()


def test_unspecified_resolver_is_derived_from_bin_dst(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    shutil.copyfile(_RESOLVER_SRC, bin_dst / "_resolve_claude_klabauter.py")

    substrate._write_agent_helper_forwarders(_TARGETS, bin_dst, False, engine_root=None)

    for name in _TARGETS:
        text = (bin_dst / name).read_text(encoding="utf-8")
        assert _IMPORT_RE.findall(text) == ["_resolve_claude_klabauter"]


def test_ambiguous_resolvers_fail_loud_without_a_dangling_forwarder(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    (bin_dst / "_resolve_one.py").write_text("", encoding="utf-8")
    (bin_dst / "_resolve_two.py").write_text("", encoding="utf-8")

    with pytest.raises(substrate.SubstrateFatalError):
        substrate._write_agent_helper_forwarders(_TARGETS, bin_dst, False, engine_root=None)

    assert not any((bin_dst / n).exists() for n in _TARGETS)


def test_ambiguous_resolvers_prefer_running_images_spelling(tmp_path):
    (tmp_path / "_resolve_claude_klabauter.py").write_text("", encoding="utf-8")
    (tmp_path / "_resolve_other.py").write_text("", encoding="utf-8")
    with pytest.raises(OSError):
        substrate._installed_resolver_module(tmp_path)
    (tmp_path / f"{substrate._AGENT_RESOLVER_MODULE}.py").write_text("", encoding="utf-8")
    assert substrate._installed_resolver_module(tmp_path) == substrate._AGENT_RESOLVER_MODULE
