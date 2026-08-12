"""
coordinator_core.ops.verify_ue_overrides — plain module, no registered op.

Purpose: walks the machine-local-registered UE-context directories
(repos.example_game_workbench_repo, repos.example_retrieval_repo, $HOME/.claude, optional
repos.example-sim-repo) and asserts each carries the expected UE plugin override keys in its
.claude/settings.json enabledPlugins object. Exits 0 on success, 1 with diagnostic
output on failure. This is a MANUAL diagnostic — per docs/wiki/per-project-plugin-
gating.md § verify-ue-overrides.sh, it is never auto-invoked by any ceremony (its peer
dirs are specific to the source author's local machine layout); run manually when UE
override drift is suspected.

Requires the `machine-local` CLI resolvable (PATH, $HOME/.claude/bin/machine-local,
or a machine-local binary co-located next to the trampoline; this module shells out
to whatever `machine-local` name/path the caller supplies).

machine-local keys consumed (must be set in registry.local.toml):
    repos.example_game_workbench_repo  — root of the example-game-workbench-repo repo
    repos.example_retrieval_repo             — root of the example-retrieval-repo repo
    repos.example-sim-repo                — root of the example-sim-repo UE project (optional; skipped
                                     if unset on this machine)

If a required key is not set, this fails loud with a remediation hint — a missing
registry value is a configuration gap that needs fixing, not a silent skip.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - `$HOME/.claude` has no `.claude` sub-directory of its own; its settings.json path
      is `$HOME/.claude/settings.json` directly, unlike the other UE-context dirs which
      use `<dir>/.claude/settings.json`. Preserved verbatim (the bash oracle's special-
      cased branch for this one entry).
    - A missing resolved directory is a hard FAIL (not a silent skip) — the old bash
      shape silently continued past a stale/wrong registry path; this port keeps the
      fail-loud replacement behavior, not the original silent-continue.
    - example-sim-repo is optional: `repos.example-sim-repo` unset on this machine means it is simply
      omitted from the walked dir list, not a failure.

Spec backlink: docs/plans/2026-05-20-coordinator-doctor-wiki.md § Chunk 10 (MISSED-2:
    hardcoded single-machine paths + vacuous-pass on missing dirs)
Port of: verify-ue-overrides.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.win_portability import is_executable, no_console_creationflags

# Plugins required to be enabled in every UE-context settings.json.
_EXPECTED_KEYS = (
    "example-game-repo-control@example-game-workbench-repo",
    "example-game-repo@example-game-workbench-repo",
)

# Either-vendor set: at least one entry from this set must be enabled. Avoids the
# dual-vendor conflict where two plugins claim the same agent surface (game-dev is
# vendored by both coordinator-claude and example-game-workbench-repo; only one should be
# active per machine to keep agent routing unambiguous).
_EITHER_VENDOR_GAME_DEV = (
    "game-dev@example-game-workbench-repo",
    "game-dev@coordinator-claude",
)


def _resolve_ml_bin(script_dir: str) -> Optional[str]:
    """Resolve the machine-local CLI: PATH, settings-home, legacy
    `$HOME/.claude/bin`, then a binary co-located next to the calling script.

    The settings-home rung is the canonical install (DR-072) and was missing
    from the bash oracle's three-rung ladder this ports. That mattered once
    `~/.claude/bin` was retired (2026-07-28): settings-home/bin is not on PATH
    by default, so the ladder reached only a directory that no longer exists
    and this check degraded to a skip on a stock machine.
    """
    on_path = shutil.which("machine-local")
    if on_path:
        return on_path
    settings_bin = os.path.join(str(settings_home()), "bin", "machine-local")
    if is_executable(settings_bin):
        return settings_bin
    # home_dir() (CLAUDE_HOME, else Path.home()) rather than a raw HOME env
    # read: native Windows shells don't set HOME, and os.environ.get("HOME", "")
    # degraded to a cwd-relative path there. This rung is now outranked by
    # settings-home above, but the landmine is removed rather than merely
    # dormant.
    home_bin = os.path.join(str(home_dir()), ".claude", "bin", "machine-local")
    if os.path.isfile(home_bin) and is_executable(home_bin):
        return home_bin
    local_bin = os.path.join(script_dir, "machine-local")
    if os.path.isfile(local_bin) and is_executable(local_bin):
        return local_bin
    return None


def _ml_get(ml_bin: str, key: str) -> Optional[str]:
    """Run `<ml_bin> get <key>`; return stripped stdout on rc==0, else None.

    timeout=20 bounds a single call so main()'s three unconditional calls stay
    within the ~60s ceiling the bash oracle's outer `timeout 60` used to impose
    on this whole script — that outer bound no longer exists now that
    sentinel.py's P-9 probe calls main() in-process (_call_native_main has no
    subprocess boundary of its own). A wedged `machine-local get <key>` (stuck
    file lock, broken FUSE/network mount) would otherwise hang the caller
    forever; a TimeoutExpired here is treated the same as any other failed
    lookup (returns None), matching this function's existing "no signal" ->
    None contract.
    """
    try:
        res = subprocess.run(
            [ml_bin, "get", key],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _ml_get: res = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _read_enabled_plugin(settings_path: str, key: str) -> str:
    """Read enabledPlugins[key] from settings_path; 'missing' on any read/parse/key-miss."""
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(f"skip: _read_enabled_plugin: with open(settings_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "missing"
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return "missing"
    val = enabled.get(key, "missing")
    if val is True:
        return "true"
    if val is False:
        return "false"
    return "missing" if val is None else str(val)


def main(argv: List[str], script_dir: Optional[str] = None) -> int:
    """CLI entrypoint: verify-ue-overrides (no positional args consumed; argv unused,
    kept for trampoline-call symmetry with other ported ops).

    `script_dir` is the THIRD rung of the ML_BIN resolver — a `machine-local` binary
    co-located next to the calling example-doctrine-repo script (coordinator/bin/machine-local), mirroring
    the bash oracle's `$SCRIPT_DIR/machine-local` fallback. The example-doctrine-repo trampoline passes its
    own directory here; this module's own file location (inside claude-klabauter) is NOT a valid
    substitute and must never be used for this rung — defaults to None (rung skipped)
    when called without it, e.g. directly from a test or a bare `python -m` invocation.
    """
    ml_bin = _resolve_ml_bin(script_dir or "")

    if ml_bin is None:
        fallback_path = os.path.join(script_dir, "machine-local") if script_dir else "machine-local"
        print(
            f"ERROR: machine-local not found on PATH and not at {fallback_path}",
            file=sys.stderr,
        )
        print("Remediation: verify your coordinator install — see", file=sys.stderr)
        print(
            "  ~/.claude/plugins/coordinator/docs/wiki/"
            "machine-local-registry.md § Verifying registry health",
            file=sys.stderr,
        )
        return 1

    def resolve_key(key: str) -> Optional[str]:
        val = _ml_get(ml_bin, key)
        if val is None:
            print(f"ERROR: machine-local key '{key}' not set on this machine", file=sys.stderr)
            print(
                "Remediation: populate the key in <settings-home>/machine-local/"
                "registry.local.toml",
                file=sys.stderr,
            )
            print(
                "  Default settings-home: ~/.coordinator-claude-settings "
                "(override via COORDINATOR_SETTINGS_HOME)",
                file=sys.stderr,
            )
            print(
                "  See coordinator/docs/wiki/machine-local-registry.md § "
                "Verifying registry health",
                file=sys.stderr,
            )
            return None
        return val

    example_game_repo_root = resolve_key("repos.example_game_workbench_repo")
    if example_game_repo_root is None:
        return 1
    example_retrieval_repo_root = resolve_key("repos.example_retrieval_repo")
    if example_retrieval_repo_root is None:
        return 1

    # Same Windows landmine as the legacy resolver rung above, second site:
    # native Windows shells do not set HOME, and os.environ.get("HOME", "")
    # would make the join below a RELATIVE ".claude" resolving against cwd — so
    # the walk would silently inspect the wrong directory and report a
    # misleading path. home_dir() is USERPROFILE-aware. Bound once here because
    # `home_claude_dir` below must be the SAME string as the list entry — the
    # loop compares against it by equality to special-case this one directory.
    home = str(home_dir())
    named_dirs: List[str] = [example_game_repo_root, example_retrieval_repo_root, os.path.join(home, ".claude")]

    example_sim_repo_root = _ml_get(ml_bin, "repos.example-sim-repo")
    if example_sim_repo_root:
        named_dirs.append(example_sim_repo_root)

    fail = False
    home_claude_dir = os.path.join(home, ".claude")

    for dir_path in named_dirs:
        if not os.path.isdir(dir_path):
            print(
                f"ERROR: resolved directory '{dir_path}' does not exist on this machine",
                file=sys.stderr,
            )
            print(
                "  Check the machine-local registry value that resolved to this path.",
                file=sys.stderr,
            )
            print(
                "  Remediation: ~/.claude/plugins/coordinator/docs/wiki/"
                "machine-local-registry.md § Verifying registry health",
                file=sys.stderr,
            )
            fail = True
            continue

        if dir_path == home_claude_dir:
            settings = os.path.join(dir_path, "settings.json")
        else:
            settings = os.path.join(dir_path, ".claude", "settings.json")

        if not os.path.isfile(settings):
            print(
                # The bash entrypoint this used to name (~/.claude/bin/
                # claude-ue-bootstrap.sh) was retired by the C5 native port and
                # is absent on every machine — the remediation was unrunnable as
                # written. Name the surviving CLI, which resolves through the
                # settings-home forwarder like every other coordinator CLI.
                f"MISSING: {settings} — run claude-ue-bootstrap {dir_path}",
                file=sys.stderr,
            )
            fail = True
            continue

        for key in _EXPECTED_KEYS:
            val = _read_enabled_plugin(settings, key)
            if val != "true":
                print(f"WRONG: {settings} [{key}] = {val} (expected true)", file=sys.stderr)
                fail = True

        game_dev_ok = False
        for key in _EITHER_VENDOR_GAME_DEV:
            val = _read_enabled_plugin(settings, key)
            if val == "true":
                game_dev_ok = True
        if not game_dev_ok:
            vendors = ", ".join(_EITHER_VENDOR_GAME_DEV)
            print(
                f"WRONG: {settings} — no game-dev vendor enabled "
                f"(expected at least one of: {vendors})",
                file=sys.stderr,
            )
            fail = True

    if not fail:
        print("all known UE-context dirs carry the expected override")
        return 0
    return 1
