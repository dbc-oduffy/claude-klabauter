"""
coordinator_core.ops.seed_skill_overrides — thin orchestration wrapper around
bin/seed-skill-overrides.py (example-doctrine-repo-resident); direct-import port of
coordinator/bin/install-health/seed-skill-overrides.sh.

Purpose: resolve the example-doctrine-repo-resident seed-skill-overrides.py helper path, build
its CLI args (--check-only when CHECK_ONLY is set, --with-deep-research
always — deep-research ships bundled in coordinator post-Wave-C4), and
invoke it via subprocess. The helper itself is NOT ported — it stays
Example-doctrine-repo-resident and owns the actual settings.json merge logic; this module only
replaces the bash orchestration shell (trust-guard + arg-building +
graceful-degrade-on-absent-helper).

Port source: coordinator/bin/install-health/seed-skill-overrides.sh
    (example-doctrine-repo), replaced with a sh/python polyglot trampoline over this
    module on cutover.
Spec backlink: docs/plans/2026-06-27-ccos-1-dual-context-validator.md
    (seed-skill-overrides chunk); install-health drop-in plan (2026-06-27).

Negative-spec:
    - Trust-checks plugin_root via the canonical
      `coordinator_core.trusted_root_guard.is_trusted` (fail-loud
      call-site shape), same as `coordinator_core.ops.generate_repomap` —
      see that module for the full anchor list.
    - Degrades gracefully (exit 0, WARNING to stderr) when the helper script
      is absent — does NOT fail the whole install-health orchestrator (a
      partial publish or a future refactor that moved the helper must not
      block the drop-in loop). This is the ONE non-fail-loud branch in an
      otherwise fail-loud trampoline; preserved verbatim from the original.
    - Always passes --with-deep-research (post-C4 merge, deep-research is
      unconditionally bundled — see the original .sh's own "Deep-research
      override" comment for the historical detection-based rationale this
      superseded).
    - Exit code is whatever the subprocess helper returns, propagated
      unchanged (matches the original's `set -euo pipefail` tail-call
      semantics — the last command's exit code IS the script's exit code).
"""
from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_passthrough_kwargs
import sys
from typing import List, Optional

from coordinator_core.trusted_root_guard import is_trusted as _trusted_root


def main(
    argv: List[str],
    plugin_root: Optional[str] = None,
    site: str = "seed-skill-overrides.sh",
    helper_root: Optional[str] = None,
) -> int:
    """Entry point.

    ``plugin_root`` is the trust-check anchor only (unchanged since this
    module's introduction — the example-doctrine-repo-side invoking-harness root the
    fail-loud trust-core validates). ``helper_root`` is the DIFFERENT root
    the `bin/seed-skill-overrides.py` helper is actually looked up under —
    added so a caller resolving legs off claude-klabauter (`coordinator_claude_klabauter_root()`,
    per the dual-anchor split: example-doctrine-repo-side trust anchor stays plugin_root,
    claude-klabauter-side content resolves off claude-klabauter root) can repoint the helper
    lookup without touching trust semantics. Defaults to ``plugin_root``,
    preserving the original single-root behavior for any other caller.

    Deliberate isolation boundary — do not convert the helper-script spawn
    to an in-process import. Mechanism: distinct interpreter — runs the
    `bin/seed-skill-overrides.py` helper under a resolved `python_cmd`, not
    necessarily this process's own interpreter. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    plugin_root = plugin_root or os.environ.get("CLAUDE_PLUGIN_ROOT") or os.getcwd()
    helper_root = helper_root or plugin_root

    if not _trusted_root(plugin_root):
        print(
            f"ERROR: {site} '{plugin_root}' outside trusted prefix — refusing to source; "
            "re-run coordinator:install (or set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a "
            "sanctioned --plugin-dir spike)",
            file=sys.stderr,
        )
        return 1

    helper = os.path.join(helper_root, "bin", "seed-skill-overrides.py")
    if not os.path.isfile(helper):
        print(
            f"[seed-skill-overrides] WARNING: helper not found at {helper}; skipping",
            file=sys.stderr,
        )
        return 0

    args: List[str] = []
    if os.environ.get("CHECK_ONLY"):
        args.append("--check-only")
    # Deep-research is always bundled in coordinator post-C4 — always seed the override.
    args.append("--with-deep-research")

    python_cmd = sys.executable or "python3"
    result = subprocess.run(
        [python_cmd, helper] + args,
        **no_console_passthrough_kwargs(),
    )  # popup-safe-env-suppressed
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
