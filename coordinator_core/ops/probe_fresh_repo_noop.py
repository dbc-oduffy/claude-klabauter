"""
coordinator_core.ops.probe_fresh_repo_noop — JSON-RPC "update_docs.probe_fresh_repo_noop"
operation.

Purpose: the freshness pre-check `/update-docs` fences against before running its full
distill/sweep pipeline. A freshly-scaffolded repo has no `DIRECTORY.md`, an empty
`archive/completed/`, and no distillable `tasks/*.md` — running the full documentation
pipeline against that state is wasted work, so the fence short-circuits to a no-op when
all three hold. This module is that three-axis check: READ-ONLY filesystem
existence/glob probes, no mutation, so it is safe to invoke any number of times with
identical inputs (AC7 — no idempotency-hazard docstring needed per the manifest row,
which rates both idempotency- and platform-hazard "none").

Domain note: this is a distinct domain from `install/prereq_probe.py` (toolchain
presence checks for install). This module probes REPO freshness, not toolchain
presence — a new module, not an extension.

Self-registration: importing this module calls register_op("update_docs.probe_fresh_repo_noop",
...) as a side-effect (same pattern as ops/artifact_emit.py). coordinator_core.ops.__init__
imports it so the registration fires at start_server()/dispatch time.

Op-key / contract (state/audits/2026-07-22-command-payload-inventory/op-classification.tsv):
    update_docs.probe_fresh_repo_noop
    params: {} (repo_root injected via _origin_worktree)
    -> {"is_fresh": bool, "reasons": list[str]}

Scope: common_dir (DIRECTORY.md / archive/ / tasks/ are main-worktree-rooted paths, shared
across linked worktrees of the same repo — same class as state/, handoffs). The handler
derives the main worktree root from the dispatch-provided repo_root via
coordinator_core.ops.fleet._common.main_worktree_root, mirroring artifact_emit.py's
derivation (a linked worktree's .git is a file; using the raw common_dir directly would
silently probe the wrong tree).

Fence source: commands/update-docs.md:30 (distinct-ops-new.tsv row
"probe-fresh-repo-noop"). Original fence rationale: "cwd guard at top falls through to
normal pipeline rather than false-triggering" — this op is the guard; a false "fresh"
verdict on a populated repo must never suppress the real pipeline, so every reason is
independently computed and ANY unmet freshness condition sets is_fresh False.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
(wave 1, chunk w2-fresh-repo-noop)

Negative-spec:
    - Does NOT write anything — three read-only pathlib existence/glob checks, no
      mutation, no lock, no atomic-write path.
    - Does NOT shell out — plain pathlib, no subprocess, no bash/coreutils.
    - Does NOT treat an absent `tasks/` directory as non-fresh — an absent directory
      has no distillable *.md files by construction, same as an empty one.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root


def _check_directory_md(repo_root: Path) -> Tuple[bool, str]:
    """True (met) iff DIRECTORY.md is absent at the repo root."""
    if (repo_root / "DIRECTORY.md").exists():
        return False, "DIRECTORY.md exists at repo root"
    return True, "no DIRECTORY.md at repo root"


def _check_archive_completed_empty(repo_root: Path) -> Tuple[bool, str]:
    """True (met) iff archive/completed/ is absent or contains no entries."""
    completed = repo_root / "archive" / "completed"
    if not completed.is_dir():
        return True, "archive/completed/ absent"
    if any(completed.iterdir()):
        return False, "archive/completed/ is non-empty"
    return True, "archive/completed/ is empty"


def _check_no_distillable_tasks(repo_root: Path) -> Tuple[bool, str]:
    """True (met) iff tasks/ has no *.md files (absent tasks/ counts as met)."""
    tasks_dir = repo_root / "tasks"
    if not tasks_dir.is_dir():
        return True, "tasks/ absent"
    md_files = list(tasks_dir.glob("*.md"))
    if md_files:
        return False, f"tasks/ has {len(md_files)} distillable *.md file(s)"
    return True, "tasks/ has no distillable *.md files"


def probe_fresh_repo(repo_root: Path) -> Tuple[bool, List[str]]:
    """Run the three-axis freshness check against repo_root.

    Returns (is_fresh, reasons) where is_fresh is True only if ALL three axes are
    met, and reasons lists a human-readable line per axis (met or unmet) so a
    caller can see exactly why the verdict landed where it did.
    """
    checks = (
        _check_directory_md(repo_root),
        _check_archive_completed_empty(repo_root),
        _check_no_distillable_tasks(repo_root),
    )
    is_fresh = all(met for met, _ in checks)
    reasons = [reason for _, reason in checks]
    return is_fresh, reasons


@register_op("update_docs.probe_fresh_repo_noop")
async def _probe_fresh_repo_noop(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'update_docs.probe_fresh_repo_noop' handler.

    Params: {} (no inputs consumed).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the
    originating worktree. Derives the main-worktree root via
    main_worktree_root(repo_root) before probing — a linked worktree's .git is a
    file, so probing repo_root directly would look in the wrong place. Fails
    loud when repo_root is None (no silent fallback to claude-klabauter's own tree).
    """
    if repo_root is None:
        raise ValueError(
            "update_docs.probe_fresh_repo_noop requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be 'common_dir' "
            "and _origin_worktree must be present in the JSON-RPC envelope."
        )
    derived_root = main_worktree_root(repo_root)
    is_fresh, reasons = probe_fresh_repo(derived_root)
    return {"is_fresh": is_fresh, "reasons": reasons}
