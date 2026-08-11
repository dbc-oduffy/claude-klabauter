"""
coordinator_core._settings_home — bootstrap-safe settings-home resolver.

Purpose: resolve the coordinator settings home, mirroring the canonical
`coordinator-settings-home` precedence: COORDINATOR_SETTINGS_HOME, falling
back to CLAUDE_HOME, falling back to the platform home directory
(Path.home() — USERPROFILE on Windows, HOME or the passwd entry on POSIX),
with `.coordinator-claude-settings` appended to the latter two. A pure
env/home read with zero external calls, so it can be safely used on the
bootstrap path before PATH/CLI availability is guaranteed.

`Path.home()` — not a `$HOME` read — is load-bearing: `HOME` is a POSIX
convention that native Windows shells do not set, and a literal
`${CLAUDE_HOME:-$HOME}` transliteration resolves to the empty string there,
yielding a cwd-relative settings home. Prose that spelled this precedence in
bash form is what minted exactly that bug in two sibling resolvers; keep the
description in terms of what the code does.

Public surface (pinned contract — do not change without updating consumers):

    def home_dir() -> Path: ...
    def settings_home() -> Path: ...
    def machine_local_dir() -> Path: ...
    def normalize_native_path(raw) -> Path: ...
    def legacy_machine_local_dir() -> Path: ...
    def check_machine_local_divergence() -> None: ...  # raises SettingsHomeDivergenceError
    class SettingsHomeDivergenceError(RuntimeError): ...
    def claude_config_dir() -> Path: ...
    def check_claude_config_divergence() -> None: ...  # raises ClaudeConfigDivergenceError
    class ClaudeConfigDivergenceError(RuntimeError): ...

`claude_config_dir()` (added 2026-08-08, CLAUDE_CONFIG_DIR resolution seam) closes a naming
mismatch, not a new resolver: `CLAUDE_HOME` is a coordinator invention naming the *parent* of
`.claude`; `CLAUDE_CONFIG_DIR` is the harness's own env var (in its env-passthrough allowlist,
54 references in the installed bundle) naming the config directory *itself*. Every existing
`<home>/.claude` call site — `home_dir() / ".claude"` and its hand-rolled equivalents — computes
the same directory `CLAUDE_CONFIG_DIR` names directly, so a caller that honours one convention
and ignores the other silently diverges the day both are set to different things. Route new
`<home>/.claude`-shaped call sites through `claude_config_dir()` instead of hand-rolling
`home_dir() / ".claude"`; call `check_claude_config_divergence()` from any caller in a position
to fail loud (mirrors `check_machine_local_divergence()`'s placement contract).

`home_dir()` (added 2026-07-29, home-resolution-lint bare_home_or_chain sweep) is the
CLAUDE_HOME-analog resolver (CLAUDE_HOME override, else the platform home via
`Path.home()`, which honours USERPROFILE on Windows) — this is the same computation
`settings_home()` already performs internally as its own private `_home_dir()`
helper, now exposed so coordinator_core callers that need the `~/.claude`-analog
home (not the settings home) can delegate here instead of hand-rolling their own
`CLAUDE_HOME or HOME` chain with no Windows rung. Not a new resolver — the algorithm
is unchanged; only its visibility widened.

Spec backlink: docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md § C1
Spec backlink: docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C1

Port of: settings-home.sh (example-doctrine-repo b644d5a9, 2026-07-22) — `_coordinator_settings_home`,
`_settings_home_realpath`, `_check_machine_local_divergence`. This module is the
Python-native mirror for claude-klabauter-resident callers.

Negative-spec:
  - NEVER shell out to `coordinator-settings-home` — this is the bootstrap-safe
    direct resolver; the whole point of this module is to stay subprocess-free.
  - NEVER call the `machine-local` CLI — general registry-key reads that want
    the CLI's rung-1 `MACHINE_LOCAL_REGISTRY_DIR` override are out of scope for
    this bootstrap-safe subset; that belongs to a separate `_machine_local.py`
    resolver, not this module.
  - `check_machine_local_divergence()` uses `Path.resolve()` (stdlib, symlink-
    and-`..`-safe) as the realpath equivalent — NOT a `python3 -c
    os.path.realpath` subprocess like the shell original's `_settings_home_realpath`,
    since this module is already pure-Python; there is no python3-absent fallback
    branch here (the shell original degrades to a literal-path lexical compare
    when python3 is missing — moot in a Python-resident caller, which by
    definition has Python).
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _require_rooted(var: str, raw: str) -> Path:
    """Return `Path(raw)`, or raise ValueError if it would anchor at the cwd.

    A rooted path ('/srv/x', 'C:\\Users\\x', '\\\\server\\share') is accepted; a
    relative one ('foo', 'C:foo') is not. On Windows a drive letter alone is NOT
    absolute — 'C:foo' means "foo relative to the cwd on drive C:" — so the test
    is `is_absolute() or root`, which accepts a POSIX-style rooted path under a
    Windows interpreter while still rejecting the drive-relative form.

    Ported from coordinator/lib/claude-home/_claude_home.py::home_dir/settings_home,
    the one resolver in the tri-plane that already validated both overrides.
    Empty is deliberately NOT an error: empty-means-unset is this module's pinned
    contract (tests/test_settings_home.py), and only the cwd-relative foot-gun is
    being closed here.
    """
    p = Path(raw)
    if not (p.is_absolute() or p.root):
        raise ValueError(
            f"{var} must be an absolute path; got {raw!r}. "
            "(On Windows, a drive letter alone is insufficient — use 'C:\\...' form.)"
        )
    return p


def _home_dir() -> Path:
    """Resolve the $HOME analog: CLAUDE_HOME if set, else the platform home."""
    claude_home = os.environ.get("CLAUDE_HOME")
    if claude_home:
        return _require_rooted("CLAUDE_HOME", claude_home)
    return Path.home()


def home_dir() -> Path:
    """Public CLAUDE_HOME-analog resolver — see module docstring "Public surface".

    Delegate here instead of hand-rolling ``os.environ.get("CLAUDE_HOME") or
    os.environ.get("HOME") or ...`` at a new call site: that shape is exactly the
    ``bare_home_or_chain`` lint's target (no Windows rung, degrades to a
    cwd-relative path when only USERPROFILE is set). This function's fallback is
    ``Path.home()``, which already honours USERPROFILE.
    """
    return _home_dir()


def settings_home() -> Path:
    """Resolve the coordinator settings-home root via pure env/home precedence."""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return _require_rooted("COORDINATOR_SETTINGS_HOME", override)
    return _home_dir() / ".coordinator-claude-settings"


def machine_local_dir() -> Path:
    """Resolve the `machine-local/` directory under the settings-home root."""
    return settings_home() / "machine-local"


def normalize_native_path(raw):
    """Convert an MSYS/Cygwin mount-form path ('/x/...' or '/cygdrive/x/...') to
    native Windows drive form ('X:/...') so native-Windows node / py.exe /
    Path.exists consumers resolve it. No-op on POSIX (os.name != 'nt') and on
    already-native paths ('X:/...', 'X:\\...'). Returns a Path.

    Rationale: a leading '/x/...' handed to a native-Windows process resolves as
    drive-relative 'X:\\x\\...' (doubled drive) — the .doe-root / repos.example_doctrine_repo
    mis-resolution bug. Mirrors the `cygpath -m` normalization used on the write side.
    """
    s = str(raw)
    if os.name != "nt":
        return Path(raw)
    m = re.match(r"^/(?:cygdrive/)?([A-Za-z])(/.*)?$", s)
    if m:
        return Path(m.group(1).upper() + ":" + (m.group(2) or "/"))
    return Path(raw)


def legacy_machine_local_dir() -> Path:
    """Resolve the pre-migration `~/.claude/machine-local` legacy location.

    Distinct from `machine_local_dir()` (the new settings-home-rooted
    location) — used only by `check_machine_local_divergence()` to compare
    the two candidate homes.
    """
    return _home_dir() / ".claude" / "machine-local"


def _is_absent_or_empty_husk(path: Path) -> bool:
    """True when `path` carries no machine-local state — absent, or a directory
    left behind empty by a completed migration.

    An empty directory is not a second content home: nothing can be read from
    it, so treating it as one turns a finished migration into a fail-loud.
    A dangling symlink and an unreadable directory both count as no-state too —
    neither yields content to diverge over.
    """
    try:
        if not path.exists():
            return True
        return not any(path.iterdir())
    except OSError:
        return True


class SettingsHomeDivergenceError(RuntimeError):
    """Raised when both the legacy and new machine-local homes exist and
    their resolved (symlink-following) paths diverge — mirrors the shell
    original's fail-loud `_check_machine_local_divergence` exit-1 path.
    """


def check_machine_local_divergence() -> None:
    """Fail loud if BOTH machine-local homes hold content AND their resolved paths differ.

    Safe (no-op, returns None) in all of:
      - Legacy ~/.claude/machine-local absent or an empty post-migration husk, OR
      - New <settings-home>/machine-local absent or an empty husk, OR
      - Both exist AND resolved paths are equal (compat symlink — NOT a
        second home).

    Raises `SettingsHomeDivergenceError` when both exist AND resolved paths
    diverge. Never silently picks one; the caller must run the migration
    (`coordinator/lib/migrate-substrate-to-settings-home.sh`) to resolve.

    CRITICAL: resolved-path comparison (not raw-path comparison) prevents a
    false positive on the compat layer — a symlink at
    ~/.claude/machine-local -> <settings-home>/machine-local resolves to the
    same path and MUST NOT raise.
    """
    legacy = legacy_machine_local_dir()
    new = machine_local_dir()

    if _is_absent_or_empty_husk(legacy) or _is_absent_or_empty_husk(new):
        return

    resolved_legacy = legacy.resolve()
    resolved_new = new.resolve()

    if resolved_legacy != resolved_new:
        raise SettingsHomeDivergenceError(
            "coordinator-settings-home: DIVERGENT MACHINE-LOCAL HOMES — cannot safely resolve.\n\n"
            "Both ~/.claude/machine-local and <settings-home>/machine-local exist and point\n"
            "at different content homes. Reading from either risks using stale or incomplete\n"
            "registry data.\n\n"
            "Remediation — run the one-time migration to consolidate both homes:\n"
            "    coordinator/lib/migrate-substrate-to-settings-home.sh\n\n"
            "Or, if you know which home is authoritative, set COORDINATOR_SETTINGS_HOME to\n"
            "point at the desired settings home and then remove or symlink the other:\n"
            "    export COORDINATOR_SETTINGS_HOME=/path/to/settings-home\n\n"
            f"  Legacy resolved : {resolved_legacy}\n"
            f"  New    resolved : {resolved_new}"
        )


def claude_config_dir() -> Path:
    """Resolve the harness's own config-directory root.

    Precedence: `CLAUDE_CONFIG_DIR` (the harness's own env var, validated
    rooted via `_require_rooted` like every other override in this module),
    else `home_dir() / ".claude"` — the same computation every pre-existing
    `<home>/.claude` call site performs by hand. See module docstring's
    "Public surface" block for the naming distinction from `CLAUDE_HOME`.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return _require_rooted("CLAUDE_CONFIG_DIR", override)
    return home_dir() / ".claude"


