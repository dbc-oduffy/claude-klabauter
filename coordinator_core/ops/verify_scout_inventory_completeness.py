"""
coordinator_core.ops.verify_scout_inventory_completeness — JSON-RPC
"research.verify_scout_inventory_completeness" operation.

Purpose: disk-first anti-hallucination gate for deep-research scout
handoffs (fence: `pipelines/deep-research/repo-driver.md:341`). A scout
reports back a list of inventory files it claims to have written; this op
is the load-bearing check that those claims are true on disk BEFORE any
downstream specialist is unblocked to consume them — see
`coordinator/CLAUDE.md` § Scouts and Disk-First Verification. The bash
oracle was a for-loop over `[ ! -f "$f" ]` plus `wc -l "$f"`; this port
replaces both with `pathlib.Path.exists()` and an in-process newline count
over each file's decoded text — zero subprocess calls, so the Windows
POSIX-shell dependency the oracle carried does not exist here at all.

Idempotency (AC7, DEC-7 note): INHERENT — pure read (existence check +
line count over each expected file's current bytes), zero writes, zero
state accretion. Two back-to-back calls against an unchanged tree return
byte-identical results; the double-invocation test asserts this.

Platform-hazard (AC8, DEC-7 note — distinct-ops-new.tsv rates this `high`
for the bash-oracle shape): the oracle's `wc -l` + `[ ! -f ]` combination
has no native cmd.exe/PowerShell equivalent and depends on a POSIX shell
being present at all (the exact class of hazard CLAUDE.md § Runtime
conventions calls windows-hostile and break-class). The port closes it by
construction rather than by branching: `Path.exists()` and
`str.splitlines()` over `Path.read_text()` are the same code path on every
platform Python runs on — there is no OS-specific branch to get wrong,
so the "high" rating on the shell-oracle side does not carry over to this
module at all.

Scope: `common_dir` (per op-classification.tsv's scope-verdict column) —
expected scout inventory files live under the CALLER's main-worktree-rooted
tasks/**/scratch/ tree (per this repo's own CLAUDE.md § state/ vs tasks/
split), shared across any linked worktree of the same repo. The engine
delivers the git common dir as `repo_root`; the worktree root is derived
via `coordinator_core.ops.fleet._common.main_worktree_root`, never via
`params.repo_root` directly — a cross-repo/cross-worktree caller supplying
only relative paths must resolve against ITS OWN worktree, not claude-klabauter's.
Registration on the three shared surfaces (`_EAGER_OP_MODULES`,
`_OP_KEY_SCOPE`, `_registry_map.py`) lands in the EM-serial registration
pass; this module carries only its own `register_op` call.

Contract: params {expected_files: list[str], min_lines: int=30} ->
          {complete: bool, missing: list[str], short: list[str]}
Spec backlink: pipelines/deep-research/repo-driver.md:341
Parent plan:   docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 2

Negative-spec:
    - Does NOT spawn `wc`, `find`, or any subprocess — the whole check is
      in-process pathlib + text-decode, no bash/coreutils dependency.
    - Does NOT distinguish "missing" from "not a regular file" (a directory
      or broken symlink at the expected path) — both land in `missing`,
      since a scout claiming a file exists when a directory sits there is
      the same false-claim the gate exists to catch.
    - Does NOT mutate anything — read-only gate, reports and returns.
    - Does NOT treat an unreadable/undecodable file as `missing` silently
      papering over a real I/O problem — `errors="replace"` decodes best-
      effort (matching the oracle's `wc -l`, which counts bytes/lines
      regardless of encoding validity) rather than raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root

_DEFAULT_MIN_LINES = 30


class ScoutInventoryParamsError(ValueError):
    """Structured failure for malformed research.verify_scout_inventory_completeness params.

    Raised when `expected_files` is not a list of strings, or `min_lines` is
    not an int — the params ARE the premise; this op refuses to guess a
    shape rather than silently coercing.
    """


def _line_count(path: Path) -> int:
    """Count lines in *path* by reading its full decoded text.

    Native replacement for the oracle's `wc -l <path>`: decodes with
    errors="replace" (best-effort, matching wc's byte-oriented tolerance
    of non-UTF8 content) and counts entries in `str.splitlines()`, which —
    unlike `wc -l`'s newline-terminator count — also counts a final
    unterminated line. That is the correct choice for THIS gate (it cares
    about "how much content is here", not byte-exact `wc` parity).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return len(text.splitlines())


def verify_scout_inventory_completeness(
    expected_files: List[str],
    min_lines: int = _DEFAULT_MIN_LINES,
    worktree: Optional[Path] = None,
) -> dict:
    """Verify each expected scout-inventory file exists and clears min_lines.

    expected_files: repo-relative (resolved against *worktree*) or absolute
                     paths the scout claims to have written.
    min_lines:       minimum line count a file must clear to count as
                      complete (default 30, matching the oracle fence).
    worktree:        root to resolve relative entries against; None →
                      the process CWD (direct/unkeyed invocation fallback).

    Returns {complete, missing, short} — `missing` names entries that do
    not exist (or are not a regular file) on disk; `short` names entries
    that exist but read fewer than `min_lines` lines. `complete` is True
    iff both lists are empty. Entries are reported verbatim as given by
    the caller, not resolved/normalized, matching the oracle's own
    behavior (it echoed back the caller's `$f` on failure).
    """
    root = worktree if worktree is not None else Path.cwd()
    missing: List[str] = []
    short: List[str] = []
    for raw in expected_files:
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else root / candidate
        if not resolved.is_file():
            missing.append(raw)
            continue
        if _line_count(resolved) < min_lines:
            short.append(raw)
    return {"complete": not missing and not short, "missing": missing, "short": short}


@register_op("research.verify_scout_inventory_completeness")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'research.verify_scout_inventory_completeness' handler — sync.

    Params: expected_files (list[str], required), min_lines (int, optional,
    default 30). repo_root arg: the git common dir delivered by
    `_OP_KEY_SCOPE="common_dir"` — worktree root is derived via
    `main_worktree_root(repo_root)`, never from params directly; None
    (direct/unkeyed invocation) falls back to the process CWD.
    """
    expected_files = params.get("expected_files")
    if not isinstance(expected_files, list) or not all(
        isinstance(entry, str) for entry in expected_files
    ):
        raise ScoutInventoryParamsError(
            "research.verify_scout_inventory_completeness: required param "
            "'expected_files' must be a list of strings, got "
            f"{type(expected_files).__name__ if expected_files is not None else 'missing'}"
        )

    min_lines = params.get("min_lines", _DEFAULT_MIN_LINES)
    if isinstance(min_lines, bool) or not isinstance(min_lines, int):
        raise ScoutInventoryParamsError(
            "research.verify_scout_inventory_completeness: 'min_lines' must "
            f"be an int, got {type(min_lines).__name__}"
        )

    param_repo_root = params.get("repo_root")
    if repo_root is None:
        worktree = Path.cwd()
    else:
        common_dir = Path(repo_root)
        mismatch = check_repo_root(param_repo_root, common_dir)
        if mismatch:
            raise ScoutInventoryParamsError(
                f"research.verify_scout_inventory_completeness: {mismatch}"
            )
        worktree = main_worktree_root(common_dir)

    return verify_scout_inventory_completeness(expected_files, min_lines, worktree)
