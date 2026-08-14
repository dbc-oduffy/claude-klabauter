"""
coordinator_core.ops.generate_repomap — thin wrapper around the Python
repomap generator. Port of: generate-repomap.sh (DoE b5a4192c, 2026-07-20).

Purpose: run generate-repomap.py with default arguments. Contains NO
RAG-gating logic — callers gate via coordinator/bin/check-rag-state.py
(DoE-resident) before invoking this. Full gating doctrine:
docs/wiki/repomap-rag-gating.md (DoE-resident).

Spec backlink: docs/plans/2026-05-09-skill-consolidation-pass.md § T2

Negative-spec:
    - Does NOT gate on RAG state.
    - Reproduces the original's exact 3-tier generator resolution order
      verbatim — (1) plugin-relative canonical path
      (<plugin_root>/bin/repomap/generate-repomap.py), (2) legacy meta-repo
      global install (~/.claude/.github/scripts/generate-repomap.py), (3)
      repo-local fallback (.github/scripts/generate-repomap.py). Tiers 2-3
      are intentional legacy bridges — do not add a fourth tier or reorder
      without keeping this module's own tier list in lockstep.
    - Trust-checks the resolved plugin_root via the canonical
      `coordinator_core.trusted_root_guard.is_trusted` (fail-loud
      call-site shape) rather than a local reimplementation — see that
      module for the full anchor list (`.claude/` prefix, DoE clone,
      registry-resolved claude-klabauter root, `COORDINATOR_PLUGIN_ROOT_TRUSTED=1`
      opt-out).
    - Reproduces the original's interpreter-probe order (python3 → python →
      py -3), with the same PYTHON= override escape hatch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from coordinator_core.trusted_root_guard import is_trusted as _trusted_root


def _resolve_python_cmd() -> Optional[List[str]]:
    override = os.environ.get("PYTHON", "").strip()
    if override:
        return override.split(" ")
    for candidate in ("python3", "python"):
        if shutil.which(candidate):
            return [candidate]
    if shutil.which("py"):
        return ["py", "-3"]
    return None


def main(argv: List[str], plugin_root: Optional[str] = None, site: str = "generate-repomap.sh") -> int:
    plugin_root = plugin_root or os.environ.get("CLAUDE_PLUGIN_ROOT") or os.getcwd()

    if not _trusted_root(plugin_root):
        print(
            f"ERROR: {site} '{plugin_root}' outside trusted prefix — refusing to source; "
            "re-run coordinator:install (or set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a "
            "sanctioned --plugin-dir spike)",
            file=sys.stderr,
        )
        return 1

    candidates = [
        os.path.join(plugin_root, "bin", "repomap", "generate-repomap.py"),
        os.path.join(os.path.expanduser("~"), ".claude", ".github", "scripts", "generate-repomap.py"),
        os.path.join(".github", "scripts", "generate-repomap.py"),
    ]
    generator = next((c for c in candidates if os.path.isfile(c)), None)
    if generator is None:
        print("ERROR: generate-repomap.py not found.", file=sys.stderr)
        print(f"  Expected (tier 1): {candidates[0]}", file=sys.stderr)
        print(f"  Fallback (tier 2, legacy): {candidates[1]}", file=sys.stderr)
        print(f"  Fallback  (tier 3): {candidates[2]}", file=sys.stderr)
        print("  Install the coordinator-claude plugin or run the setup script.", file=sys.stderr)
        return 1

    python_cmd = _resolve_python_cmd()
    if python_cmd is None:
        print("ERROR: no python interpreter found (tried python3, python, py).", file=sys.stderr)
        print("  Set PYTHON=<path> to pin explicitly.", file=sys.stderr)
        return 1

    if not argv:
        cmd = python_cmd + [
            generator,
            "--project-root",
            os.environ.get("PROJECT_ROOT", "."),
            "--budget",
            "4000",
            "--profile",
            "balanced",
        ]
    else:
        cmd = python_cmd + [generator] + list(argv)

    from coordinator_core.win_portability import no_console_passthrough_kwargs

    result = subprocess.run(cmd, **no_console_passthrough_kwargs())
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
