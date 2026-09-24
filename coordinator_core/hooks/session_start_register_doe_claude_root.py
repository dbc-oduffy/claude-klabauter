"""coordinator_core.hooks.session_start_register_doe_claude_root —
SessionStart(*) op: self-heals `engine.working_repos.doe_claude` and
`repos.doe_claude` in the machine-local registry.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/session-start-register-doe-claude-root.py`. That
script self-resolved DoE-claude's own root from `__file__` (rung 0, a
`parents[3]` walk correct only because the script lived three levels under
DoE-claude's own `coordinator/hooks/scripts/` tree) with a payload-cwd scan as
a fallback (rung 1, for the OSS-mirror layout where `__file__` shares no path
prefix with the fleet's real clones).

ADAPTATION (class 1, per this package's own conventions): this module now
lives INSIDE the engine, whose own `__file__` never resolves to a DoE-claude
checkout at all — rung 0 would be structurally wrong here, not merely
layout-specific. Rung 1 (the payload-cwd scan, gated on the SAME wrong-repo
guard: `.coordinator-dev-repo` sentinel + `slug: doe-claude`) is therefore the
ONLY resolution rung this port keeps — it was already correct for the
"__file__ shares no path prefix with the real clones" case, and this op's own
`__file__` never shares that prefix either. `session's repo through the
payload cwd`, per the row's own body.

Registry read/write is the SECOND adaptation: DoE's `_registry_value`/
`_settings_home_registry_dir`/`machine_local_set` (a subprocess-spawning
`machine-local set` CLI call under `_registry_write`'s resolution ladder) are
replaced with `coordinator_core.machine_resolver.registry_get`/`registry_set`
— the engine's own in-process, zero-subprocess TOML reader/writer for the
identical `registry.local.toml` file (DR-071 public promotion; see that
module's own docstring). No second write path is invented.

Op contract: `params["payload"]["cwd"]` is the only field read (rung 1's scan
anchor). Every failure mode degrades to `no_advisory()`/a silent no-op — a
SessionStart op that raises greets every session with a stack trace, worse
than a no-op. Idempotent: writes nothing when the registry already carries
this resolved root.

Negative-spec:
    Does NOT resolve rung 0 (`__file__`-anchored candidate) — see ADAPTATION
    above; this module's own `__file__` is never a DoE-claude checkout path.
    Does NOT shell out to a `machine-local` CLI — `registry_set` writes
    `registry.local.toml` directly, in-process.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import registry_get, registry_set

_REGISTRY_KEY = "engine.working_repos.doe_claude"
_REPOS_REGISTRY_KEY = "repos.doe_claude"
_SENTINEL_NAME = ".coordinator-dev-repo"
_EXPECTED_SLUG = "doe-claude"

#: Pointer files every no-launcher fence reads to find the doctrine clone.
_DOE_ROOT_POINTER_BASENAME = ".doe-root"

#: Ceiling on directories stat'd by the scan. A bounded probe of named
#: positions, never a filesystem walk.
_MAX_SCAN_CANDIDATES = 64


def _immediate_subdirs(base: Path) -> "list[Path]":
    try:
        return sorted(child for child in base.iterdir() if child.is_dir())
    except Exception:
        return []


def _is_genuine_doe_claude_repo(root: Path) -> bool:
    """Wrong-repo guard. Never raises; any read failure is "not confirmed" —
    the fail-closed direction for a guard preventing a false-positive
    registration. Exact `slug:` equality, never a substring match (would
    also match a prefix-sharing slug such as `doe-claude-fork`)."""
    sentinel = root / _SENTINEL_NAME
    try:
        if not sentinel.is_file():
            return False
        text = sentinel.read_text(encoding="utf-8")
    except Exception:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("slug:"):
            slug = stripped[len("slug:"):].strip()
            return slug == _EXPECTED_SLUG
    return False


def _scan_for_doe_clone(payload_cwd: "Optional[str]") -> "Optional[Path]":
    if not payload_cwd:
        return None
    try:
        start = Path(payload_cwd).resolve()
    except Exception:
        return None

    seen: "set[Path]" = set()
    checked = 0
    for base in (start, *list(start.parents)[:3]):
        for candidate in (base, *_immediate_subdirs(base)):
            if candidate in seen:
                continue
            seen.add(candidate)
            checked += 1
            if checked > _MAX_SCAN_CANDIDATES:
                return None
            try:
                if _is_genuine_doe_claude_repo(candidate):
                    return candidate
            except Exception:
                continue  # per-candidate probe; one unreadable candidate must not abort the scan
    return None


def _doe_root_pointer_paths() -> "list[Path]":
    paths: "list[Path]" = []
    try:
        paths.append(settings_home() / _DOE_ROOT_POINTER_BASENAME)
    except Exception:
        pass  # settings-home leg unresolvable; the other pointer path leg still covers it
    try:
        paths.append(home_dir() / ".claude" / _DOE_ROOT_POINTER_BASENAME)
    except Exception:
        pass  # home-dir leg unresolvable; the other pointer path leg still covers it
    return paths


def _write_doe_root_pointer(root_str: str) -> None:
    """Write-when-absent-or-different (unlike `repos.doe_claude`, which is
    write-when-absent-only — see `_maybe_seed_repos_doe_claude`). Fail open
    per leg: one unwritable location never stops the other.

    Atomic (tmp + replace): sibling sessions read these pointers at the same
    SessionStart, and a truncate-then-write exposes a blank file to them."""
    for pointer in _doe_root_pointer_paths():
        try:
            if pointer.is_file() and pointer.read_text(encoding="utf-8").strip() == root_str:
                continue
        except Exception:
            pass  # existing-content check is best-effort; fall through and (re)write the pointer
        tmp = pointer.with_name(f"{pointer.name}.{os.getpid()}.tmp")
        try:
            pointer.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(root_str + "\n", encoding="utf-8", newline="\n")
            os.replace(tmp, pointer)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass  # best-effort tmp-file cleanup; a leftover tmp file does not affect correctness
            continue


def _maybe_seed_repos_doe_claude(root_str: str) -> None:
    """Write-when-absent-only seed for `repos.doe_claude` — an operator may
    legitimately point this key at a different clone; never overwritten."""
    try:
        current = registry_get(_REPOS_REGISTRY_KEY)
    except Exception:
        current = None
    if current:
        return
    registry_set(_REPOS_REGISTRY_KEY, root_str)


@register_op("hooks.session_start_register_doe_claude_root")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()

    try:
        root = _scan_for_doe_clone(cwd)
    except Exception:
        root = None
    if root is None:
        return no_advisory()  # not confirmed anywhere -- never register

    root_str = str(root)

    try:
        _write_doe_root_pointer(root_str)
    except Exception:
        pass  # pointer write is best-effort; the registry write below still reports the root

    try:
        current = registry_get(_REGISTRY_KEY)
    except Exception:
        current = None

    if current != root_str:
        try:
            registry_set(_REGISTRY_KEY, root_str)
        except Exception:
            pass  # registry write is best-effort; the pointer files above still record the root

    try:
        _maybe_seed_repos_doe_claude(root_str)
    except Exception:
        pass  # seed-if-absent is best-effort; an operator-set registry value is never at risk here

    return no_advisory()
