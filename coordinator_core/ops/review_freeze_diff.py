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
from typing import Dict, List, Optional, Tuple

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

    A THIN CASE OF `freeze_diffs_batch` (one-element `requests` list) — see that
    function's docstring for the shared algorithm and the batch/single-path
    parity guarantee this delegation exists to hold. Both the `review.freeze_diff`
    JSON-RPC handler below and `coordinator/bin/freeze-review-diff.py`'s CLI call
    this function; neither re-derives the git-diff-and-write sequence, and this
    function itself no longer re-derives it either.

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

    Negative-spec: does NOT overwrite an existing frozen diff whose content
    differs. A slice id is a filename, so a generic one (`ceremony-artifacts`,
    `slice-1`) collides with whatever peer froze it first — and on a shared
    worktree that peer's file is often tracked, so an unconditional write
    destroys their frozen evidence with nothing erroring and nothing logged.
    A colliding freeze is a structured error naming both the id and the path;
    a re-freeze producing byte-identical content is idempotent and allowed.

    Returns:
        On success: {"diff_path": str, "head_sha_path": str, "head_sha": str,
                     "empty": bool, "error": None}
        On failure: {"diff_path": None, "head_sha_path": None, "head_sha": None,
                     "empty": None, "error": str}
    """
    return freeze_diffs_batch(
        repo_root, [{"slice_id": slice_id, "range": range_, "paths": paths}]
    )[0]


def _range_shape(range_: str) -> Optional[int]:
    """Number of `git rev-parse` output lines a range of this shape produces:
    3 for a three-dot symmetric range (`A...B` -> `B`, `A`, `^mergebase`), 2 for
    a two-dot range (`A..B` -> `B`, `^A`), `None` when `range_` carries neither
    separator — a bare single ref, which `freeze_diffs_batch` does not support
    (see that function's docstring). Checked by substring, not regex: `..`/`...`
    inside a ref name is not a real-world shape this module's callers produce
    (matches `_zero_commit_range_error`'s own `sep` detection)."""
    if "..." in range_:
        return 3
    if ".." in range_:
        return 2
    return None


def freeze_diffs_batch(
    repo_root: Path,
    requests: List[dict],
) -> List[dict]:
    """Freeze many `(slice_id, range[, paths])` requests to the same
    `state/review-trail/diffs/<slice_id>.{diff,head.sha}` shape `freeze_diff`
    writes, in a bounded number of git spawns — ONE `git rev-parse` (resolving
    every request's diff endpoints AND the batch's shared freeze-time HEAD in
    one call) plus, only when at least one request survives structural
    validation, ONE `git diff-tree --stdin -p` (producing every surviving
    request's diff in one call) — regardless of how many requests are given.
    `freeze_diff` is a thin one-element case of this function (see its own
    docstring); the two cannot drift because there is only one algorithm.

    Each request is a dict: `{"slice_id": str REQUIRED, "range": str REQUIRED,
    "paths": Optional[List[str]]}`. Returns a list of result dicts, one per
    request, SAME ORDER, SAME SHAPE as `freeze_diff`'s return value (a
    structured `_error(...)` dict on that request's own failure — never an
    exception for a per-request precondition, matching `freeze_diff`'s own
    fail-soft-per-call contract).

    Every request must share the SAME `paths` restriction (or all omit it) —
    `git diff-tree --stdin` applies one pathspec to the whole stdin batch, not
    per line; a batch mixing different `paths` values across requests raises
    `ValueError` (a caller-composition bug, not a per-request data problem,
    so it is not folded into any one request's `_error(...)` result).

    Range shape: EITHER a two-dot (`A..B`) OR three-dot (`A...B`) range (see
    `_range_shape`) — the two shapes this module's own negative-spec names as
    examples, and the only shapes any existing caller/test passes. A bare
    single ref with neither separator is NOT batchable: unlike a commit-vs-
    commit freeze, `git diff <ref>` diffs that ref against the *working tree*,
    a different operation `git diff-tree` cannot express — that request's
    result is a structured `_error(...)`, not a raised exception, since it is
    a per-request data shape rather than a caller-composition bug.

    Diff endpoints: for a two-dot range, `git rev-parse "A..B"` prints `B`
    (unprefixed, the diff's "new" side) then `^A` (prefixed, the "old" side)
    — the pair fed to `diff-tree` is `(A, B)`. For a three-dot range,
    `git rev-parse "A...B"` prints `B`, then `A`, then `^mergebase(A,B)` — the
    pair fed to `diff-tree` is `(mergebase, B)`, matching `git diff A...B`'s
    own documented translation to `git diff $(git merge-base A B) B`. Measured
    live against this repo's own history (both shapes) before landing this
    function — see the op's own tests for the fixture proving byte-identical
    output against the single-range path.

    Zero-commit refusal (mirrors `_zero_commit_range_error`, single-spawn
    simplification): a request refuses iff its two RESOLVED diff endpoints
    are the identical commit — the exact shape the caret-eating incident this
    check guards against produces (see this module's own negative-spec),
    decided from the same `rev-parse` call with no extra spawn. This is a
    NARROWER refusal than the single-path original's `git rev-list --count`-
    based check, which also refuses a two-dot range whose head is a strict
    ancestor of its base even when the two endpoints are NOT identical — that
    narrower case now freezes an (empty) diff here instead of refusing it.
    Every existing caller/test range is either non-empty or the identical-
    endpoint case, so this narrowing is not observed today; documented rather
    than silently accepted, per this module's fail-loud-never-silent posture.
    """
    if not requests:
        return []

    normalized: List[dict] = [
        {
            "slice_id": req.get("slice_id") or "",
            "range": req.get("range") or "",
            "paths": req.get("paths") or None,
        }
        for req in requests
    ]

    paths_values = {
        tuple(req["paths"]) if req["paths"] else None for req in normalized
    }
    if len(paths_values) > 1:
        raise ValueError(
            "freeze_diffs_batch: requests carry different 'paths' pathspecs — "
            "git diff-tree --stdin applies one pathspec to the whole batch, "
            "never per request; split into separate batch calls per distinct "
            "pathspec instead"
        )
    shared_paths = next(iter(paths_values)) if paths_values else None
    shared_paths_list = list(shared_paths) if shared_paths else None

    results: List[Optional[dict]] = [None] * len(normalized)
    shapes: Dict[int, int] = {}
    for i, req in enumerate(normalized):
        if not req["range"]:
            results[i] = _error(
                "range is required and is never defaulted — the caller owns range "
                "resolution (e.g. a session-id-scoped range); pass it explicitly."
            )
            continue
        slice_err = _validate_slice_id(req["slice_id"])
        if slice_err is not None:
            results[i] = _error(slice_err)
            continue
        shape = _range_shape(req["range"])
        if shape is None:
            results[i] = _error(
                f"range {req['range']!r} carries neither '..' nor '...' — a bare "
                "single ref is not batchable (it diffs against the working tree, "
                "not another commit, which git diff-tree cannot express)"
            )
            continue
        shapes[i] = shape

    pending_idx = [i for i in range(len(normalized)) if results[i] is None]
    if not pending_idx:
        return results  # type: ignore[return-value]

    rev_parse_args = [normalized[i]["range"] for i in pending_idx] + ["HEAD"]
    rp = _git(["rev-parse", *rev_parse_args], cwd=repo_root)
    if not rp.ok:
        raise ValueError(
            f"git rev-parse failed while resolving freeze batch endpoints: "
            f"{rp.stderr.strip()}"
        )
    lines = [ln.strip() for ln in rp.stdout.splitlines() if ln.strip()]

    cursor = 0
    endpoints: Dict[int, Tuple[str, str]] = {}
    for i in pending_idx:
        shape = shapes[i]
        chunk = lines[cursor : cursor + shape]
        cursor += shape
        if len(chunk) != shape:
            raise ValueError(
                "git rev-parse produced fewer lines than expected while resolving "
                "the freeze batch — endpoint resolution and HEAD sha are out of sync"
            )
        if shape == 2:
            b_sha, a_sha = chunk[0], chunk[1].lstrip("^")
        else:
            b_sha, a_sha = chunk[0], chunk[2].lstrip("^")
        endpoints[i] = (a_sha, b_sha)
    if cursor >= len(lines):
        raise ValueError(
            "git rev-parse produced no HEAD line for the freeze batch — endpoint "
            "resolution and HEAD sha are out of sync"
        )
    head_sha = lines[cursor]

    zero_commit_idx = {i for i, (a_sha, b_sha) in endpoints.items() if a_sha == b_sha}
    diff_pending_idx = [i for i in pending_idx if i not in zero_commit_idx]

    per_pair_diff: Dict[int, str] = {}
    if diff_pending_idx:
        stdin_payload = "".join(f"{endpoints[i][0]} {endpoints[i][1]}\n" for i in diff_pending_idx)
        diff_argv = ["diff-tree", "--stdin", "-p"]
        if shared_paths_list:
            diff_argv += ["--", *shared_paths_list]
        dt = _git(diff_argv, cwd=repo_root, input_data=stdin_payload)
        if not dt.ok:
            raise ValueError(
                f"git diff-tree --stdin failed for the freeze batch: {dt.stderr.strip()}"
            )
        diff_texts = _split_diff_tree_stdin_output(
            dt.stdout, [endpoints[i][0] for i in diff_pending_idx]
        )
        per_pair_diff = dict(zip(diff_pending_idx, diff_texts))

    diffs_dir = repo_root / "state" / "review-trail" / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    for i in pending_idx:
        slice_id = normalized[i]["slice_id"]
        range_ = normalized[i]["range"]
        if i in zero_commit_idx:
            results[i] = _error(
                f"range {range_!r} resolves to ZERO commits — refusing to freeze a "
                "diff for a range that names no commits. This is the exact shape a "
                "caret-eating shell/shim produces from a legitimate per-commit "
                "'<sha>^..<sha>' request (e.g. a Windows .cmd forwarder collapsing "
                "it to '<sha>..<sha>'). Verify the range was constructed correctly."
            )
            continue

        diff_text = per_pair_diff[i]
        diff_path = diffs_dir / f"{slice_id}.diff"
        sha_path = diffs_dir / f"{slice_id}.head.sha"

        if diff_path.exists() and diff_path.read_text(encoding="utf-8") != diff_text:
            results[i] = _error(
                f"slice_id '{slice_id}' already names a frozen diff at {diff_path} with "
                "different content — a slice id is a filename, and a generic one collides "
                "with whatever peer froze it first. Re-freeze under a slice id that names "
                "this range."
            )
            continue

        diff_path.write_text(diff_text, encoding="utf-8", newline="\n")
        sha_path.write_text(head_sha + "\n", encoding="utf-8", newline="\n")
        declare_write(diff_path)
        declare_write(sha_path)

        results[i] = {
            "diff_path": str(diff_path),
            "head_sha_path": str(sha_path),
            "head_sha": head_sha,
            "empty": not diff_text.strip(),
            "error": None,
        }

    return results  # type: ignore[return-value]


def _split_diff_tree_stdin_output(stdout: str, a_shas: List[str]) -> List[str]:
    """Split `git diff-tree --stdin -p`'s combined output back into one diff
    per pair, returned in the same order as `a_shas` (the order the pairs
    were fed on stdin). `--stdin` fed two explicit tree-ish per line prints a
    PAIR HEADER line — the first (old/base) tree-ish's own sha, alone on its
    own line — immediately before that pair's diff, with no blank-line
    separator between one pair's last diff line and the next pair's header
    (measured live against this repo's own history before landing this
    function). The header line itself is stripped — `git diff <range>` never
    prints one, and this function's whole purpose is byte-identical parity
    with that output.

    Matches on the KNOWN `a_shas` value for each pair, in order, rather than
    "any bare 40-hex-char line" — a diff body line is vanishingly unlikely to
    collide with a caller-supplied sha, but matching the caller's own known
    values is exact where a generic hex-line pattern would only be probable.
    """
    lines = stdout.splitlines(keepends=True)
    segments: List[List[str]] = [[] for _ in a_shas]
    current = -1
    for line in lines:
        stripped = line.rstrip("\n")
        if current + 1 < len(a_shas) and stripped == a_shas[current + 1]:
            current += 1
            continue
        if current >= 0:
            segments[current].append(line)
    return ["".join(chunk) for chunk in segments]


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
