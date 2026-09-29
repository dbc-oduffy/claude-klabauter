"""resolve_plugin_root: env wins; else the installed cache is preferred over a source clone."""
from __future__ import annotations

from coordinator_core.trusted_root_guard import (
    installed_cache_plugin_root,
    is_trusted,
    resolve_plugin_root,
)


def _cache(home, *versions):
    base = home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator"
    for v in versions:
        (base / v).mkdir(parents=True)
    return base


def test_explicit_env_wins(tmp_path):
    env = {"HOME": str(tmp_path), "CLAUDE_PLUGIN_ROOT": "/x/y"}
    assert resolve_plugin_root("/clone", env=env) == "/x/y"


def test_cache_is_preferred_over_source_clone(tmp_path):
    base = _cache(tmp_path, "4.3.0", "4.10.0", "4.9.1")
    env = {"HOME": str(tmp_path)}
    assert resolve_plugin_root(str(tmp_path / "clone"), env=env) == str(base / "4.10.0")
    assert installed_cache_plugin_root(env) == str(base / "4.10.0")


def test_own_cache_root_is_kept(tmp_path):
    base = _cache(tmp_path, "4.3.0", "4.4.0")
    env = {"HOME": str(tmp_path)}
    own = str(base / "4.3.0")
    assert resolve_plugin_root(own, env=env) == own
    assert is_trusted(own, env=env) is True


def test_no_cache_falls_back_to_own_root(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert resolve_plugin_root("/clone", env=env) == "/clone"
    assert is_trusted("/clone", env=env) is False
