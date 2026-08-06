"""
coordinator_core.claude_klabauter_root — ported from coordinator/lib/coordinator-claude-klabauter-root.sh
(example-doctrine-repo clean-slate migration, sourced-lib variant — example-doctrine-repo .sh is left untouched; its ~60
`source coordinator-claude-klabauter-root.sh` callers switch to `import coordinator_core.claude_klabauter_root`
in a later gated wave, per port-template variant "SOURCED LIB").

Purpose: resolves the claude-klabauter sibling-repo root, analogous to how CLAUDE_HOME->~/.claude
works for the coordinator meta-repo. Mirror-image of `coordinator_core.ops.gen_doe_root_pointer`
(which resolves DOE_ROOT from inside a example-doctrine-repo-clone-relative context) — this module resolves
CLAUDE_KLABAUTER_ROOT for callers already running inside the claude-klabauter engine.

Spec backlink: docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md § C1 / AC1
Windows portability rung: docs/plans/2026-07-14-claude-klabauter-windows-portability.md § C1

Resolution chain (unchanged from the bash oracle), in order:
  1. CLAUDE_KLABAUTER_ROOT env var — if already set, return it unchanged.
  1.5. <settings-home>/machine-local/.claude-klabauter-root pointer file — a cheap direct-file-read,
       checked ahead of the expensive machine-local subprocess ladder so per-invoke
       resolution spawns zero subprocesses on Windows. Falls through to rung 2 if
       absent/empty.
  2. `machine-local get repos.claude_klabauter` (CLI, PATH-resolved) — delegates to the
     §4c four-rung discovery ladder (explicit env override -> OS-keyed search-root
     marker autodiscovery -> path-exceptions -> registry.local.toml fallback). Does NOT
     reimplement those rungs here — shells out exactly like the bash oracle's Rung 2, so
     the registry-merge logic has exactly one implementation.
  3. Hard error (RuntimeError) with actionable remediation, mirroring the bash oracle's
     stderr message verbatim (module-level constant so callers can print it themselves).

Public API:
    def coordinator_claude_klabauter_root() -> str   — mirrors the shell function of the same name.
        Returns the resolved absolute path. Raises RuntimeError (message = the bash
        oracle's stderr remediation text) on failure. Unlike the shell function this
        does NOT export CLAUDE_KLABAUTER_ROOT into os.environ on success as a side effect free
        of caller intent — callers that want the §4b idempotency-gate behavior set
        os.environ["CLAUDE_KLABAUTER_ROOT"] themselves after a successful call.

Negative-spec:
    - Does NOT reimplement the machine-local registry.toml/registry.local.toml parser —
      shells out to the `machine-local` CLI (PATH-resolved), exactly like the bash
      oracle's Rung 2 and gen_doe_root_pointer.py's Tier 2.
    - Does NOT export CLAUDE_KLABAUTER_ROOT to os.environ as a side effect (the bash oracle does,
      per its own §4b idempotency-gate docstring) — a pure resolver is safer to import
      from a long-lived process (e.g. a future op) where implicit env mutation on
      import-time-adjacent calls would be a surprising side effect. Callers that need
      the shell's idempotency-gate behavior opt in explicitly.
      Review: code-reviewer — this note previously recorded a deliberate
      ASYMMETRY against `coordinator_core.ops.coordinator_doe_root`, which did
      export `REPO_EXAMPLE_DOCTRINE_REPO` to os.environ on every successful resolution to
      mirror ITS bash oracle's `export`. That asymmetry was retired on
      2026-07-21: the export leaked interpreter-global state across tests and
      into every subprocess child's inherited env, and `coordinator_doe_root` is
      now pure too (its re-resolution guard moved to an explicit module-scope
      memo with a reset seam). Both resolvers now make the SAME choice, and this
      module's was the one that turned out right — see that module's docstring
      § DECISION REVERSAL.
    - Does NOT spawn a subprocess for Rung 1 or Rung 1.5 — only Rung 2 shells out.
    - The bash oracle's `set -uo pipefail` / BASH_VERSINFO guard notes have no meaning
      here — this is a pure-Python module. Omitted intentionally.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from coordinator_core._settings_home import machine_local_dir

#: Wall-clock bound on the Rung 2 `machine-local` subprocess. This resolver is
#: reached from PreToolUse hook paths, so an unbounded wait blocks an interactive
#: tool call outright; a timeout degrades to Rung 3's actionable error instead,
#: which is the same disposition Rung 2 already takes on an exec failure. Sized to
#: be far longer than a registry read and far shorter than a hook budget.
_RUNG2_TIMEOUT_SECS = 2.0

_REMEDIATION = (
    "coordinator_claude_klabauter_root: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set.\n"
    "  The machine-local registry has no 'repos.claude_klabauter' entry on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c"
)


def coordinator_claude_klabauter_root() -> str:
    """Resolve the claude-klabauter sibling-repo root via the documented chain.

    Returns the resolved absolute path (as read — no realpath/normalization beyond
    the source's own whitespace-strip, mirroring the bash oracle's behavior).
    Raises RuntimeError with the bash oracle's remediation text on failure.
    """
    # Rung 1: CLAUDE_KLABAUTER_ROOT already set in environment (§4b idempotency gate).
    existing = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if existing:
        return existing

    # Rung 1.5: cheap direct-file-read pointer, checked ahead of the expensive
    # machine-local subprocess ladder. Absence/emptiness is a normal fallback
    # state, not an error — falls through to Rung 2.
    pointer_path = machine_local_dir() / ".claude-klabauter-root"
    try:
        with open(pointer_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass  # missing/unreadable pointer file — normal, falls through to Rung 2

    # Rung 2: machine-local registry (§4c four-rung discovery ladder runs inside
    # the `machine-local` CLI itself — not reimplemented here).
    ml_bin = shutil.which("machine-local")
    if ml_bin is not None:
        try:
            result = subprocess.run(
                [ml_bin, "get", "repos.claude_klabauter"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_RUNG2_TIMEOUT_SECS,
            )
        except OSError:
            # `which` found a `machine-local` on PATH but exec failed (e.g. removed
            # mid-race, not actually executable) — falls through to Rung 3's hard
            # error like any other unresolved case; not a distinct failure mode.
            result = None
        except subprocess.TimeoutExpired:
            # Same disposition as the exec failure above: an unresolved Rung 2, not a
            # distinct failure mode. The bound exists because this resolver is reached
            # from PreToolUse hook paths on the interactive critical path — an
            # unbounded wait there hangs the tool call rather than degrading to Rung
            # 3's actionable error, and a slow answer is worth less than a prompt one.
            result = None

        if result is not None and result.returncode == 0:
            resolved = result.stdout.strip()
            if resolved:
                return resolved

    # Rung 3: hard error with actionable remediation.
    raise RuntimeError(_REMEDIATION)
