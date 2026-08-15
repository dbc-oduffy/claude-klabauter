"""
coordinator_core.ops.verify_templates_setup_sync — byte-identity check between
live ~/.claude/setup helpers and their coordinator/templates/setup/ mirrors.

Purpose: inspect-only drift detector. Reports OK / MISMATCH / LIVE_MISSING /
TMPL_MISSING / NOT_PRESENT per tracked pair; exits non-zero if any pair is not
in the OK or NOT_PRESENT-for-both state. There is no --fix — recovery is
manual and template-as-authoritative (`cp coordinator/templates/setup/<file>
~/.claude/setup/<file>`); a prior live→template --fix path was removed (it
directly contradicted the outward-only doctrine — see
docs/plans/2026-05-21-generic-percolation-via-coordinator-install.md § Step 3
and the DoE lesson 2026-07-06-verify-templates-setup-sync-sh-fix-is-ba.yaml).

Port of: verify-templates-setup-sync.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-21-generic-percolation-via-coordinator-install.md § Step 3 [DEAD-CITATION: plan file never committed to this repo]

Negative-spec:
    - Takes no flags. A stray positional arg (e.g. a stale caller still
      passing `--fix`) is NOT actioned as a copy trigger — a warning is
      printed to stderr and the verify proceeds anyway, faithfully
      reproducing the bash oracle's behavior (which likewise ignored `$1`
      after the warning).
    - When BOTH sides of a pair are absent, that pair is a graceful skip
      (NOT_PRESENT), not a failure — this matches the "verifier authored
      before the files it verifies" bootstrap pattern used by sibling
      verify-*-sync scripts.
    - Exit code convention (parity-critical): 0 only if every checked pair is
      OK, or if not a single pair was checked and no mismatch/missing state
      was recorded (the "no files present" bootstrap case). Any MISMATCH,
      LIVE_MISSING, or TMPL_MISSING makes the whole run exit 1.
    - An unresolvable plugin root (CLAUDE_PLUGIN_ROOT unset) is a hard
      failure (exit 1, PluginRootUnresolved), not a Path.cwd() fallback —
      cwd is whatever directory the caller happened to invoke from, and a
      silent wrong-root would misreport every pair as TMPL_MISSING/OK
      against the wrong tree rather than surfacing the actual problem
      (caller failed to resolve/set CLAUDE_PLUGIN_ROOT). See
      _resolve_plugin_root()'s docstring for the caller-resolves-topology
      split this enforces.
"""

from __future__ import annotations

import filecmp
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core._settings_home import claude_config_dir

# Pairs: (live_relative_name, template_relative_name) — relative filenames,
# both dirs rooted at LIVE_SETUP / TEMPLATES_SETUP respectively.
PAIRS: List[Tuple[str, str]] = [
    ("publish.sh", "publish.sh"),
    ("publish_sync.py", "publish_sync.py"),
    ("publish-targets.example.sh", "publish-targets.example.sh"),
    (".percolate-identity.example", ".percolate-identity.example"),
    ("percolate-hooks/README.md", "percolate-hooks/README.md"),
    (
        "percolate-hooks/_lib/depersonalize-bin-resolve.sh",
        "percolate-hooks/_lib/depersonalize-bin-resolve.sh",
    ),
    (
        "percolate-hooks/coordinator-claude/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude/post-rsync/10-transform.sh",
    ),
    (
        "percolate-hooks/coordinator-claude-publish-repo-docs/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-docs/post-rsync/10-transform.sh",
    ),
    (
        "percolate-hooks/coordinator-claude-publish-repo-setup/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-setup/post-rsync/10-transform.sh",
    ),
    (
        "percolate-hooks/coordinator-claude-publish-repo-toplevel/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-toplevel/post-rsync/10-transform.sh",
    ),
    (
        "percolate-hooks/coordinator-claude-toplevel-wiki/post-rsync/20-transform.sh",
        "percolate-hooks/coordinator-claude-toplevel-wiki/post-rsync/20-transform.sh",
    ),
    (
        "percolate-hooks/coordinator-claude-toplevel-install/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-toplevel-install/post-rsync/10-transform.sh",
    ),
]


