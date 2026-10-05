"""coordinator_core.content_root

Resolves the coordinator content root: the directory holding the plugin's
`agents/`, `snippets/`, `subagent-sandbox-policy.yaml` (directly, or nested
under a `coordinator/` subdirectory in an authoring clone).

Resolution order, first non-empty wins:
    1. registry `repos.content_root`
    2. `<settings-home>/machine-local/.coordinator-content-root`
    3. `<claude-home>/.claude/.coordinator-content-root`
    4. legacy registry key and legacy pointer files (compat fallback, below)
    5. the installed plugin root: `CLAUDE_PLUGIN_ROOT`, then
       `<claude-config-dir>/plugins/coordinator-claude` -- no hand-set key needed

Read-only; never raises. Returns "" when nothing resolves. The returned path is
not validated beyond the rung-5 content probe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from coordinator_core._settings_home import claude_config_dir, settings_home
from coordinator_core.machine_resolver import _registry_get_exact, registry_get, registry_set

# Writes a machine-local content-root pointer under settings home, outside the tracked tree.
GENERATES = []

CONTENT_ROOT_KEY = "repos.content_root"
POINTER_NAME = ".coordinator-content-root"

# compat-fallback: names read only so pre-rename boxes keep resolving.
_LEGACY_KEY = "repos.content_root"  # private-name-ok: compat-fallback
_LEGACY_POINTER = ".coordinator-content-root"  # private-name-ok: compat-fallback
_LEGACY_ENGINE_KEY = "engine.working_repos.content_root"  # private-name-ok: compat-fallback
ENGINE_CONTENT_ROOT_KEY = "engine.working_repos.content_root"


def _home_context() -> tuple[str, bool]:
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    return home, bool(override)


def _read_pointer(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""


def read_pointer_files(home: str | None = None, names: tuple[str, ...] = (POINTER_NAME, _LEGACY_POINTER)) -> str:
    """First non-empty pointer file across `names`, durable location before `<home>/.claude`."""
    if home is None:
        home_env, override = _home_context()
        if not (home_env or override):
            return ""
        home = home_env
    for name in names:
        candidates = [settings_home() / "machine-local" / name]
        if home:
            candidates.append(Path(home) / ".claude" / name)
        for candidate in candidates:
            content = _read_pointer(candidate)
            if content:
                return content
    return ""


def installed_plugin_root() -> str:
    """The installed plugin's content directory, probed for `agents/` or `snippets/`."""

    def _has_content(candidate: Path) -> bool:
        return (candidate / "agents").is_dir() or (candidate / "snippets").is_dir()

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root and _has_content(Path(env_root)):
        return env_root
    try:
        base = claude_config_dir() / "plugins" / "coordinator-claude"
    except Exception:
        return ""
    for candidate in (base / "coordinator", base):
        if _has_content(candidate):
            return str(candidate)
    return ""


def read_content_root() -> str:
    home, override = _home_context()
    if home or override:
        value = registry_get(CONTENT_ROOT_KEY) or read_pointer_files(home or None, (POINTER_NAME,))
        if value:
            return value
        value = registry_get(_LEGACY_KEY) or read_pointer_files(home or None, (_LEGACY_POINTER,))
        if value:
            return value
    if home or os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return installed_plugin_root()
    return ""


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of `migrate_legacy_config`. `migrated` is False on a no-op run."""

    migrated: bool
    value: str = ""
    source: str = ""
    written: tuple[str, ...] = ()


def _legacy_pointer_candidates(home: str) -> list[Path]:
    candidates = [settings_home() / "machine-local" / _LEGACY_POINTER]
    if home:
        candidates.append(Path(home) / ".claude" / _LEGACY_POINTER)
    return candidates


def _find_legacy_source(home: str) -> tuple[str, str, Path | None]:
    """(value, source label, legacy pointer file it came from or None); value "" when none resolves."""
    for key in (_LEGACY_KEY, _LEGACY_ENGINE_KEY):
        value = registry_get(key)
        if value:
            return value, key, None
    for candidate in _legacy_pointer_candidates(home):
        value = _read_pointer(candidate)
        if value:
            return value, str(candidate), candidate
    return "", "", None


def migrate_legacy_config() -> MigrationResult:
    """Copy a legacy-named root into the content-root registry key and pointer.

    Acts only when `repos.content_root` is absent or empty and a legacy source resolves.
    Never deletes the legacy pointer file; `registry_set` retires the paired legacy registry
    key. A second call is a no-op; spawns no process.
    """
    home, override = _home_context()
    if not (home or override):
        return MigrationResult(False)
    if _registry_get_exact(CONTENT_ROOT_KEY):
        return MigrationResult(False)
    value, source, legacy_pointer = _find_legacy_source(home)
    if not value:
        return MigrationResult(False)
    written = []
    registry_set(CONTENT_ROOT_KEY, value)
    written.append(CONTENT_ROOT_KEY)
    if registry_get(_LEGACY_ENGINE_KEY) and not _registry_get_exact(ENGINE_CONTENT_ROOT_KEY):
        registry_set(ENGINE_CONTENT_ROOT_KEY, value)
        written.append(ENGINE_CONTENT_ROOT_KEY)
    pointer_dir = legacy_pointer.parent if legacy_pointer else settings_home() / "machine-local"
    pointer = pointer_dir / POINTER_NAME
    if not _read_pointer(pointer):
        pointer_dir.mkdir(parents=True, exist_ok=True)
        pointer.write_text(value + "\n", encoding="utf-8", newline="\n")
        written.append(str(pointer))
    return MigrationResult(True, value, source, tuple(written))