class ClaudeConfigDivergenceError(RuntimeError):
    """Raised when `CLAUDE_CONFIG_DIR` is set, resolves to a directory other
    than `home_dir() / ".claude"`, and BOTH hold content — the harness-vs-
    coordinator split-brain `claude_config_dir()` exists to close. Mirrors
    `SettingsHomeDivergenceError`'s fail-loud contract.
    """


def check_claude_config_divergence() -> None:
    """Fail loud if `CLAUDE_CONFIG_DIR` is set, resolves elsewhere than
    `home_dir() / ".claude"`, AND both hold content.

    Safe (no-op, returns None) in all of:
      - `CLAUDE_CONFIG_DIR` unset — `claude_config_dir()` falls back to the
        same default this function compares against, so there is nothing to
        diverge from.
      - Either side is absent or an empty post-migration husk.
      - Resolved paths are equal (e.g. a compat symlink, or `CLAUDE_CONFIG_DIR`
        pointed at `~/.claude` under a different spelling) — NOT a second home.

    Raises `ClaudeConfigDivergenceError` on genuine divergence. Never silently
    picks one: a hook or subprocess honouring the harness's `CLAUDE_CONFIG_DIR`
    while coordinator code reads `~/.claude` (or vice versa) is exactly the
    silent-mis-resolution failure mode this seam exists to close.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if not override:
        return

    configured = _require_rooted("CLAUDE_CONFIG_DIR", override)
    default = home_dir() / ".claude"

    if _is_absent_or_empty_husk(configured) or _is_absent_or_empty_husk(default):
        return

    resolved_configured = configured.resolve()
    resolved_default = default.resolve()

    if resolved_configured != resolved_default:
        raise ClaudeConfigDivergenceError(
            "coordinator-settings-home: DIVERGENT CLAUDE CONFIG DIRS — cannot safely resolve.\n\n"
            "CLAUDE_CONFIG_DIR is set and resolves to a directory other than the\n"
            "CLAUDE_HOME-derived ~/.claude, and both hold content. Reading from either\n"
            "risks missing markers or caches the harness (honouring CLAUDE_CONFIG_DIR) or\n"
            "coordinator code (honouring CLAUDE_HOME) actually wrote.\n\n"
            "Remediation — pick one and align the other:\n"
            "  - If CLAUDE_CONFIG_DIR is authoritative, point CLAUDE_HOME at its parent\n"
            "    directory, or migrate the ~/.claude content into it, or\n"
            "  - Unset CLAUDE_CONFIG_DIR to use the CLAUDE_HOME-derived location.\n\n"
            f"  CLAUDE_CONFIG_DIR resolved : {resolved_configured}\n"
            f"  ~/.claude       resolved : {resolved_default}"
        )
