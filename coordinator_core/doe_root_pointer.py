"""
coordinator_core.doe_root_pointer

Port of: read-doe-root-pointer.sh (example-doctrine-repo 6fb5fb37, 2026-07-22)

Purpose: resolves the example-doctrine-repo repo root, registry-first per DR-071 (2026-07-22 — the
settings-home machine-local registry key `repos.example_doctrine_repo` is the canonical,
authoritative coordinator-root anchor; `.doe-root` is a demoted, non-authoritative
mirror), with the pointer-file rungs retained as durable-then-legacy fallbacks:
    1. `repos.example_doctrine_repo`                                (registry — canonical, DR-071)
    2. `<settings-home>/machine-local/.doe-root`         (durable file mirror)
    3. `${CLAUDE_HOME:-$HOME}/.claude/.doe-root`         (legacy fallback)
written by `coordinator_core.ops.gen_doe_root_pointer`. Mirror-image of
`coordinator_core.claude_klabauter_root` (which resolves CLAUDE_KLABAUTER_ROOT from inside the claude-klabauter
engine) — this module resolves the example-doctrine-repo root from the registry/pointer file, the
cold-read primitive consumed by the coordinator-clone resolver's rung 3.

**Why registry-first, and why direct tomllib (not the `machine-local` CLI):**
the registry lives at `<settings-home>/machine-local/registry.local.toml`, outside
`~/.claude`, so it survives a Claude Code reset that wipes `~/.claude` (and, with
it, the `.doe-root` file mirror AND, during the settings-home migration window,
the `machine-local` CLI's `~/.claude/bin/` mirror of its reader/exec bits — the
canonical home is `<settings-home>/bin/`, outside `~/.claude`, but the demoted
mirror is what a reset actually wipes). Reading the registry TOML directly via
`coordinator_core.machine_resolver.registry_get` — rather than shelling out to
`machine-local get repos.example_doctrine_repo` — is what makes this rung actually
reset-safe: "`machine-local get` works" is not proof of reset-survival, since the
CLI itself can be the thing a reset just broke. See DR-071
(`docs/decisions/DR-071-durable-coordinator-root-anchor-settings-home-registry-doe-root-demoted-to-cache.md`
in example-doctrine-repo) and the consumer-contract memo
`cross-repo/inbox/2026-07-22-claude-central-em-durable-root-anchor-contract.md`.

Spec backlink: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md
DR-148: no realpath, no GNU-isms — this is a pure-Python read, no shell-portability
concern applies, but the resolution semantics (whitespace handling, absent-file
behavior) mirror the bash oracle exactly for the two file rungs.

Public API:
    def read_doe_root_pointer() -> str   — registry-first successor to the shell
        function coordinator_read_doe_root_pointer(). Returns the registry
        `repos.example_doctrine_repo` value if resolvable, else the pointer file's content
        (single line, stripped) or "" if the registry key is unresolved, the
        home directory cannot be resolved, both pointer files are absent, or
        they are unreadable. Does NOT validate whether the returned path exists
        on disk — callers apply that gate themselves (mirrors the bash oracle's
        explicit-gate design).

Negative-spec:
    - Does NOT write or create the pointer file, and does NOT write to the
      registry — read-only on all three rungs, mirrors the bash oracle (which
      is a pure `cat`, zero tool dependency, zero side effects).
    - Does NOT validate the resolved path exists or contains a coordinator/
      subdir — that gate is the caller's responsibility (the coordinator-clone
      resolver applies its own -d gate after calling this), exactly as the
      bash oracle's docstring states.
    - Does NOT raise on a missing/unreadable pointer file or unresolved
      registry key — returns "" like the bash oracle's
      `cat ... 2>/dev/null || true`, never a hard error. Contrast with
      coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root(), which DOES
      raise on final-rung failure — the two mirror-image resolvers have
      deliberately different failure contracts, carried over unchanged from
      their respective bash oracles.
    - Does NOT shell out to the `machine-local` CLI for the registry rung —
      see "Why registry-first" above; a direct tomllib read via
      `machine_resolver.registry_get` is the load-bearing reset-safety choice,
      not a style preference.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from coordinator_core._settings_home import settings_home
from coordinator_core.machine_resolver import registry_get

# coordinator/lib — sibling of this file's repo-root parent, hosting the
# shared coordinator_read_doe_root_pointer() substrate (rungs 2-3: durable +
# legacy `.doe-root` pointer file reads). Two parents up from this file
# (coordinator_core/doe_root_pointer.py -> coordinator_core -> repo root),
# then down into coordinator/lib.
_COORDINATOR_LIB_DIR = str(Path(__file__).resolve().parent.parent / "coordinator" / "lib")

# Published payload flattens: the mirror ships the helper at "<repo root>/lib"
# with no "coordinator/" segment. Probed as a fallback below — private tree
# wins first.
_COORDINATOR_LIB_DIR_FLAT = str(Path(__file__).resolve().parent.parent / "lib")


def _file_rungs_via_shared_helper() -> str:
    """Codename-free rungs 2-3: delegate to
    coordinator/lib/read_doe_root_pointer.py::coordinator_read_doe_root_pointer()
    rather than reimplementing the `.doe-root` pointer file read. That helper
    already tries `${settings-home}/machine-local/.doe-root` then
    `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` in that order, returns "" on
    failure, and never raises. Does NOT validate the resolved path exists —
    callers (this module's own contract) apply that gate.
    """
    lib_dir = _COORDINATOR_LIB_DIR
    if not os.path.isfile(os.path.join(lib_dir, "read_doe_root_pointer.py")):
        lib_dir = _COORDINATOR_LIB_DIR_FLAT
    added = lib_dir not in sys.path
    if added:
        sys.path.insert(0, lib_dir)
    try:
        from read_doe_root_pointer import coordinator_read_doe_root_pointer

        return coordinator_read_doe_root_pointer()
    except Exception:
        # Swallows: helper missing at both probed dirs, import error inside
        # the helper itself, or any runtime failure in the read — all
        # collapse to "no pointer configured" by contract (never-raise).
        return ""
    finally:
        if added:
            try:
                sys.path.remove(lib_dir)
            except ValueError:
                pass


def read_doe_root_pointer_file(home: str | None = None) -> str:
    """Resolve the example-doctrine-repo root from the pointer FILES only — durable, then legacy.

        1. <settings-home>/machine-local/.doe-root   (durable — the write target)
        2. <home>/.claude/.doe-root                  (legacy fallback)

    Returns "" when neither is present/readable. `home` defaults to
    ``${CLAUDE_HOME:-$HOME}``; callers that accept an injectable home (tests,
    sandbox probes) pass it explicitly.

    This is the file-rungs subset of :func:`read_doe_root_pointer`, WITHOUT the
    registry rung. It exists for the resolvers that deliberately consult the
    registry at a different priority than DR-071's registry-first order (several
    try the pointer file, then a *different* registry key such as
    ``plugin.mirrors.coordinator-claude.live_path``) — those must not have the
    ``repos.example_doctrine_repo`` rung silently spliced in ahead of their own. Prefer
    :func:`read_doe_root_pointer` unless you are one of those.

    Extracted 2026-07-28: the durable rung became load-bearing when
    ``gen_doe_root_pointer`` stopped writing the legacy target, and six call
    sites were each open-coding a single legacy ``open()``. One implementation
    so the next relocation is one edit, not a six-site sweep that misses two.
    """
    if home is None:
        home = (
            os.environ.get("CLAUDE_HOME")
            or os.environ.get("HOME")
            or os.environ.get("USERPROFILE")
            or os.path.expanduser("~")
        )
    for candidate in (
        settings_home() / "machine-local" / ".doe-root",
        Path(home) / ".claude" / ".doe-root",
    ):
        try:
            content = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return content
    return ""


def read_doe_root_pointer() -> str:
    """Resolve the example-doctrine-repo repo root — registry-first (DR-071), durable-file, legacy-file.

    Read order (DR-071, 2026-07-22 — supersedes the prior durable-file-first
    order of read-doe-root-pointer.sh, DR-072, 2026-07-21):
        1. registry `repos.example_doctrine_repo`                     (canonical anchor)
        2. <settings-home>/machine-local/.doe-root        (durable file mirror)
        3. ${CLAUDE_HOME:-$HOME}/.claude/.doe-root         (legacy fallback)

    Returns the example-doctrine-repo repo root path (single line, stripped) or "" if the
    registry key is unresolved AND neither pointer file is present/readable,
    or no home directory can be resolved.
    """
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    settings_home_override = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.environ.get(
        "MACHINE_LOCAL_REGISTRY_DIR"
    )

    # Guard the registry rung on a resolvable home/settings-home context.
    # Without this guard, ``registry_get`` -> ``_settings_home.settings_home()``
    # falls back to ``Path.home()`` (the real OS home directory) when
    # CLAUDE_HOME/HOME/USERPROFILE are ALL unset — the "no home resolvable"
    # contract case this function documents as returning "" would otherwise
    # silently read this machine's actual settings-home registry instead.
    if home or settings_home_override:
        registry_value = registry_get("repos.example_doctrine_repo")
        if registry_value:
            return registry_value

    # Same home/settings-home guard the registry rung above carries, and for the
    # same reason: ``settings_home()`` falls back to ``Path.home()`` (the real OS
    # home) when CLAUDE_HOME/HOME/USERPROFILE are ALL unset, so an unguarded read
    # here would return this machine's actual durable/legacy pointer in the
    # documented "no home resolvable" case that contracts to "".
    if not (home or settings_home_override):
        return ""

    if home:
        # Rungs 2-3: durable-then-legacy `.doe-root` pointer file reads, via
        # the shared coordinator/lib helper rather than a local
        # reimplementation (see _file_rungs_via_shared_helper docstring).
        # Codename-free — its vocabulary keys are all compound-with-claude, so
        # the OSS scrub cannot touch them. Provably equivalent to the prior
        # inline read for this state: the shared helper recomputes its own
        # home via the IDENTICAL ``CLAUDE_HOME or HOME or USERPROFILE or ""``
        # expression this function uses (both include the USERPROFILE rung),
        # and its settings-home resolution
        # (coordinator/lib/settings_home.py::settings_home()) mirrors
        # coordinator_core._settings_home.settings_home()'s precedence
        # (COORDINATOR_SETTINGS_HOME override, else CLAUDE_HOME/Path.home())
        # line for line — see both modules' docstrings.
        return _file_rungs_via_shared_helper()

    # home unset but settings_home_override set: the shared helper gates its
    # own entry on a resolvable ${CLAUDE_HOME:-$HOME:-$USERPROFILE} home and
    # returns "" immediately without it — so it can never reach the durable
    # rung in this state, unlike the prior inline code, which tried the
    # durable rung via settings_home() (override-driven, home-independent)
    # regardless of home. The legacy rung stays unreachable here either way
    # (it requires `home`), matching the prior code's own `if not home: return
    # ""` gate before the legacy read. So only the durable rung applies, read
    # directly rather than through the (home-gated) shared helper.
    try:
        durable = settings_home() / "machine-local" / ".doe-root"
        content = durable.read_text(encoding="utf-8").rstrip("\n")
        if content:
            return content
    except OSError:
        pass
    return ""
