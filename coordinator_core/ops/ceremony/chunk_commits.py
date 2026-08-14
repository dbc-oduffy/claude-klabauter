"""
coordinator_core.ops.ceremony.chunk_commits — JSON-RPC "ceremony.chunk_commits" op.

Purpose: answers "did chunk `<id>` of plan `<path>` commit?" as a nameable engine
command, replacing two raw git reads DoE's `execute-plan/SKILL.md` embedded in
skill prose (skills carry no shell — see the sizing memo this plan actions,
`cross-repo/inbox/2026-08-10-doe-claude-em-chunk-commit-liveness-cli-request.md`).

Three wrong forms this op exists to make unreachable (executed evidence,
`docs/plans/2026-08-10-chunk-commit-liveness-becomes-a-nameable.md` § "The
mechanism, executed not assumed", run against
`docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md`,
add-commit `7ff7a493e`):

    | form                                              | result                | verdict        |
    |-----------------------------------------------------------------------------------------|
    | `git log --grep '^C1:' -- <plan-path>`             | empty, exit 0         | false negative |
    | `git log --all --grep '^C1:'`                      | 95 vs 6 anchored      | false positive |
    | `git log --grep '^C1:'` anchored, no subject filter| matches BODY lines    | false positive |

The fourth row is this plan's own finding: `^` in `git --grep` anchors to ANY
line of the commit message, not the subject. `--grep '^Commit-Token:'` matched
`b8b98de77`, whose SUBJECT is `sizing: chunk-commit liveness becomes a
nameable claude-klabauter command` — the hit came off a trailer line in the body. An
implementation that fixes only the range (rows 1-2) still ships row 3's false
positive.

Resolution, three stages, in order:
    1. Resolve the plan's add-commit — composes `git_native.log_diff_filter`
       (AC8), NOT a fresh shell-out. Mirrors the existing pattern in
       `coordinator_core.coverage` (`--follow -M100% --diff-filter=A
       --format=%H`, taking the OLDEST (last) line — git log is newest-first).
       See `coordinator_core.ops.ceremony.branch_resolution` for the sibling
       predicate-pin discussion of why `A` and not `ARC`.
    2. Range-scope to `<add-sha>..HEAD`. The anchor is NOT caller-overridable
       (AC4) — `resolve_chunk_commits` takes no range/anchor override
       parameter at all, so no argument can reach a repo-wide query.
    3. Filter on the commit SUBJECT, never `--grep` (AC2/AC3): the range log
       call requests `--format=%H<sep>%s` and this module does its own
       `str.startswith(f"{chunk_id}:")` test against the `%s` field. Git's own
       `%s` placeholder is defined as the first line of the commit message —
       reading it, rather than piping through `--grep` (which scans every
       line of the FULL message, subject and body alike), makes the
       body-line false positive structurally unreachable rather than merely
       untested.

Fails LOUD (raises) on an unresolvable plan path or a plan with no add-commit
(AC6) — never returns an empty list for either, since an empty list is
reserved for "queried, found nothing" (a chunk that never committed). Every
wrong form in the table above failed at exit 0; this op does not add a fourth.

Return shape (AC5): a `list` of `{"sha": str, "subject": str}` dicts, oldest
first — never a boolean, never a single sha. A chunk id that legitimately
carries multiple commits (this plan's own `C1` resolved to 6 in its anchored
range) returns every one of them.

Scope: "none" in `coordinator_core.op_scopes` (unlisted default — see that
module's own docstring on the unlisted-default contract). This op resolves
its own git repo from the caller-supplied `plan_path` itself (via `git
rev-parse --show-toplevel`, mirroring the `cartography.*` /
`workflow.validate` target-resolution convention: explicit caller-supplied
path, any repo, never the caller's own dispatching tree / engine-injected
`repo_root`) rather than depending on `_origin_worktree` keying.

Spec backlink: pln-chunk-commit-liveness-becomes-c77555 § C1

Negative-spec (hard-won):
  - Does NOT use `git log --grep` anywhere — not even as a prefilter. The
    Problem section's row 4 finding is that `--grep`'s anchor is unsound for
    this op's purpose at ANY stage; reading `%s` directly is both simpler and
    correct, so there is no reason to keep `--grep` in the loop at all.
  - Does NOT accept a pathspec-scoped query form (AC3) — the range log call
    (`resolve_chunk_commits`'s second git invocation) never receives a `--`
    pathspec; only the add-commit resolution (stage 1) is pathspec-scoped,
    and that pathspec is fixed to the plan path used to derive the anchor,
    never caller-widenable.
  - Does NOT accept a caller-supplied range/anchor override (AC4) — no
    parameter of `resolve_chunk_commits` or this op's handler can widen the
    query past `<plan's own add-sha>..HEAD`.
  - Does NOT collapse the return to a boolean (AC5) — the caller is a
    liveness precondition, but collapsing to true/false throws away which
    commits landed, which is the evidence the caller actually needs.
  - Does NOT let empty and error share a code path (AC6) — an unresolvable
    plan path or an unresolvable add-commit raises; only a resolved range
    that matches zero commits returns `[]`.
  - Does NOT shell out to a fresh `git` subprocess for the add-commit
    resolution — composes `git_native.log_diff_filter` (AC8).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Union

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git, log_diff_filter

#: Field separator for the range-log `--format` string. A control byte that
#: cannot appear in a commit subject line (`%s` is single-line by definition —
#: git itself folds an embedded newline out of the subject placeholder), so a
#: naive `str.partition` split is unambiguous; no escaping/quoting needed.
_FIELD_SEP = "\x1f"


def resolve_chunk_commits(
    repo_root: Union[str, Path], plan_path: str, chunk_id: str
) -> List[Dict[str, str]]:
    """Return the chunk-`chunk_id` commits of the plan at `plan_path`, scoped
    to `<plan's own add-commit>..HEAD`.

    Params:
        repo_root: git worktree root `plan_path` is resolved relative to.
        plan_path: repo-relative path (POSIX-separated) to the plan document,
                   exactly as it appears (appeared, if since renamed with
                   `--follow` intact) in git history.
        chunk_id:  the chunk id whose commits are being queried (e.g. `"C1"`).
                   Matched against the START of each candidate commit's
                   subject line as `f"{chunk_id}:"` — never a substring or
                   unanchored match, so `"C1"` cannot accidentally match a
                   `"C10:"` subject.

    Returns:
        `[]` when the plan resolved an add-commit but no commit in
        `<add-sha>..HEAD` carries a subject starting with `f"{chunk_id}:"`
        (AC5 — a chunk that never ran; exit 0, empty is a real answer).
        Otherwise a list of `{"sha": <full sha>, "subject": <subject line>}`,
        oldest first (`git log --reverse`), one entry per matching commit —
        never truncated to one, even when several commits share the chunk id.

    Raises:
        ValueError: `plan_path` does not resolve to an existing file under
            `repo_root`, or the plan has no resolvable add-commit (AC6 — both
            are FAILURES, never a silently-empty list).
        RuntimeError: either underlying `git log` invocation itself failed
            (non-zero exit from git, not "found nothing") — also AC6 territory,
            surfaced distinctly from the ValueError cases above so a caller
            can tell "bad input" from "git itself is unhappy" if it cares to.
    """
    root = Path(repo_root)
    plan_abs = root / plan_path
    if not plan_abs.exists():
        raise ValueError(
            f'ceremony.chunk_commits: plan path "{plan_path}" does not exist '
            f"under {root} — cannot resolve an add-commit for a plan that "
            "isn't there (AC6 fail-loud, never a silent empty list)"
        )

    # Stage 1 — add-commit resolution. Composes log_diff_filter (AC8), never a
    # fresh subprocess call. --follow -M100% --diff-filter=A --format=%H --
    # <plan_path>, mirroring coverage.py's own add-commit resolution exactly
    # (see this module's docstring for the coverage.py backlink).
    add_result = log_diff_filter(
        root,
        "A",
        extra_args=["--follow", "-M100%", "--format=%H", "--", plan_path],
    )
    if not add_result.ok:
        raise RuntimeError(
            f"ceremony.chunk_commits: add-commit resolution failed (exit "
            f"{add_result.returncode}): {add_result.stderr.strip()}"
        )
    add_shas = [line.strip() for line in add_result.stdout.splitlines() if line.strip()]
    if not add_shas:
        raise ValueError(
            f"ceremony.chunk_commits: no add-commit found for plan "
            f'"{plan_path}" — cannot anchor the chunk-commit range without a '
            "plan add-commit (AC6 fail-loud, never a silent empty list)"
        )
    # git log is newest-first; the OLDEST add-commit is the last line.
    add_sha = add_shas[-1]

    # Stage 2 — range-scope to <add-sha>..HEAD (AC4: not caller-overridable —
    # no parameter of this function can widen past this range) and read the
    # SUBJECT of every commit in it, unfiltered by any pathspec (AC3: no `--`
    # pathspec here, only stage 1's fixed one). --reverse for oldest-first
    # output, matching this function's own documented return order.
    range_spec = f"{add_sha}..HEAD"
    log_result = _git(
        ["log", "--reverse", range_spec, f"--format=%H{_FIELD_SEP}%s"],
        cwd=root,
    )
    if not log_result.ok:
        raise RuntimeError(
            f'ceremony.chunk_commits: range log over "{range_spec}" failed '
            f"(exit {log_result.returncode}): {log_result.stderr.strip()}"
        )

    # Stage 3 — subject filter (AC2). Deliberately `%s` (git's own
    # single-line subject placeholder) compared with a plain `str.startswith`
    # — never `--grep`, which scans every line of the full message (subject
    # AND body) and is exactly the false-positive this op exists to close.
    prefix = f"{chunk_id}:"
    commits: List[Dict[str, str]] = []
    for line in log_result.stdout.splitlines():
        if not line:
            continue
        sha, sep, subject = line.partition(_FIELD_SEP)
        if not sep:
            # Malformed/unsplit line (should not happen with a well-formed
            # --format) — skip rather than guess at a subject that isn't
            # really there.
            continue
        if subject.startswith(prefix):
            commits.append({"sha": sha, "subject": subject})

    return commits


@register_op("ceremony.chunk_commits")
def _handler(params: dict, repo_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """JSON-RPC "ceremony.chunk_commits" handler — read-only, sync.

    Sync (not async): the only I/O is synchronous `git` subprocess calls via
    `git_native`; `ipc.py` offloads sync handlers via `asyncio.to_thread`
    (same convention as `commit_exec_bit._handler`).

    Params:
        plan_path (str, required) — path to the plan document. Resolved via
                  `Path(plan_path).resolve()`, which resolves against THIS OP
                  PROCESS's own cwd, not the invoking caller's — a relative
                  path only lands correctly when the op process's cwd
                  happens to equal the caller's. A CLI invocation whose
                  process directly inherits the caller's shell cwd (e.g. `cd
                  <worktree> && coordinator-invoke ceremony.chunk_commits
                  '{...}'`) gets that for free; any indirection that does
                  NOT guarantee cwd propagation to the op process (a bin/
                  forwarder spawning or routing to the engine, for example)
                  must pass an absolute path — see `coordinator/bin/
                  chunk-commits`, which absolutizes plan_path against its own
                  cwd before it reaches this handler for exactly this
                  reason. This op derives its own git repository from
                  wherever the resolved path lands, rather than from any
                  engine-injected repo_root.
        chunk_id  (str, required) — the chunk id to query (e.g. "C1").

    `repo_root` (the engine-injected common_dir, present only for ops with a
    non-"none" `coordinator_core.op_scopes` entry) is accepted per the
    standard handler signature but UNUSED — this op's scope is unlisted
    (default "none"), matching the `cartography.*` / `workflow.validate`
    target-resolution convention: an explicit caller-supplied path, any repo,
    never the caller's own dispatching tree.

    Returns:
        A bare `list` of `{"sha": str, "subject": str}` dicts (AC5 — never a
        boolean, never a single sha) — the JSON-RPC "result" field IS this
        list, unwrapped, so `coordinator-invoke ceremony.chunk_commits
        '{...}'` prints the list directly.

    Raises (propagates to a non-zero CLI exit, AC6 — dispatch_message
    converts an uncaught handler exception to a JSON-RPC INTERNAL_ERROR
    response, and `coordinator_core.invoke.__main__` exits non-zero on any
    JSON-RPC error response):
        ValueError: params.plan_path / params.chunk_id missing or blank; the
            resolved plan_path does not exist; the resolved path is not
            inside any git repository; or the plan has no add-commit.
        RuntimeError: an underlying `git log` invocation itself failed.
    """
    plan_path_param = params.get("plan_path")
    if not isinstance(plan_path_param, str) or not plan_path_param.strip():
        raise ValueError(
            "ceremony.chunk_commits: params.plan_path is required and must "
            "be a non-empty string"
        )
    chunk_id_param = params.get("chunk_id")
    if not isinstance(chunk_id_param, str) or not chunk_id_param.strip():
        raise ValueError(
            "ceremony.chunk_commits: params.chunk_id is required and must "
            "be a non-empty string"
        )

    plan_abs = Path(plan_path_param.strip()).resolve()
    if not plan_abs.exists():
        raise ValueError(
            f'ceremony.chunk_commits: plan path "{plan_path_param}" does not '
            "resolve to an existing file"
        )
    if not plan_abs.is_file():
        raise ValueError(
            f'ceremony.chunk_commits: plan path "{plan_path_param}" resolves '
            "to a directory, not a plan document"
        )

    toplevel_result = _git(["rev-parse", "--show-toplevel"], cwd=plan_abs.parent)
    if not toplevel_result.ok:
        raise ValueError(
            f"ceremony.chunk_commits: could not resolve a git repository "
            f'containing "{plan_path_param}": {toplevel_result.stderr.strip()}'
        )
    repo_toplevel = Path(toplevel_result.stdout.strip())
    rel_plan_path = os.path.relpath(plan_abs, repo_toplevel).replace("\\", "/")

    return resolve_chunk_commits(repo_toplevel, rel_plan_path, chunk_id_param.strip())
