"""host_registry: the one host declaration, importable by package and by file path."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.install import host_registry as pkg_registry

_FILE = Path(pkg_registry.__file__)


def _by_path():
    spec_ = importlib.util.spec_from_file_location("host_registry_by_path", _FILE)
    mod = importlib.util.module_from_spec(spec_)
    sys.modules[spec_.name] = mod  # dataclass resolution needs the module registered
    spec_.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mod", [pkg_registry, None], ids=["package", "file-path"])
def test_ids_and_foreign(mod):
    mod = mod or _by_path()
    assert tuple(s.id for s in mod.REGISTRY) == ("claude", "codex", "agy", "grok")
    assert mod.FOREIGN == ("codex", "agy", "grok")
    assert mod.ids() == mod.IDS


def test_plugin_hosts_are_claude_and_grok():
    assert pkg_registry.plugin_host_ids() == ("claude", "grok")


def test_spec_unknown_raises_and_known_returns():
    assert pkg_registry.spec("grok").home_default == ".grok"
    with pytest.raises(ValueError):
        pkg_registry.spec("gemini")


def test_home_default_and_override(tmp_path):
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    assert pkg_registry.home(pkg_registry.spec("agy"), env) == tmp_path / ".gemini"
    assert pkg_registry.home(pkg_registry.spec("codex"), env) == tmp_path / ".codex"
    other = tmp_path / "elsewhere"
    assert pkg_registry.home(pkg_registry.spec("grok"), {**env, "GROK_HOME": str(other)}) == other
    assert pkg_registry.home(pkg_registry.spec("claude"), {**env, "CLAUDE_HOME": str(other)}) == other / ".claude"


def test_user_home_picks_platform_key(tmp_path):
    env = {"HOME": str(tmp_path / "h"), "USERPROFILE": str(tmp_path / "u")}
    assert pkg_registry.user_home(env, "win32") == tmp_path / "u"
    assert pkg_registry.user_home(env, "linux") == tmp_path / "h"


def test_binaries_carry_windows_suffixes():
    for host in pkg_registry.FOREIGN:
        s = pkg_registry.spec(host)
        assert s.binaries == (host, f"{host}.exe", f"{host}.cmd")
