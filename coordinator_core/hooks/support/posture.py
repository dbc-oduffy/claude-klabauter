"""Shared, fail-open `engagement_posture` resolver.

Ported from DoE-claude `coordinator/hooks/scripts/_posture.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C4. ADAPTATION (the
class-1 site named in this package's own `__init__.py` docstring): DoE's
copy resolved the consuming repo root via a sibling-import of
`_engine_root._session_repo_root` (CLAUDE_PROJECT_DIR when set and real,
else a zero-spawn upward walk for a `.git` entry). That sibling module is
not part of this chunk's footprint and cannot be imported across the
doctrine/engine boundary, so `_find_repo_root` now calls
`coordinator_core.ops._git_root_util.git_root_zero_spawn` directly -- the
engine's own equivalent primitive, same resolution order (CLAUDE_PROJECT_DIR
first, honoured only for an in-process caller per that module's own hazard
note about a warm-server process inheriting a stranger's env; a zero-spawn
upward `.git` walk otherwise). Everything else (the two-rung resolution --
per-repo `coordinator.local.md` override, then the machine-local identity
file -- the fail-open-to-"precision" contract, and the per-process caching)
is unchanged from the source.

Spec backlink: docs/plans/2026-08-10-posture-scaled-autonomous-disposition.md (chunk C1).

Resolution order:
  1. `coordinator.local.md` frontmatter key `engagement_posture`, at the
     CONSUMING repo root, when it resolves and the key is present
     (per-repo override). The consuming repo root is resolved via
     `git_root_zero_spawn` (see the ADAPTATION note above) -- NOT a walk
     from this file's own `__file__`, which would only ever find this
     engine's own checkout.
  2. `~/.claude/coordinator-identity.yaml` key `engagement_posture` (the
     durable machine-local record).
  3. Fail open to "precision".

FAIL-OPEN DIRECTION IS LOAD-BEARING: "precision" is the anchor whose
behaviour stays unchanged. Every failure path -- missing file, unreadable
file, unparseable content, absent key, value outside the enum -- returns
"precision". An unreadable identity file degrades to "change nothing",
never to "start blocking".

Both consulted files are flat `key: value` text (a YAML-flavored
frontmatter block and a flat YAML mapping respectively); a line-scan parser
is correct here and avoids adding a YAML dependency on a hot path.
"""

from __future__ import annotations

import os

from coordinator_core.ops._git_root_util import git_root_zero_spawn

_VALID_POSTURES = frozenset({"precision", "default", "substrate-free"})
# posture in the enum. A `_FAIL_OPEN_` prefix would read as the opposite.
_MOST_CAUTIOUS_POSTURE = "precision"

_cached_posture: str | None = None
_cached_posture_by_root: dict[str, str] = {}


def _extract_key_from_lines(lines, key: str) -> str | None:
    prefix = key + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                return value
    return None


def _read_key_from_file(path: str, key: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    except UnicodeDecodeError:
        return None
    return _extract_key_from_lines(lines, key)


def _find_repo_root() -> str | None:
    """Anchor at the CONSUMING repo root: `CLAUDE_PROJECT_DIR` when set and a
    real directory (in-process caller only), else a zero-spawn upward walk
    for a `.git` entry (directory for a normal clone, file for a worktree).

    Delegates to `coordinator_core.ops._git_root_util.git_root_zero_spawn` --
    the engine's own root-resolution primitive, reused rather than
    reimplemented (see this module's own ADAPTATION note above). Previously
    walked upward from THIS FILE's own `__file__` looking for a directory
    containing `coordinator.local.md`, which only ever resolves this
    plugin's own checkout -- correct by accident in a dev repo where
    `--plugin-dir` points the plugin at the working tree itself, and silent
    dead weight on a marketplace install where the plugin lives under
    `~/.claude/plugins/` and the consumer's `coordinator.local.md` lives
    somewhere `__file__` can never reach."""
    try:
        return git_root_zero_spawn()
    except Exception:
        return None


def _resolve_posture_from(repo_root: str | None) -> str:
    try:
        root = repo_root if repo_root is not None else _find_repo_root()
        if root is not None:
            local_md = os.path.join(root, "coordinator.local.md")
            value = _read_key_from_file(local_md, "engagement_posture")
            if value is not None:
                value = value.lower()
            if value in _VALID_POSTURES:
                return value

        # WS-2 home-resolution shape: CLAUDE_HOME first, `Path.home()` as the terminal
        from pathlib import Path
        claude_home = os.environ.get("CLAUDE_HOME") or Path.home()
        identity_path = os.path.join(claude_home, ".claude", "coordinator-identity.yaml")
        value = _read_key_from_file(identity_path, "engagement_posture")
        if value is not None:
            value = value.lower()
        if value in _VALID_POSTURES:
            return value
    except Exception:
        pass

    return _MOST_CAUTIOUS_POSTURE


def resolve_posture(repo_root: str | None = None) -> str:
    """Return the resolved engagement posture, one of "precision",
    "default", "substrate-free". Fails open to "precision" on any
    unreadable/unparseable/absent/out-of-enum condition, and on any other
    exception raised anywhere in the resolution body (path walk, expanduser,
    etc.) -- callers importing this module get the fail-open contract
    unconditionally, not just for the two guarded I/O paths.

    `repo_root` (optional): an already-resolved consuming-repo root to
    anchor at directly, bypassing `_find_repo_root()`'s own CLAUDE_PROJECT_DIR/
    `.git`-walk anchoring. Every existing caller passes nothing and gets
    byte-identical behaviour to before this parameter existed.

    Cached per process, in one of two module-level stores depending on call
    shape -- a call with a different `repo_root` is NEVER served a value
    cached under a prior root:
      - no `repo_root` (or falsy): `_cached_posture`, a bare scalar, matching
        this function's pre-existing single-value cache exactly.
      - explicit `repo_root`: `_cached_posture_by_root`, keyed on the exact
        `repo_root` string passed in.
    """
    global _cached_posture

    if repo_root:
        if repo_root in _cached_posture_by_root:
            return _cached_posture_by_root[repo_root]
        result = _resolve_posture_from(repo_root)
        _cached_posture_by_root[repo_root] = result
        return result

    if _cached_posture is not None:
        return _cached_posture
    _cached_posture = _resolve_posture_from(None)
    return _cached_posture
