"""
coordinator_core.ops.probe_onboarding_currency — P-13 onboarding currency classifier.

Purpose: compares a repo's onboarded COORDINATOR_SCHEMA_VERSION stamp against the
coordinator source's current schema version and prints one of four statuses:

    current              — stamp present, matches current schema version
    drift(N->M)          — stamp present, older than current version
    unstamped(legacy)    — probe ran OK, no stamp found; repo predates currency feature
    inconclusive(...)    — probe could not run (schema constant unreadable, etc.)
    source_is_live        — repo IS the coordinator source itself; missing stamp expected

Port of: probe-onboarding-currency.sh (example-doctrine-repo 894d4bc6, 2026-07-22), which also
inlined lib/coordinator-currency.sh::coordinator_currency_probe (that shared bash lib
was not itself ported here — its read/write/probe logic is reproduced standalone below
so this module has no bash dependency).

Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 1.
Doctrine: docs/wiki/doctor-probe-design.md § inconclusive Is a First-Class Probe Status.

Exit codes:
    0 — probe ran and produced a classification (the overwhelmingly common case;
        the probe is designed to NEVER block a caller on a soft-infra issue)
    1 — FATAL: CLAUDE_HOME resolved to a path already ending in /.claude (F14 guard:
        CLAUDE_HOME is a $HOME substitute, not the .claude dir itself — a caller
        passing the wrong value would silently double-append and probe the wrong
        repo). This is the ONE fail-loud branch; every other path exits 0.

Interface — env-only (mirrors the original script; no CLI args):
    CLAUDE_HOME                          — repo root to check (default: $HOME); /.claude
                                            is appended unless COORDINATOR_CURRENCY_REPO_ROOT
                                            is set (see F14 guard above)
    COORDINATOR_CURRENCY_REPO_ROOT       — explicit repo_root override, used verbatim
    COORDINATOR_CURRENCY_SOURCE_IS_LIVE  — set to "1" to force-treat a missing stamp
                                            as expected (skip, not a warning)
    COORDINATOR_CURRENCY_PLUGIN_ROOT     — override coordinator plugin root (the dir
                                            containing coordinator-schema-version).
                                            The trampoline (coordinator/bin/
                                            probe-onboarding-currency.py) always
                                            sets this explicitly before calling
                                            main() — see its _resolve_plugin_root()
                                            (env override wins verbatim, else
                                            resolves via coordinator_registry.doe_root()
                                            + "/coordinator").
    COORDINATOR_CURRENCY_SCRIPT_DIR      — the CALLING trampoline's own directory.
                                            example-doctrine-repo-side contract fact (path resolution
                                            specific to where the trampoline file
                                            lives on disk) that this engine module
                                            cannot derive itself — the trampoline sets
                                            it before calling main(). Used to
                                            auto-detect source_is_live (script lives
                                            under <repo_root>/plugins/), and as a
                                            dirname(SCRIPT_DIR) fallback default for
                                            plugin_root when PLUGIN_ROOT is unset —
                                            that fallback assumes the calling script
                                            and coordinator-schema-version are
                                            co-located (true of the retired example-doctrine-repo
                                            layout, NOT of the current trampoline,
                                            which migrated to claude-klabauter while
                                            coordinator-schema-version stayed in
                                            example-doctrine-repo's coordinator/ tree — see that
                                            trampoline's module docstring "Plugin-root
                                            resolution note"). No known caller relies
                                            on this fallback today (the trampoline and
                                            plugin_health/sentinel.py's in-process P-13
                                            caller both always set PLUGIN_ROOT
                                            explicitly); it is retained only for a
                                            hypothetical co-located-layout caller and
                                            MUST NOT be "restored" as the plugin-root
                                            default without re-verifying co-location.

Negative-spec:
    - Does NOT source or shell out to lib/coordinator-currency.sh — this module is a
      standalone reimplementation of coordinator_currency_probe's read/classify path.
      The bash-oracle branch that printed
      `inconclusive(probe-infra: lib/coordinator-currency.sh not found ...)` when that
      file was missing does not apply here (no such file dependency exists post-port);
      this is not a behavior regression since that branch only fired on a broken
      install where the example-doctrine-repo bin/lib layout itself was corrupt.
    - Forward-dated stamps (stamped_version > current_version) are classified
      inconclusive, NOT drift or current — reproduces the bash oracle's stated
      "probe cannot validate forward stamps" behavior verbatim, not a bug to fix.
    - CLAUDE_HOME/.claude concatenation is literal string concatenation (mirrors bash
      `"${CLAUDE_HOME:-$HOME}/.claude"`), not os.path.join — a CLAUDE_HOME with a
      trailing slash produces a doubled slash exactly as the bash oracle did.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional

_SCHEMA_VERSION_RE = re.compile(r"^[1-9][0-9]*$")
_STAMP_LINE_RE = re.compile(r"^schema_version:\s*(.*)$", re.MULTILINE)


def _strip_one_trailing_slash(value: str) -> str:
    """Mirror bash `${value%/}` — strips AT MOST one trailing slash."""
    if value.endswith("/"):
        return value[:-1]
    return value


def _read_schema_version(plugin_root: str) -> Optional[str]:
    ver_file = os.path.join(plugin_root, "coordinator-schema-version")
    try:
        with open(ver_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        print(f"skip: _read_schema_version: with open(ver_file, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    ver = re.sub(r"\s+", "", raw)
    if _SCHEMA_VERSION_RE.match(ver):
        return ver
    return None


def _read_stamp(stamp_path: str) -> Optional[str]:
    try:
        with open(stamp_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: _read_stamp: with open(stamp_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    match = _STAMP_LINE_RE.search(text)
    if not match:
        return None
    ver = re.sub(r"\s+", "", match.group(1))
    if _SCHEMA_VERSION_RE.match(ver):
        return ver
    return None


def coordinator_currency_probe(repo_root: str, plugin_root: str) -> str:
    """Read-only classification. Always succeeds (never raises) — returns a status string."""
    current_version = _read_schema_version(plugin_root)
    if current_version is None:
        return (
            f"inconclusive(schema constant unreadable: "
            f"{plugin_root}/coordinator-schema-version missing or malformed)"
        )

    stamp_path = os.path.join(repo_root, "docs", "coordinator-currency.yaml")
    if not os.path.isfile(stamp_path):
        return "unstamped(legacy)"

    stamped_version = _read_stamp(stamp_path)
    if stamped_version is None:
        return f"inconclusive(stamp present but schema_version unparseable: {stamp_path})"

    if stamped_version == current_version:
        return "current"
    if int(stamped_version) < int(current_version):
        return f"drift({stamped_version}->{current_version})"
    return (
        f"inconclusive(stamped version {stamped_version} > current version "
        f"{current_version}; probe cannot validate forward stamps)"
    )


def main(argv: List[str]) -> int:
    """CLI entry: env-only interface (no positional args), mirrors the bash oracle."""
    script_dir = os.environ.get("COORDINATOR_CURRENCY_SCRIPT_DIR", "")

    plugin_root = os.environ.get("COORDINATOR_CURRENCY_PLUGIN_ROOT", "")
    if not plugin_root:
        plugin_root = os.path.dirname(script_dir) if script_dir else ""

    repo_root_override = os.environ.get("COORDINATOR_CURRENCY_REPO_ROOT", "")
    if repo_root_override:
        repo_root = repo_root_override
    else:
        claude_home = os.environ.get("CLAUDE_HOME", "")
        # Separator-agnostic: a Windows CLAUDE_HOME arrives backslash-separated
        # (e.g. "...\.claude"), so a bare "/.claude" suffix check silently
        # misses the F14 guard on Windows.
        claude_home_cmp = claude_home.replace("\\", "/") if claude_home else claude_home
        if claude_home_cmp and _strip_one_trailing_slash(claude_home_cmp).endswith("/.claude"):
            print(
                f"probe-onboarding-currency: FATAL: CLAUDE_HOME='{claude_home}' ends in '/.claude'.",
                file=sys.stderr,
            )
            print(
                "  CLAUDE_HOME is a $HOME substitute, NOT the .claude directory itself.",
                file=sys.stderr,
            )
            print("  Remediation: set CLAUDE_HOME to the PARENT of .claude", file=sys.stderr)
            print(
                "  (e.g. export CLAUDE_HOME=$HOME  or  export CLAUDE_HOME=/home/<username>)",
                file=sys.stderr,
            )
            return 1
        repo_root = (
            f"{claude_home or os.environ.get('HOME') or os.environ.get('USERPROFILE', '')}"
            "/.claude"
        )

    source_is_live = False
    if os.environ.get("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", "0") == "1":
        source_is_live = True
    elif script_dir:
        chome_norm = _strip_one_trailing_slash(repo_root)
        script_norm = _strip_one_trailing_slash(script_dir)
        if script_norm.startswith(chome_norm + "/plugins"):
            source_is_live = True

    if source_is_live:
        print("source_is_live")
        return 0

    print(coordinator_currency_probe(repo_root, plugin_root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