class PluginRootUnresolved(RuntimeError):
    """Raised by _resolve_plugin_root() when CLAUDE_PLUGIN_ROOT is unset.

    There is no fallback root for this module to guess at — see
    _resolve_plugin_root()'s docstring for why a cwd() fallback is a
    silent-wrong-root hazard, not a graceful default.
    """


def _resolve_plugin_root() -> Path:
    """Resolves the plugin root (coordinator/templates/setup/'s parent).

    This module does NOT derive the root from its own __file__ location:
    it is imported from the claude-klabauter side, not from a copy of the DoE
    coordinator/bin/ tree, so `__file__`-relative resolution would point at
    the wrong repo entirely (claude-klabauter has no templates/ tree at all).
    It also does NOT fall back to Path.cwd() when CLAUDE_PLUGIN_ROOT is
    unset — cwd is whatever directory the caller happened to invoke from,
    a silent-wrong-root twin of the __file__ hazard this module already
    guards against. Resolving the DoE-claude repo root is the CALLER's
    job (topology knowledge the caller has and this module does not): the
    coordinator/bin/verify-templates-setup-sync.py trampoline sets
    CLAUDE_PLUGIN_ROOT via the doe_root() registry helper before invoking
    main(); coordinator_core.plugin_health.sentinel's in-process probe P-11
    call sets it directly from its own resolved plugins_root. Both callers
    are responsible for setting the env var — this function's only job is
    to read it, or fail loud when it is missing.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    raise PluginRootUnresolved(
        "CLAUDE_PLUGIN_ROOT is unset — cannot resolve the plugin root that "
        "owns templates/setup/. The caller must resolve the DoE-claude repo "
        "root (e.g. via the coordinator_registry.doe_root() ladder) and set "
        "CLAUDE_PLUGIN_ROOT before calling main()."
    )


def check_pairs(
    templates_setup: Path, live_setup: Path
) -> Tuple[List[str], int]:
    """Runs the sync check over PAIRS. Returns (report_lines, exit_code)."""
    lines: List[str] = []
    exit_code = 0
    any_checked = False

    for live_name, tmpl_name in PAIRS:
        live_path = live_setup / live_name
        tmpl_path = templates_setup / tmpl_name

        live_exists = live_path.is_file()
        tmpl_exists = tmpl_path.is_file()

        if not live_exists and not tmpl_exists:
            lines.append(
                f"NOT_PRESENT  {live_name} (neither live nor template exists "
                "yet — Step 1 will create them)"
            )
            continue

        if not live_exists:
            lines.append(
                f"LIVE_MISSING {live_name} (template exists but live copy "
                f"absent at {live_path})"
            )
            exit_code = 1
            continue

        if not tmpl_exists:
            lines.append(
                f"TMPL_MISSING {live_name} (live exists but template absent "
                f"at {tmpl_path})"
            )
            exit_code = 1
            continue

        any_checked = True

        if filecmp.cmp(str(live_path), str(tmpl_path), shallow=False):
            lines.append(f"OK           {live_name}")
        else:
            lines.append(f"MISMATCH     {live_name}")
            exit_code = 1

    if not any_checked and exit_code == 0:
        lines.append("no files present — nothing to verify (Step 1 will create the live and template copies)")

    return lines, exit_code


def main(argv: List[str]) -> int:
    """CLI entry: resolve roots, warn on stray args, run check, print report."""
    if argv and argv[0] != "":
        print(
            "WARNING: verify-templates-setup-sync takes no flags (--fix was "
            "removed; inspect-only now).",
            file=sys.stderr,
        )
        print(
            "         Manual recovery is template-as-authoritative: "
            "cp coordinator/templates/setup/<file> ~/.claude/setup/<file>",
            file=sys.stderr,
        )

    try:
        plugin_root = _resolve_plugin_root()
    except PluginRootUnresolved as exc:
        print(f"verify-templates-setup-sync: {exc}", file=sys.stderr)
        return 1

    templates_setup = plugin_root / "templates" / "setup"
    live_setup = claude_config_dir() / "setup"

    lines, exit_code = check_pairs(templates_setup, live_setup)
    for line in lines:
        print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
