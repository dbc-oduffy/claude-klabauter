"""
coordinator_core.doe_root_pointer

Port of: read-doe-root-pointer.sh (coordinator-claude 6fb5fb37, 2026-07-22)

Purpose: resolves the coordinator-claude repo root, registry-first per DR-071 (2026-07-22 — the
settings-home machine-local registry key `repos.example_doctrine_repo` is the canonical,
authoritative coordinator-root anchor; `.doe-root` is a demoted, non-authoritative
mirror), with the pointer-file rungs retained as durable-then-legacy fallbacks:
    1. `repos.example_doctrine_repo`                                (registry — canonical, DR-071)
    2. `<settings-home>/machine-local/.doe-root`         (durable file mirror)
    3. `${CLAUDE_HOME:-$HOME}/.claude/.doe-root`         (legacy fallback)
written by `coordinator_core.ops.gen_doe_root_pointer`. Mirror-image of
`coordinator_core.claude_klabauter_root` (which resolves CLAUDE_KLABAUTER_ROOT from inside the claude-klabauter
engine) — this module resolves the coordinator-claude root from the registry/pointer file, the
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
in coordinator-claude) and the consumer-contract memo
`cross-repo/inbox/2026-07-22-claude-central-em-durable-root-anchor-contract.md`.

Spec backlink: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md [DEAD-CITATION: plan file never committed to this repo]
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
from pathlib import Path

from coordinator_core._settings_home import settings_home
from coordinator_core.machine_resolver import registry_get


def read_doe_root_pointer_file(home: str | None = None) -> str:
    """Resolve the coordinator-claude root from the pointer FILES only — durable, then legacy.

        1. <settings-home>/machine-local/.doe-root   (durable — the write target)
        2. <home>/.claude/.doe-root                  (legacy fallback)

    Returns "" when neither is present/readable. `home` defaults to
    ``${CLAUDE_HOME:-$HOME:-$USERPROFILE}``; callers that accept an injectable
    home (tests, sandbox probes) pass it explicitly.

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

    Review: B3 (MAJOR, 2026-08-08) -- when called with no explicit `home` and
    none of CLAUDE_HOME/HOME/USERPROFILE nor a settings-home override are
    resolvable, this previously still defaulted `home` via
    `os.path.expanduser("~")` and read the REAL machine's legacy pointer --
    the same "artifact of where it ran" failure `read_doe_root_pointer()`'s
    own guard exists to prevent (that guard was never mirrored here even
    though this function became a rung-1.5(a) caller in C1B). Now returns ""
    in that state, matching the sibling guard exactly.
    Review: B4 (MAJOR, 2026-08-08) -- this is now the ONLY place either
    public entrypoint reads the pointer files; `read_doe_root_pointer()`
    below delegates to it for the home-present case rather than carrying an
    independent inline copy that disagreed on whitespace stripping
    (`.strip()` here vs. `.rstrip("\\n")` there, for a trailing-space/
    leading-tab pointer file). Whitespace handling is unified on
    `.rstrip("\\n")` -- the bash-`$(cat ...)`-mirroring choice the module
    docstring's negative-spec already commits to. The former
    `_file_rungs_via_shared_helper()` delegate into `coordinator/lib` (with
    its private-vs-flat-payload directory probe) is retired along with it:
    this local implementation is provably equivalent for every state it
    covered (see prior module history) and removes two further defects that
    delegate carried -- B8 (a `sys.path`/`sys.modules` shadowing hazard) and
    B9 (a swallowed exception indistinguishable from a clean miss) both
    apply only to code that no longer exists.
    """
    if home is None:
        home_env = (
            os.environ.get("CLAUDE_HOME")
            or os.environ.get("HOME")
            or os.environ.get("USERPROFILE")
        )
        settings_home_override = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.environ.get(
            "MACHINE_LOCAL_REGISTRY_DIR"
        )
        if not (home_env or settings_home_override):
            return ""
        home = home_env or os.path.expanduser("~")
    for candidate in (
        settings_home() / "machine-local" / ".doe-root",
        Path(home) / ".claude" / ".doe-root",
    ):
        try:
            content = candidate.read_text(encoding="utf-8").rstrip("\n")
        except OSError:
            continue
        if content:
            return content
    return ""


def read_doe_root_pointer() -> str:
    """Resolve the coordinator-claude repo root — registry-first (DR-071), durable-file, legacy-file.

    Read order (DR-071, 2026-07-22 — supersedes the prior durable-file-first
    order of read-doe-root-pointer.sh, DR-072, 2026-07-21):
        1. registry `repos.example_doctrine_repo`                     (canonical anchor)
        2. <settings-home>/machine-local/.doe-root        (durable file mirror)
        3. ${CLAUDE_HOME:-$HOME}/.claude/.doe-root         (legacy fallback)

    Returns the coordinator-claude repo root path (single line, stripped) or "" if the
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
        # Rungs 2-3: durable-then-legacy `.doe-root` pointer file reads,
        # delegated to read_doe_root_pointer_file() -- the single remaining
        # implementation of this read (B4 review fix, 2026-08-08; see that
        # function's docstring). Passing `home` explicitly reuses the exact
        # value this function already resolved above, rather than letting
        # the callee re-derive it.
        return read_doe_root_pointer_file(home)

    # home unset but settings_home_override set: read_doe_root_pointer_file's
    # own no-explicit-home guard (B3) treats this state as reachable
    # (settings_home_override alone satisfies it) and would fall back to
    # os.path.expanduser("~") for its `home` -- reading the REAL machine's
    # legacy pointer, exactly the artifact-of-where-it-ran failure B3 exists
    # to prevent. So this state is deliberately NOT delegated: only the
    # durable rung applies here (settings_home() is override-driven,
    # home-independent), read directly, matching the prior code's own
    # ``if not home: return ""`` gate before the legacy read.
    try:
        durable = settings_home() / "machine-local" / ".doe-root"
        content = durable.read_text(encoding="utf-8").rstrip("\n")
        if content:
            return content
    except OSError:
        pass
    return ""
