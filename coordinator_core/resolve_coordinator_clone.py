"""
coordinator_core.resolve_coordinator_clone — native peer of coordinator-claude's
``coordinator/lib/resolve-coordinator-clone.sh`` (the unified coordinator
install-root resolver), CLI-invocation-mode only.

Purpose: the shared coordinator-claude-oracle resolver that C11's 13 call sites (this plan's
narrowed surface) fold into rather than each re-deriving its own
``bash resolve-coordinator-clone.sh --clone-root|--content-root`` subshell.
Companion to the already-native ``coordinator_core.state_root``
(``coordinator_state_root`` peer) — together the two modules retire every
"shell out to a coordinator-claude resolver lib" bridge in C11's surface.

Scope note — CLI mode only, not sourced mode: all of this plan's call sites
invoke the bash oracle as a standalone subprocess (``bash script.sh
--clone-root``), never ``source`` it. This port therefore covers the
oracle's standalone-CLI branch (``_rcc_resolve_git_ops`` /
``_rcc_resolve_content`` plus their shared mode selector and registry/pointer/
cache helpers) — NOT the sourced-mode branch at the bottom of the bash file
(caller-scope ``COORDINATOR_CLONE``/``COORDINATOR_CONTENT_ROOT`` variable
export, or its ``trusted_root_guard`` fail-open probe). No caller in this
plan's surface sources the oracle, so that branch has no native consumer to
port for; a future caller that needs sourced-mode semantics ports that branch
separately rather than overloading this module's contract.

Port of: resolve-coordinator-clone.sh (coordinator-claude 290997c7, 2026-07-22), 804 lines.
This is the Python-native mirror for claude-klabauter-resident callers, exactly as
``coordinator_core.state_root`` and ``coordinator_core.ops.coordinator_doe_root``
are for their oracles.

Public API:
    resolve_clone_root() -> str
        Native peer of ``--clone-root`` (alias ``--for-git-ops``): the
        .git-backed clone directory. Raises ResolveCoordinatorCloneError on
        any failure (ambiguous source mode, no git-backed clone found).

    resolve_content_root() -> str
        Native peer of ``--content-root`` (alias ``--for-content``): the
        highest-precedence readable payload directory. Raises
        ResolveCoordinatorCloneError on any failure.

    main(argv) -> int
        CLI-shaped wrapper preserving the bash oracle's exit codes for parity
        testing: 0 on success (path on stdout, no trailing newline), 1 on a
        resolution failure (remediation on stderr), 2 on a usage error (wrong
        arg count / unknown flag) — mirrors the oracle's ``exit 2`` usage path,
        distinct from ``coordinator_core.state_root``'s 0/1/2 shape (whose rc-2
        means "cross-cutting artifact", not "usage error"; the two modules'
        rc-2 do NOT mean the same thing — do not conflate them).

Five-rung git-ops ladder / seven-rung content ladder, and the shared dev-vs-oss
source-mode selector, are reproduced verbatim from the bash oracle's own
header comment — see docstrings on `_resolve_source_mode`, `resolve_clone_root`,
`resolve_content_root` below for the rung-by-rung mapping.

Negative-spec (faithfully reproduced — do NOT "fix" mid-port):
    - Does NOT implement the sourced-mode branch (caller-scope variable
      export, the inline ``trusted_root_guard`` fail-open subprocess probe at
      the bottom of the bash file) — see Scope note above.
    - Does NOT reimplement the four composed peers this module is a SIBLING
      of (``coordinator_core.state_root``, ``coordinator_core.ops.
      coordinator_doe_root``, ``coordinator_core.claude_klabauter_root``,
      ``coordinator_core.doe_root_pointer``) — no second settings-home/
      state-root/clone resolver.
    - The pointer-file read (``_read_doe_root_pointer``) now DELEGATES to
      ``coordinator_core.doe_root_pointer.read_doe_root_pointer`` rather than
      reimplementing it locally (fixed 2026-07-22, DR-071): that shared port
      previously implemented only the LEGACY rung, which was stale relative
      to the durable-then-legacy bash oracle and would have silently
      mis-resolved on a migrated machine; it has since been updated to the
      full DR-071 order (registry `repos.example_doctrine_repo` -> durable
      `<settings-home>/machine-local/.doe-root` -> legacy
      `${CLAUDE_HOME:-$HOME}/.claude/.doe-root`), so delegating here is now
      correct and deletes what would otherwise be a second, drift-prone copy
      of the same read order.
    - `_registry_example_doctrine_repo` reads the registry canonical key via
      ``coordinator_core.machine_resolver.registry_get`` — a direct-tomllib
      read, not the ``machine-local`` CLI — for the same reset-safety reason
      documented on ``registry_get`` and ``doe_root_pointer``: the CLI's
      reader/exec bits live under the resettable ``~/.claude/bin/``, so
      "`machine-local get` works" is not proof of reset-survival. The CLI
      subprocess (``_machine_local_get``) is retained as a fallback rung only
      (covers a future/exotic case where `registry_get`'s settings-home
      resolution can't find the registry but the CLI's own resolution can),
      never the primary read.
    - `_rcc_registry_live_path`'s bash original parses ``registry.local.toml``
      directly (nested-table AND flat-dotted-key forms) as a defensive
      fallback for when ``machine-local get`` doesn't surface nested-table
      values. This port instead reads the same key via ``registry_get``
      (direct tomllib, no subprocess) first and only shells out to
      ``machine-local get plugin.mirrors.coordinator-claude.live_path`` when
      that fails to resolve — same two-rung shape ``_registry_example_doctrine_repo``
      above already uses for `repos.example_doctrine_repo`, not a second convention.
      (Originally ported as CLI-only; that was a hot-path spawn defect fixed
      2026-07-28 — see `_registry_live_path`'s own docstring.)
    - Versioned-cache newest-wins comparison is numeric major.minor.patch
      (DR-148-safe), matching the bash oracle's non-``sort -rV`` loop.
    - `--print-map`-shaped standalone modes do not exist on this oracle
      (that flag belongs to ``coordinator_state_root`` only) — not ported.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.doe_root_pointer import read_doe_root_pointer as _read_doe_root_pointer
from coordinator_core.machine_resolver import registry_get

_SUBPROCESS_TIMEOUT_SECS = 15


class ResolveCoordinatorCloneError(RuntimeError):
    """Rc-1 shape: any resolution failure (ambiguous source mode, no
    git-backed clone / no readable content root found, bad
    COORDINATOR_SOURCE_MODE value)."""


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


def _claude_home_dir() -> Optional[str]:
    """``${CLAUDE_HOME:-$HOME}/.claude`` — mirrors `_rcc_claude_home_dir`."""
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return os.path.join(home, ".claude") if home else None


def _machine_local_get(key: str) -> Optional[str]:
    """``machine-local get <key>`` -> stripped stdout on success, None on any
    failure (missing binary, non-zero exit, timeout, empty output) — same
    discard-and-continue shape as ``coordinator_core.ops.coordinator_doe_root.
    _machine_local_get`` (not re-derived; this module has its own copy since
    that one is private to its module, but the contract is identical)."""
    ml_bin = shutil.which("machine-local")
    if ml_bin is None:
        return None
    try:
        result = subprocess.run(
            [ml_bin, "get", key],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        # Missing binary / timeout — discard-and-continue per docstring; a
        # caller without machine-local configured hits this on every call,
        # so this stays a comment rather than a per-call warning.
        return None
    if result.returncode != 0:
        return None
    resolved = (result.stdout or "").strip()
    return resolved or None


def _registry_example_doctrine_repo() -> Optional[str]:
    """Canonical registry key (DR-071) — mirrors `_rcc_registry_example_doctrine_repo`.

    Reads via ``machine_resolver.registry_get`` (direct tomllib) first, for
    the reset-safety reason documented on that function and on this module's
    negative-spec: the ``machine-local`` CLI's reader/exec bits live under
    the resettable ``~/.claude/bin/``, so its failure doesn't mean the
    registry itself is unresolvable. The CLI subprocess is retained as a
    fallback rung only.
    """
    value = registry_get("repos.example_doctrine_repo")
    if value:
        return value
    return _machine_local_get("repos.example_doctrine_repo")


def _registry_live_path() -> Optional[str]:
    """Fallback registry key — mirrors `_rcc_registry_live_path` (see module
    negative-spec for the direct-CLI-read simplification).

    Reads via ``machine_resolver.registry_get`` (direct tomllib) first, same
    reset-safety + no-subprocess reasoning as `_registry_example_doctrine_repo` above —
    this key was previously CLI-only, which meant every `resolve_content_root`
    call on a machine with `machine-local` on PATH spawned a ~80ms subprocess
    on this rung even though it sits on the COMMON path (rung 3 of 7, not a
    last-resort), because the direct-tomllib read was never tried first. Fixed
    2026-07-28 (spawn-on-hot-path defect). The CLI subprocess is retained as a
    genuine fallback rung — it still fires when `registry_get` can't resolve
    the key (e.g. this key present under `machine-local`'s CLI-managed state
    but not yet mirrored into `registry.local.toml`/`registry.toml`)."""
    value = registry_get("plugin.mirrors.coordinator-claude.live_path")
    if value:
        return value
    return _machine_local_get("plugin.mirrors.coordinator-claude.live_path")


def _newest_cache_dir() -> Optional[str]:
    """Newest ``major.minor.patch`` dir under
    ``<claude_home>/plugins/cache/coordinator-claude/coordinator/*/`` —
    numeric compare (DR-148-safe), mirrors `_rcc_newest_cache`. Returns None
    if the cache parent is absent or has no version-shaped children."""
    claude_home = _claude_home_dir()
    if not claude_home:
        return None
    cache_parent = Path(claude_home, "plugins", "cache", "coordinator-claude", "coordinator")
    if not cache_parent.is_dir():
        return None

    best: Optional[Path] = None
    best_key = (-1, -1, -1)
    for child in cache_parent.iterdir():
        if not child.is_dir():
            continue
        parts = child.name.split(".")
        nums: List[int] = []
        for part in (parts + ["0", "0", "0"])[:3]:
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            nums.append(int(digits) if digits else 0)
        key = (nums[0], nums[1], nums[2])
        if key > best_key:
            best_key = key
            best = child
    return str(best) if best is not None else None


def _flat_evidences_coordinator_tree(flat: Optional[str]) -> bool:
    """True if `flat` is a directory carrying actual evidence of a
    coordinator tree — `.git/` (a raw git checkout, the B6 motivating
    scenario) or the published manifest relpath. Mere directory existence is
    NOT evidence (see B6 review finding on `_resolve_source_mode`'s flat
    rung): an interrupted install, a `rm -rf <dir>/*` leftover, or a
    user-created placeholder directory must not classify the box as "dev"."""
    if not flat or not os.path.isdir(flat):
        return False
    if os.path.isdir(os.path.join(flat, ".git")):
        return True
    return os.path.isfile(os.path.join(flat, "schemas", "coordinator-registry.manifest.json"))


# ---------------------------------------------------------------------------
# Rung-0: dev-vs-oss source-mode selector, shared by both verbs.
# ---------------------------------------------------------------------------


def _resolve_source_mode(verb: str) -> str:
    """Returns exactly one of "dev" | "oss" | "passthrough". Raises
    ResolveCoordinatorCloneError on ambiguity, an invalid
    COORDINATOR_SOURCE_MODE value, or no source found at all. Mirrors
    `_rcc_resolve_source_mode` rung-for-rung:

      1. PASSTHROUGH — COORDINATOR_CLONE set (git-ops) or CLAUDE_PLUGIN_ROOT /
         COORDINATOR_ROOT set (content) -> "passthrough", unconditionally.
      2. Explicit COORDINATOR_SOURCE_MODE=dev|oss -> that value verbatim;
         any other non-empty value fails loud.
      3. Marker auto-discovery: a resolvable candidate clone (canonical
         registry -> fallback registry -> pointer, DR-071 rank order) with
         `.coordinator-dev-repo` present -> "dev". Candidate resolved +
         unmarked + OSS install also present -> fail loud (ambiguous). OSS
         install alone -> "oss". Candidate resolved, unmarked, no OSS present
         -> "dev" (sole candidate, non-ambiguous). Neither -> fail loud (no
         source).
    """
    if verb == "git-ops":
        if os.environ.get("COORDINATOR_CLONE"):
            return "passthrough"
    elif verb == "content":
        if os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("COORDINATOR_ROOT"):
            return "passthrough"
    else:  # pragma: no cover - defense-in-depth, unreachable via public API
        raise ResolveCoordinatorCloneError(
            f"resolve-coordinator-clone: internal error — unknown verb '{verb}'"
        )

    mode_env = os.environ.get("COORDINATOR_SOURCE_MODE")
    if mode_env:
        if mode_env in ("dev", "oss"):
            return mode_env
        raise ResolveCoordinatorCloneError(
            f'resolve-coordinator-clone: COORDINATOR_SOURCE_MODE is set to "{mode_env}" '
            "but must be \"dev\" or \"oss\""
        )

    claude_home = _claude_home_dir()
    flat = os.path.join(claude_home, "plugins", "coordinator-claude") if claude_home else None
    oss_present = bool(flat) and os.path.isfile(os.path.join(flat, ".claude-plugin", "plugin.json"))

    # DR-071: registry `repos.example_doctrine_repo` (canonical) ranks above the
    # `.doe-root` pointer file mirror — inverted 2026-07-22 from the prior
    # pointer-first order. The flat marketplace-clone layout is added as a
    # last-resort candidate rung (not just consulted via `oss_present` below)
    # so an unmarked flat clone with no `.claude-plugin/plugin.json` manifest
    # (e.g. a raw git checkout dropped at the flat path, no marketplace
    # manifest, no .coordinator-dev-repo marker) is still a resolvable
    # candidate instead of silently falling through to "no source found" —
    # gated on `not oss_present` so a genuine marketplace install (which DOES
    # carry the manifest) is never double-counted as both the OSS install AND
    # the "unmarked candidate" in the ambiguity check below.
    #
    # Review: B6 (MAJOR, 2026-08-08) -- this rung was previously gated on
    # `os.path.isdir(flat)` alone: mere directory existence (an interrupted
    # install, a `rm -rf <dir>/*` leftover, a user-created placeholder)
    # classified the box as "dev", silently trading an accurate "no
    # coordinator source found ... run coordinator:install" error for a
    # downstream git-ladder failure that doesn't mention install. Now
    # requires actual evidence of a coordinator tree, matching the same
    # `.git`-or-manifest evidence `coordinator_doe_root._cf_flat_layout_probe`
    # already requires for the identical path (that probe uses the
    # marketplace marker; this rung uses `.git`/manifest since the
    # motivating scenario is a raw git checkout with no marketplace marker).
    candidate = (
        _registry_example_doctrine_repo()
        or _registry_live_path()
        or _read_doe_root_pointer()
        or (flat if flat and not oss_present and _flat_evidences_coordinator_tree(flat) else "")
        or ""
    )
    candidate_resolved = bool(candidate) and os.path.isdir(candidate)

    dev_marker_present = candidate_resolved and os.path.isfile(
        os.path.join(candidate, ".coordinator-dev-repo")
    )

    if dev_marker_present:
        return "dev"

    if candidate_resolved and oss_present:
        raise ResolveCoordinatorCloneError(
            "resolve-coordinator-clone: ambiguous coordinator source — an unmarked "
            f'candidate clone was found at "{candidate}" (no .coordinator-dev-repo '
            f'marker) AND an OSS install was found at '
            f'"{os.path.join(claude_home, "plugins", "coordinator-claude")}".\n'
            "  Set COORDINATOR_SOURCE_MODE=dev or COORDINATOR_SOURCE_MODE=oss to disambiguate."
        )

    if oss_present:
        return "oss"

    if candidate_resolved:
        return "dev"

    raise ResolveCoordinatorCloneError(
        "resolve-coordinator-clone: no coordinator source found (no dev marker, no "
        "OSS install); set COORDINATOR_SOURCE_MODE or run coordinator:install."
    )


# ---------------------------------------------------------------------------
# --clone-root (alias --for-git-ops)
# ---------------------------------------------------------------------------


def resolve_clone_root() -> str:
    """The .git-backed clone directory. Mirrors `_rcc_resolve_git_ops`:

    OSS mode: ONLY the flat marketplace clone qualifies, and only if it has
    `.git` (a marketplace byte-copy has none) -> fail loud otherwise, no
    fall-through to the dev rungs.

    dev / passthrough mode, in order:
      1. COORDINATOR_CLONE env var (must have `.git/`, else fail loud)
      2. registry: repos.example_doctrine_repo (canonical) then
         plugin.mirrors.coordinator-claude.live_path (fallback) — gated on
         `.git/`; present-but-not-git-backed falls through, not a hard stop
      3. `.doe-root` pointer (durable-then-legacy) -> that root itself,
         gated on `.git/`
      4. Flat layout `<claude_home>/plugins/coordinator-claude`, gated on
         `.git/`
      5. Fail loud
    """
    mode = _resolve_source_mode("git-ops")

    claude_home = _claude_home_dir()
    flat = os.path.join(claude_home, "plugins", "coordinator-claude") if claude_home else None

    if mode == "oss":
        if flat and os.path.isdir(os.path.join(flat, ".git")):
            return flat
        raise ResolveCoordinatorCloneError(
            "resolve-coordinator-clone --for-git-ops: OSS mode selected but no "
            "git-backed OSS clone found (a marketplace byte-copy install has no .git).\n"
            "  Run: coordinator:install OR set COORDINATOR_CLONE to a git-backed clone path."
        )

    # dev / passthrough — unchanged ladder.
    clone_env = os.environ.get("COORDINATOR_CLONE")
    if clone_env:
        if os.path.isdir(os.path.join(clone_env, ".git")):
            return clone_env
        raise ResolveCoordinatorCloneError(
            f'resolve-coordinator-clone: COORDINATOR_CLONE is set to "{clone_env}" but '
            "it has no .git directory"
        )

    live = _registry_example_doctrine_repo() or _registry_live_path()
    if live and os.path.isdir(os.path.join(live, ".git")):
        return live

    doe_root = _read_doe_root_pointer()
    if doe_root and os.path.isdir(os.path.join(doe_root, ".git")):
        return doe_root

    if flat and os.path.isdir(os.path.join(flat, ".git")):
        return flat

    raise ResolveCoordinatorCloneError(
        "resolve-coordinator-clone --for-git-ops: no git-backed coordinator clone found.\n"
        "  Tried: COORDINATOR_CLONE env, registry repos.example_doctrine_repo (canonical),\n"
        "         registry plugin.mirrors.coordinator-claude.live_path (fallback),\n"
        "         ~/.claude/.doe-root pointer, flat ~/.claude/plugins/coordinator-claude\n"
        "  (no .git in any tried location)\n"
        "  Run: coordinator:install OR set COORDINATOR_CLONE to the clone path."
    )


# ---------------------------------------------------------------------------
# --content-root (alias --for-content)
# ---------------------------------------------------------------------------


def resolve_content_root() -> str:
    """Highest-precedence readable payload directory. Mirrors
    `_rcc_resolve_content`:

    OSS mode: newest versioned cache, else the flat marketplace manifest ->
    fail loud otherwise, no fall-through to the dev rungs.

    dev / passthrough mode, in order:
      1. CLAUDE_PLUGIN_ROOT (must exist, else fail loud)
      2. COORDINATOR_ROOT (must exist, else fail loud)
      3. registry live_path (dev-loop clone wins over cache)
      4. newest versioned cache
      5. `.doe-root` pointer -> `<root>/coordinator`, gated on that dir existing
      6. flat layout, gated on `.claude-plugin/plugin.json` manifest marker
      7. Fail loud
    """
    mode = _resolve_source_mode("content")

    claude_home = _claude_home_dir()
    flat = os.path.join(claude_home, "plugins", "coordinator-claude") if claude_home else None

    if mode == "oss":
        newest = _newest_cache_dir()
        if newest:
            return newest
        if flat and os.path.isfile(os.path.join(flat, ".claude-plugin", "plugin.json")):
            return flat
        raise ResolveCoordinatorCloneError(
            "resolve-coordinator-clone --for-content: OSS mode selected but no readable "
            "OSS content root found (tried versioned cache, flat marketplace manifest).\n"
            "  Run: coordinator:install OR set COORDINATOR_ROOT to the coordinator directory."
        )

    # dev / passthrough — unchanged ladder.
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root_env:
        if os.path.isdir(plugin_root_env):
            return plugin_root_env
        raise ResolveCoordinatorCloneError(
            f'resolve-coordinator-clone: CLAUDE_PLUGIN_ROOT is set to "{plugin_root_env}" '
            "but it does not exist"
        )

    coordinator_root_env = os.environ.get("COORDINATOR_ROOT")
    if coordinator_root_env:
        if os.path.isdir(coordinator_root_env):
            return coordinator_root_env
        raise ResolveCoordinatorCloneError(
            f'resolve-coordinator-clone: COORDINATOR_ROOT is set to "{coordinator_root_env}" '
            "but it does not exist"
        )

    live = _registry_live_path()
    if live and os.path.isdir(live):
        return live

    newest = _newest_cache_dir()
    if newest:
        return newest

    doe_root = _read_doe_root_pointer()
    if doe_root and os.path.isdir(os.path.join(doe_root, "coordinator")):
        return os.path.join(doe_root, "coordinator")

    if flat and os.path.isfile(os.path.join(flat, ".claude-plugin", "plugin.json")):
        return flat

    raise ResolveCoordinatorCloneError(
        "resolve-coordinator-clone --for-content: no readable coordinator content root found.\n"
        "  Tried: CLAUDE_PLUGIN_ROOT, COORDINATOR_ROOT, registry live_path,\n"
        "         versioned cache glob, ~/.claude/.doe-root pointer,\n"
        "         flat ~/.claude/plugins/coordinator-claude\n"
        "  Run: coordinator:install OR set COORDINATOR_ROOT to the coordinator directory."
    )


# ---------------------------------------------------------------------------
# CLI-shaped wrapper (parity testing / non-Python callers only — Python
# callers should import resolve_clone_root()/resolve_content_root() directly)
# ---------------------------------------------------------------------------

_FLAG_TO_VERB = {
    "--clone-root": "clone-root",
    "--for-git-ops": "clone-root",
    "--content-root": "content-root",
    "--for-content": "content-root",
}


def main(argv: Optional[List[str]] = None) -> int:
    """Prints the resolved path to stdout (no trailing newline) and returns 0;
    or writes remediation to stderr and returns 1 (resolution failure) / 2
    (usage error — wrong arg count or unknown flag), mirroring the bash
    oracle's standalone-CLI exit codes."""
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) != 1 or args[0] not in _FLAG_TO_VERB:
        sys.stderr.write(
            "Usage: resolve-coordinator-clone.sh --clone-root|--content-root "
            "(aliases: --for-git-ops|--for-content)\n"
        )
        return 2

    try:
        if _FLAG_TO_VERB[args[0]] == "clone-root":
            path = resolve_clone_root()
        else:
            path = resolve_content_root()
    except ResolveCoordinatorCloneError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1

    sys.stdout.write(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
