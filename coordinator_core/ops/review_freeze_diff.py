"""
coordinator_core.ops.review_freeze_diff — freeze a caller-supplied range-diff
plus the freeze-time HEAD sha to
``state/review-trail/diffs/<slice-id>.{diff,head.sha}``, by name.

Purpose: registers ``review.freeze_diff``. coordinator:code-reviewer's Bash is
allowlist-confined by ``coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist``
(fail-closed, no escape hatch, correctly so) — it cannot run `git diff` itself.
Five non-weekly DoE-claude review-dispatch gates each grew a hand-written
``git diff <range> -- <paths> > state/review-trail/diffs/<slice-id>.diff``
fenced shell block to work around that — a command payload an EM reads out of
a markdown fence and retypes into a shell (unlintable, untestable, invisible
to the coverage gate, because a fence is not a file — PM ruling, 2026-07-22).
This op is the named entrypoint those five fences collapse into.

Same contract and on-disk shape as the pre-existing standalone CLI
``coordinator/bin/freeze-review-diff.py`` (landed 2026-07-23, commits
`2a592819`/`eebb9b48`, before this op existed) — that CLI now delegates its
git-diff-and-write algorithm to :func:`freeze_diff` below instead of running
its own subprocess calls, so the composing algorithm exists exactly once.
The CLI keeps its own argv parsing / exit-code contract (a JSON-RPC op has no
argv to parse); this module owns everything past "range + slice_id + paths
have been extracted from the caller".

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-review-diff-freeze-op-wanted.md
Prior pattern: coordinator/skills/parallel-code-review/SKILL.md (DoE-claude) — the
existing frozen-diff + head.sha shape this op generalizes to the other five
non-weekly review-dispatch gates.
Sibling op: coordinator_core.ops.ceremony.snapshot_diff_and_head
(``review.snapshot_diff_and_head``) — SHA-pair-named snapshot directory,
DEFAULTED base/head refs, built for the weekly ceremony's own idempotency
shape. This op is deliberately NOT a variant of that one: its range is
NEVER defaulted (see negative-spec) and its output is named by caller-chosen
``slice_id`` under a flat ``diffs/`` directory, matching the five gates'
pre-existing fence shape byte-for-byte rather than introducing a new one.

Negative-spec (hard-won — do NOT reintroduce):
    - Does NOT default ``range`` — not to ``origin/main...HEAD``, not to
      anything else. ``/workstream-complete`` resolves a *session-scoped*
      range (matching the ``Session-Id:`` git trailer) specifically so a
      shared ``work/*`` branch's concurrent-session commits are not swept
      into a review — the 2026-06-15 multi-EM-brightline-noise failure. A
      defaulted range here would silently reintroduce that trap at every
      caller. The caller owns range resolution; this op only owns freezing it.
    - Does NOT resolve or validate ``range`` beyond passing it to ``git diff``
      verbatim.
    - Does NOT shell to bash/sh — git only, via the Windows-safe
      ``ops.ceremony.git_native._git`` helper (CREATE_NO_WINDOW +
      stdin=DEVNULL; see that module's docstring).
    - Does NOT delete or rotate a prior freeze under the same slice_id — a
      second freeze under the same id overwrites the prior pair (same
      last-write-wins posture as ``review_trail.write``).
    - An empty diff (e.g. a range with no net change under ``paths``) is a
      VALID outcome, not an error: both files are still written, and the
      returned envelope carries ``"empty": true`` for the caller to note —
      never a die-silent-on-zero-match gate.
    - Does NOT treat a zero-net-change diff over a >= 1-commit range as an
      error (see negative-spec entry above) — that stays a valid ``empty:
      true`` outcome. The ONLY refusal this op adds is a diff-shaped
      ``range_`` (contains ``..``/``...``) that resolves to ZERO COMMITS via
      ``git rev-list --count`` — a range mangled en route (e.g. a Windows
      `.cmd` forwarder eating the caret in `<sha>^..<sha>`) collapsing to
      `<sha>..<sha>`. That refusal fires BEFORE either output file is
      written, mirroring ``review_trail_write._reject_empty_sha_range``
      (same discriminator, same ``git rev-list --count`` check) — see that
      function's docstring for the fuller incident history. Not copy-pasted
      into a second home: this op owns its own check because
      ``review_trail_write.py`` is a heavily peer-trafficked file whose own
      call sites this fix does not touch.
"""


from __future__ import annotations

MUTATES = ["state/review-trail/diffs/*.diff", "state/review-trail/diffs/*.head.sha"]  # slice_id-keyed, data-dependent set

from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git
from coordinator_core.session.declared_writes import declare_write


def _validate_slice_id(slice_id: str) -> Optional[str]:
    """Return an error message if slice_id is not a bare filename component,
    else None. A slice-id is a filename component, not a path — reject any
    path separator or a `..` traversal segment (mirrors freeze-review-diff.py's
    original validation, now the single copy both the op and the CLI share)."""
    if not slice_id:
        return "slice_id is required"
    if "/" in slice_id or "\\" in slice_id:
        return f"slice_id must not contain a path separator: {slice_id!r}"
    if ".." in slice_id:
        return f"slice_id must not contain '..': {slice_id!r}"
    return None


def _error(message: str) -> dict:
    """Structured-error envelope: contract fields present, values None, plus "error"."""
    return {
        "diff_path": None,
        "head_sha_path": None,
        "head_sha": None,
        "empty": None,
        "error": message,
    }


