"""
coordinator_core.ops.ceremony.snapshot_diff_and_head — read-only git snapshot
(range-diff + HEAD SHA) for ceremony use.

Purpose: registers `review.snapshot_diff_and_head`, the named-op replacement
for the `parallel-code-review` fence that freezes "the diff at the merge
boundary" for the synthesizer's later head-drift comparison
(`coordinator/skills/parallel-code-review/SKILL.md:155`):
    TS=date; mkdir $FINDINGS_DIR
    git diff origin/main...HEAD > diff.patch
    git rev-parse HEAD > head.sha

Read-only: this op never mutates the caller's git state (no add/commit/
stash/push/reset/checkout) — it only reads (`git diff`, `git rev-parse`) and
writes two plain files under a snapshot directory of its own naming.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 2 (op-classification.tsv row `snapshot-diff-and-head`).

DEC-7 idempotency note (platform-hazard rated `medium` in the manifest —
the original fence's `TS=$(date -u +%Y%m%dT%H%M%SZ)` wall-clock stamp is
POSIX-shell-only and, more fundamentally, makes a second identical
invocation mint a NEW directory rather than a safe no-op, failing AC7 as
transliterated). This op closes both problems the same way: the snapshot
directory is named from the resolved `base_ref`/`head_ref` commit SHAs
(`<base_sha[:12]>-<head_sha[:12]>`) instead of wall-clock time. Two
back-to-back calls with identical params against an unchanged repo resolve
to the same SHAs, hence the same directory — no timestamp, no shell `date`,
no GNU/BSD dialect split, and (AC7) a second call is a genuine no-op: if the
directory already holds a `head.sha` matching the freshly-resolved
`head_sha`, this op treats the existing `diff.patch` as already correct and
skips recomputing/rewriting it, rather than merely asserting idempotence.

Negative-spec:
  - Does NOT shell to bash/sh/date/coreutils — timestamps and diff/rev-parse
    both come from Python + `git_native._git` (Windows-safe subprocess flags:
    CREATE_NO_WINDOW, stdin=DEVNULL — see git_native module docstring).
  - Does NOT run any mutating git subcommand.
  - Does NOT default `findings_dir` — the caller names its own snapshot root;
    this op only chooses the sub-directory name under it.
"""

from __future__ import annotations

# Generator-provenance declaration: this op writes diff.patch/head.sha under
# a caller-supplied findings_dir ("this op only chooses the sub-directory
# name under it" -- module docstring negative-spec), never a path this
# module defaults into the tracked repo tree.
GENERATES = []

from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git

#: Number of leading hex characters of each resolved SHA used to build the
#: snapshot directory name — long enough to be practically collision-free
#: for a single repo's diff-snapshot use case, short enough to stay a
#: readable directory name.
_SHA_PREFIX_LEN = 12


def _error(message: str, **extra: object) -> dict:
    """Structured-error envelope: contract fields present, values None, plus "error"."""
    result: dict = {"ts_dir": None, "diff_path": None, "head_sha": None, "error": message}
    result.update(extra)
    return result


def _resolve_sha(repo_root: Path, ref: str) -> tuple[Optional[str], Optional[str]]:
    """`git rev-parse <ref>` → (sha, error_message). Exactly one is None."""
    result = _git(["rev-parse", ref], cwd=repo_root)
    if not result.ok:
        return None, f"git rev-parse {ref!r} failed: {result.stderr.strip() or result.returncode}"
    sha = result.stdout.strip()
    if not sha:
        return None, f"git rev-parse {ref!r} returned empty output"
    return sha, None


@register_op("review.snapshot_diff_and_head")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.snapshot_diff_and_head" handler — read-only, sync.

    Sync (not async): the only I/O is subprocess.run (via `git_native._git`)
    plus plain file writes; ipc.py offloads sync handlers via
    `asyncio.to_thread`.

    Params:
        findings_dir (str, required) — directory this op creates a snapshot
                                        sub-directory under. Caller-owned;
                                        this op never chooses its own root.
        base_ref     (str, optional, default "origin/main") — left side of
                                        the range-diff.
        head_ref     (str, optional, default "HEAD") — right side of the
                                        range-diff, and the ref whose SHA is
                                        captured as `head_sha`.

    Returns:
        {"ts_dir": str, "diff_path": str, "head_sha": str}
        — plus {"error": str} (all three fields None) on any structured
        failure: missing repo_root, missing/invalid findings_dir, or either
        ref failing to resolve.

    Keying scope: show_top — `git diff`/`git rev-parse` read the CALLER's
    checked-out worktree tip, which differs per linked worktree; repo_root
    here is expected to be that per-worktree top-level directory (not a
    shared common_dir), so distinct worktrees never collapse onto the same
    snapshot slot.
    """
    if repo_root is None:
        return _error(
            "review.snapshot_diff_and_head requires a show_top-keyed dispatch; "
            "repo_root (worktree top-level) was not supplied"
        )

    findings_dir_raw = params.get("findings_dir")
    if not isinstance(findings_dir_raw, str) or not findings_dir_raw.strip():
        return _error("params.findings_dir is required and must be a non-empty string")

    base_ref = params.get("base_ref") or "origin/main"
    head_ref = params.get("head_ref") or "HEAD"
    if not isinstance(base_ref, str) or not isinstance(head_ref, str):
        return _error("params.base_ref and params.head_ref must be strings")

    findings_dir = normalize_native_path(findings_dir_raw)

    head_sha, head_err = _resolve_sha(repo_root, head_ref)
    if head_err is not None:
        return _error(head_err)
    base_sha, base_err = _resolve_sha(repo_root, base_ref)
    if base_err is not None:
        return _error(base_err)

    ts_dir = findings_dir / f"{base_sha[:_SHA_PREFIX_LEN]}-{head_sha[:_SHA_PREFIX_LEN]}"
    diff_path = ts_dir / "diff.patch"
    head_sha_path = ts_dir / "head.sha"

    # Idempotency short-circuit (AC7 / DEC-7, see module docstring): a prior
    # call already froze this exact (base_sha, head_sha) pair — trust it
    # rather than recomputing.
    if head_sha_path.is_file() and diff_path.is_file():
        existing = head_sha_path.read_text(encoding="utf-8").strip()
        if existing == head_sha:
            return {
                "ts_dir": str(ts_dir),
                "diff_path": str(diff_path),
                "head_sha": head_sha,
            }

    diff_result = _git(["diff", f"{base_ref}...{head_ref}"], cwd=repo_root)
    if not diff_result.ok:
        return _error(
            f"git diff {base_ref}...{head_ref} failed: "
            f"{diff_result.stderr.strip() or diff_result.returncode}"
        )

    ts_dir.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff_result.stdout, encoding="utf-8", newline="\n")
    head_sha_path.write_text(head_sha + "\n", encoding="utf-8", newline="\n")

    return {
        "ts_dir": str(ts_dir),
        "diff_path": str(diff_path),
        "head_sha": head_sha,
    }
