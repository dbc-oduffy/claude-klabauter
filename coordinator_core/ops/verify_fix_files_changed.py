"""
coordinator_core.ops.verify_fix_files_changed — JSON-RPC
"bug_sweep.verify_fix_files_changed" operation.

Purpose: the bug-sweep Phase-4 mechanical diff gate, ported native (fence:
Coordinator-claude coordinator/skills/bug-sweep/SKILL.md:261). The bash oracle piped
`jq -r '.[].file'` through `sort -u`, `comm -23`, and `<(...)` process
substitution — bash-only (no dash/sh/native-Windows equivalent) and
dependent on `jq` and `comm` being installed. Settlement B9
(docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B9,
RATIFIES) collapses the whole pipeline:

    json.load(phase2_fix_now_path)      → expected-fixed set
    `git diff --name-only` (list argv)  → changed set
    claimed_no_diff = sorted(expected - changed)

Zero shell pipeline, zero external-tool dependency — a plain Python set
difference over the JSON manifest and git's own output. The result names the
files an executor CLAIMED to fix that show no working-tree diff (the
false-positive / silent-no-op cohort the gate exists to catch).

Manifest shape (fence contract, `jq -r '.[].file'`): a JSON ARRAY of objects
each carrying a string `file` key (repo-relative path). Malformed or missing
JSON → structured error — the manifest IS the premise (settlement B9); an
entry without a string `file` is malformed, not skippable (where jq would
silently emit `null`, this port fails loud — the oracle's silent-null was an
accident of jq, not a contract).

Idempotency (AC7, DEC-7 note): INHERENT — pure read (one JSON read + one
read-only `git diff --name-only` subprocess), zero writes, zero state
accretion; identical inputs against an unchanged working tree return
identical results. The double-invocation test asserts two back-to-back calls
return identical results.

CC-1 note: `git` is a named cross-platform binary spawned via direct
list-argv `subprocess.run` — no `shell=True`, no bash/sh/cmd string, no
process substitution anywhere in the invocation chain.

Path comparison is VERBATIM string equality against git's repo-relative
forward-slash output — matching the oracle's `comm -23` byte comparison
exactly; no separator normalization is applied to manifest entries (a
manifest authored with backslash paths would not have matched under the
oracle either — faithful port, not a gap silently papered over).

Scope: `show_top` (ratified per-row in op-classification.tsv —
`git diff --name-only` reads the CALLER's working-tree state, which differs
per linked worktree of the same repo; a common_dir key would answer for the
wrong worktree's uncommitted diff). Registration on the three shared
surfaces (`_EAGER_OP_MODULES`, `_OP_KEY_SCOPE`, `_registry_map.py`) lands in
the EM-serial registration pass per CC-3; this module carries only its own
`register_op`.

Contract: params {phase2_fix_now_path: str} -> {claimed_no_diff: list[str]}
Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B9
Parent plan:   docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § DEC-2

Negative-spec:
    - NO jq, comm, sort, or process-substitution equivalents are spawned —
      the pipeline is fully in-process except the single sanctioned
      `git diff --name-only` spawn.
    - Does NOT block or gate on a non-empty result — reporting is the
      caller's job (the fence explicitly does not block commit on a
      non-empty MISSING set); this op computes and returns, nothing more.
    - Does NOT run `git diff` against HEAD/--cached variants — the oracle
      read the UNSTAGED working-tree diff and so does this port.
"""

from __future__ import annotations

import json
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op


class FixManifestError(RuntimeError):
    """Structured failure for bug_sweep.verify_fix_files_changed (CC-7).

    Raised when the phase2-fix-now manifest is missing, unparseable, or
    shape-invalid (the manifest IS the premise — settlement B9), or when the
    `git diff` probe itself fails (not-a-repo / git absent).
    """


def _load_expected_files(manifest_path: Path) -> set:
    """Parse the phase2-fix-now.json manifest into the expected-fixed set.

    Shape contract (fence `jq -r '.[].file'`): JSON array of objects, each
    with a string `file` key. Any deviation raises FixManifestError naming
    the manifest path and the offending entry index.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: cannot read fix-now manifest "
            f"{str(manifest_path)!r}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: fix-now manifest "
            f"{str(manifest_path)!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: fix-now manifest "
            f"{str(manifest_path)!r} must be a JSON array of "
            f"{{'file': <path>}} objects, got {type(data).__name__}"
        )
    expected = set()
    for index, entry in enumerate(data):
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise FixManifestError(
                "bug_sweep.verify_fix_files_changed: fix-now manifest "
                f"{str(manifest_path)!r} entry {index} lacks a string 'file' "
                "key — the manifest is the premise; refusing to guess"
            )
        expected.add(entry["file"])
    return expected


def _changed_files(repo_root: Path) -> set:
    """Windows-safe `git diff --name-only` → the changed-files set (CC-1)."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(repo_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )  # popup-safe-env-suppressed
    except OSError as exc:
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: failed to spawn git in "
            f"{str(repo_root)!r}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: `git diff --name-only` exited "
            f"{proc.returncode} in {str(repo_root)!r}: "
            f"{(proc.stderr or '').strip()}"
        )
    return {line for line in proc.stdout.splitlines() if line.strip()}


def verify_fix_files_changed(
    phase2_fix_now_path: str,
    repo_root: Optional[Path] = None,
) -> dict:
    """Report fix-now-claimed files with zero working-tree diff (settlement B9).

    phase2_fix_now_path: the bug-sweep run's phase2-fix-now.json manifest.
    repo_root:           worktree to diff; None → the process CWD (matching
                         the fence, which ran `git diff` from the repo root).

    Returns {claimed_no_diff: sorted list} per the manifest contract.
    Raises FixManifestError on a malformed/missing manifest or a failed git
    probe (CC-7 fail-loud).
    """
    expected = _load_expected_files(Path(phase2_fix_now_path))
    changed = _changed_files(repo_root if repo_root is not None else Path.cwd())
    return {"claimed_no_diff": sorted(expected - changed)}


@register_op("bug_sweep.verify_fix_files_changed")
def _verify_fix_files_changed(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'bug_sweep.verify_fix_files_changed' handler — sync.

    Params: phase2_fix_now_path (str, required). repo_root is the
    ipc-injected per-request worktree top (scope show_top) — the diff must
    answer for the CALLER's worktree; None (direct/unkeyed invocation) falls
    back to the process CWD, the fence's own behavior.
    """
    manifest_path = params.get("phase2_fix_now_path") or ""
    if not manifest_path:
        raise FixManifestError(
            "bug_sweep.verify_fix_files_changed: required param "
            "'phase2_fix_now_path' is missing or empty"
        )
    return verify_fix_files_changed(manifest_path, repo_root=repo_root)