def _zero_commit_range_error(range_: str, repo_root: Path) -> Optional[str]:
    """Return an error message iff `range_` is diff-shaped (contains ``..``
    or ``...``) and resolves to ZERO commits via ``git rev-list --count`` in
    `repo_root`, else None (not diff-shaped, resolves to >= 1 commit, or
    `git rev-list` itself fails to run — a resolution failure is a different
    problem than "zero commits" and is left to `git diff` itself to surface
    normally, same as `review_trail_write._reject_empty_sha_range`'s sibling
    check treats an unresolvable range as its own distinct failure).

    Mirrors `review_trail_write._reject_empty_sha_range`'s discriminator
    (same `..`/`...` diff-shape test, same `git rev-list --count` check) —
    see that function's docstring for the fuller incident history this
    guards against. Not shared code: that module is heavily peer-trafficked
    and its own call sites are out of scope for this fix.
    """
    sep = "..." if "..." in range_ else (".." if ".." in range_ else None)
    if sep is None:
        return None
    result = _git(["rev-list", "--count", range_], cwd=repo_root)
    if not result.ok:
        return None
    try:
        count = int(result.stdout.strip())
    except ValueError:
        return None
    if count != 0:
        return None
    return (
        f"range {range_!r} resolves to ZERO commits — refusing to freeze a "
        "diff for a range that names no commits. This is the exact shape a "
        "caret-eating shell/shim produces from a legitimate per-commit "
        "'<sha>^..<sha>' request (e.g. a Windows .cmd forwarder collapsing "
        "it to '<sha>..<sha>'). Verify the range was constructed correctly."
    )


def freeze_diff(
    repo_root: Path,
    range_: str,
    slice_id: str,
    paths: Optional[List[str]] = None,
) -> dict:
    """Core algorithm: freeze `range_`'s diff (optionally restricted to `paths`)
    plus the freeze-time HEAD sha to
    `<repo_root>/state/review-trail/diffs/<slice_id>.{diff,head.sha}`.

    The ONE implementation of this write — both the `review.freeze_diff`
    JSON-RPC handler below and `coordinator/bin/freeze-review-diff.py`'s CLI
    call this function; neither re-derives the git-diff-and-write sequence.

    Params:
        repo_root — the git worktree root the freeze runs against.
        range_    — caller-supplied git diff range (e.g. "abc123..def456" or
                    "origin/main...HEAD"). REQUIRED; never defaulted (see
                    module negative-spec) — an empty string is a structured
                    error, not a fallback trigger.
        slice_id  — filename component used to name the two output files.
                    REQUIRED; rejected if it contains a path separator or
                    '..' (see `_validate_slice_id`).
        paths     — optional pathspec list restricting the diff; None/empty
                    means "no restriction" (matches the CLI's `--paths` with
                    zero values behaving identically to omitting the flag).

    Returns:
        On success: {"diff_path": str, "head_sha_path": str, "head_sha": str,
                     "empty": bool, "error": None}
        On failure: {"diff_path": None, "head_sha_path": None, "head_sha": None,
                     "empty": None, "error": str}
    """
    if not range_:
        return _error(
            "range is required and is never defaulted — the caller owns range "
            "resolution (e.g. a session-id-scoped range); pass it explicitly."
        )

    slice_err = _validate_slice_id(slice_id)
    if slice_err is not None:
        return _error(slice_err)

    zero_commit_err = _zero_commit_range_error(range_, repo_root)
    if zero_commit_err is not None:
        return _error(zero_commit_err)

    head_result = _git(["rev-parse", "HEAD"], cwd=repo_root)
    if not head_result.ok or not head_result.stdout.strip():
        return _error(f"cannot resolve HEAD sha: {head_result.stderr.strip()}")
    head_sha = head_result.stdout.strip()

    diff_args = ["diff", range_]
    if paths:
        diff_args += ["--", *paths]
    diff_result = _git(diff_args, cwd=repo_root)
    if not diff_result.ok:
        return _error(f"git diff {range_} failed:\n{diff_result.stderr}")

    diffs_dir = repo_root / "state" / "review-trail" / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    diff_path = diffs_dir / f"{slice_id}.diff"
    sha_path = diffs_dir / f"{slice_id}.head.sha"

    diff_path.write_text(diff_result.stdout, encoding="utf-8")
    sha_path.write_text(head_sha + "\n", encoding="utf-8")
    declare_write(diff_path)
    declare_write(sha_path)

    return {
        "diff_path": str(diff_path),
        "head_sha_path": str(sha_path),
        "head_sha": head_sha,
        "empty": not diff_result.stdout.strip(),
        "error": None,
    }


@register_op("review.freeze_diff")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.freeze_diff" handler — read-mostly, sync.

    Sync (not async): the only I/O is subprocess.run (via `git_native._git`)
    plus two plain file writes; ipc.py offloads sync handlers via
    `asyncio.to_thread`.

    Params:
        range    (str, REQUIRED) — see `freeze_diff`'s `range_` — never
                                    defaulted; an absent/empty value is a
                                    structured error, not a silent fallback.
        slice_id (str, required) — see `freeze_diff`.
        paths    (list[str], optional) — see `freeze_diff`.

    Returns: see `freeze_diff`'s Returns section.

    Keying scope: show_top — `git diff`/`rev-parse` read the CALLER's checked
    out worktree tip, which differs per linked worktree (same reasoning as
    the sibling `review.snapshot_diff_and_head` op).
    """
    if repo_root is None:
        return _error(
            "review.freeze_diff requires a show_top-keyed dispatch; "
            "repo_root (worktree top-level) was not supplied"
        )

    range_raw = params.get("range")
    range_ = range_raw if isinstance(range_raw, str) else ""

    slice_id_raw = params.get("slice_id")
    slice_id = slice_id_raw if isinstance(slice_id_raw, str) else ""

    paths_raw = params.get("paths")
    if paths_raw is not None and not isinstance(paths_raw, list):
        return _error("params.paths must be a list of strings when provided")
    paths = paths_raw or None

    return freeze_diff(repo_root, range_, slice_id, paths)
