"""
coordinator_core.ops.plan_reapprove_mechanical_tags — JSON-RPC
"plan.reapprove_mechanical_tags" operation.

Purpose: restamp ``approved_body_sha`` for a plan whose body differs from its
approved revision ONLY by mechanical tag lines added at the head of a row's
``body: |`` block (``phase: code``). The approved revision is the newest
committed revision of the plan whose body hashes to the recorded stamp; the op
diffs the current body against it and restamps only when every difference is a
tag line. The hash itself is unchanged, so tag lines stay visible to review
everywhere else.

Wire params:
    plan (str, required) — the plan, repo-relative or absolute, inside the main
                           worktree.

Reply fields:
    {"plan": str, "restamped": bool, "outcome": "restamped"|"already-ok"|"refused",
     "message": str, "approved_body_sha_before": str|None,
     "approved_body_sha_after": str|None, "tag_lines_added": int,
     "offending_line": {"line": int, "text": str}|None}

A refusal is a REPLY, not an exception, and leaves the file's bytes untouched.

Negative-spec:
  - Does NOT change ``approval_body_sha``; nothing is neutralised in the hash.
  - Does NOT accept an added, changed, moved or deleted prose line, a tag whose
    key is outside ``MECHANICAL_TAG_KEYS``, or a tag outside a row's
    ``body: |`` block head inside the ``plan-tasks`` fence.
  - Does NOT touch an ``executing`` plan; that route is
    ``review-exec-auth-stamp restamp``.
  - Does NOT commit, flip ``status``, or re-run plan review.
  - Does NOT guess a base: no committed revision within ``_LOG_DEPTH`` matching
    the stamp is a refusal.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # rewrites approved_body_sha of the caller-named plan only
GENERATES: list = []

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_OK,
    approval_body_sha,
    canonical_body_sha,
    check_approved_body,
    frontmatter_body_text,
    read_fm_field_unquoted,
    split_frontmatter,
    stamp_approved_body_sha,
)
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.run import LOCAL_PLUMBING_BUDGET_SECS, run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops._path_guard import contained_path

#: Closed key set a tag line may carry. Extending it is a code change on purpose.
MECHANICAL_TAG_KEYS = frozenset({"phase"})

#: Committed revisions of the plan examined, newest first.
_LOG_DEPTH = 40

_BODY_HEADER_RE = re.compile(r"^(\s*)(?:-\s+)?body:\s*[|>][-+0-9]*\s*$")
_TAG_RE = re.compile(r"^(\s+)([a-z_]+):[ \t]+[A-Za-z0-9][A-Za-z0-9_.-]*[ \t]*$")
_ZERO_SHA = "0" * 40


def _is_tag(line: str) -> Optional[int]:
    """The tag line's indent, or None when ``line`` is not an allowed tag."""
    m = _TAG_RE.match(line.rstrip("\r\n"))
    if m is None or m.group(2) not in MECHANICAL_TAG_KEYS:
        return None
    return len(m.group(1))


def tag_only_difference(base_body: str, current_body: str) -> Tuple[int, Optional[Tuple[int, str]]]:
    """``(tags_added, first_offender)`` for ``current_body`` against ``base_body``.

    ``first_offender`` is ``(1-based line in current_body, text)`` or None when
    ``base_body`` is a subsequence of ``current_body`` and every extra line is a
    tag at the head of a ``body: |`` block inside the plan-tasks fence. A base
    line missing from the current body is reported at the line where it was lost.
    """
    base = base_body.splitlines(keepends=True)
    cur = current_body.splitlines(keepends=True)
    located = locate_fenced_block(current_body)
    if located.status == LocateStatus.LOCATED and located.span is not None:
        f_start = current_body.count("\n", 0, located.span[0])
        f_end = current_body.count("\n", 0, located.span[1])
    else:
        f_start = f_end = -1

    i = 0
    added = 0
    header_indent: Optional[int] = None
    run_indent: Optional[int] = None
    for n, line in enumerate(cur):
        in_run = header_indent is not None
        if i < len(base) and line == base[i]:
            i += 1
            m = _BODY_HEADER_RE.match(line.rstrip("\r\n"))
            if m is not None:
                header_indent, run_indent = len(m.group(1)), None
            elif in_run and (ind := _is_tag(line)) is not None and (run_indent in (None, ind)):
                run_indent = ind
            else:
                header_indent = run_indent = None
            continue
        ind = _is_tag(line)
        if (
            in_run
            and ind is not None
            and f_start <= n <= f_end
            and ind > (header_indent or 0)
            and run_indent in (None, ind)
        ):
            run_indent = ind
            added += 1
            continue
        return added, (n + 1, line.rstrip("\r\n"))
    if i < len(base):
        return added, (len(cur) + 1, "<missing from current body> " + base[i].rstrip("\r\n"))
    return added, None


def _committed_blob_shas(worktree_root: Path, rel: str) -> List[str]:
    """New-side blob shas of the plan's last ``_LOG_DEPTH`` committed revisions,
    newest first, from ONE ``git log`` read."""
    res = run_git(
        [
            "-C", str(worktree_root), "log", f"-n{_LOG_DEPTH}", "--no-renames",
            "--no-abbrev", "--raw", "--format=", "--", rel,
        ],
        timeout=LOCAL_PLUMBING_BUDGET_SECS,
    )
    if res.returncode != 0:
        return []
    shas: List[str] = []
    for ln in res.stdout.splitlines():
        if not ln.startswith(":"):
            continue
        parts = ln.split("\t", 1)[0].split()
        if len(parts) >= 4 and parts[3] != _ZERO_SHA:
            shas.append(parts[3])
    return shas


def _approved_base_text(worktree_root: Path, rel: str, stamped: str) -> Optional[str]:
    common_dir = resolve_git_common_dir(worktree_root)
    for sha in _committed_blob_shas(worktree_root, rel):
        obj = read_object(common_dir, sha)
        if obj is None or obj[0] != "blob":
            continue
        text = obj[1].decode("utf-8", errors="replace")
        if stamped.lower() in (
            approval_body_sha(text),
            canonical_body_sha(text),
            approval_body_sha(text, neutral_plan_edges=False),
        ):
            return text
    return None


def _resolve_plan(raw: str, worktree_root: Path) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    if contained_path(candidate, [worktree_root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
    return candidate


@register_op("plan.reapprove_mechanical_tags")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.reapprove_mechanical_tags" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.reapprove_mechanical_tags requires a resolved repo_root")
    raw_plan = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError(
            f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file"
        )

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = _resolve_plan(raw_plan.strip(), worktree_root)
    try:
        rel = plan_path.relative_to(worktree_root).as_posix()
    except ValueError:  # pragma: no cover - contained_path already refused this
        rel = plan_path.as_posix()

    state: Dict[str, Any] = {"added": 0, "offender": None, "before": None, "after": None}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {rel}")
        stamped = read_fm_field_unquoted(split.fm_text, "approved_body_sha")
        state["before"] = stamped
        if not stamped:
            raise MutateAbort(
                "approved_body_sha absent: nothing to re-approve; run plan review"
            )
        status = (read_fm_field_unquoted(split.fm_text, "status") or "").lower()
        if status == "executing":
            raise MutateAbort(
                "plan is executing: run `review-exec-auth-stamp restamp <plan> --by <reviewer> --reason <text>`"
            )
        if check_approved_body(old_text)[0] == APPROVED_BODY_OK:
            state["outcome"] = "already-ok"
            state["after"] = stamped
            return old_text

        base_text = _approved_base_text(worktree_root, rel, stamped)
        if base_text is None:
            raise MutateAbort(
                f"no committed revision of {rel} within the last {_LOG_DEPTH} matches "
                f"approved_body_sha {stamped[:12]}; re-run plan review to re-approve"
            )
        added, offender = tag_only_difference(
            frontmatter_body_text(base_text), frontmatter_body_text(old_text)
        )
        state["added"] = added
        if offender is not None:
            state["offender"] = {"line": offender[0], "text": offender[1]}
            raise MutateAbort(
                f"body differs from the approved revision beyond tag lines at body line "
                f"{offender[0]}: {offender[1]!r}; re-run plan review to re-approve"
            )
        new_text = stamp_approved_body_sha(old_text)
        if check_approved_body(new_text)[0] != APPROVED_BODY_OK:
            raise MutateAbort("restamp did not produce an ok approved_body_sha; file untouched")
        state["outcome"] = "restamped"
        state["after"] = approval_body_sha(new_text)
        return new_text

    try:
        locked_rmw(plan_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        raise ValueError(f"no such plan: {rel}")
    except LockTimeout as exc:
        raise ValueError(f"timed out waiting for the file lock on {rel}: {exc}")
    except MutateAbort as exc:
        return {
            "plan": rel,
            "restamped": False,
            "outcome": "refused",
            "message": exc.args[0] if exc.args else "mutation aborted",
            "approved_body_sha_before": state["before"],
            "approved_body_sha_after": None,
            "tag_lines_added": state["added"],
            "offending_line": state["offender"],
        }

    outcome = state["outcome"]
    return {
        "plan": rel,
        "restamped": outcome == "restamped",
        "outcome": outcome,
        "message": (
            "approved_body_sha already matches the body"
            if outcome == "already-ok"
            else f"restamped; {state['added']} tag line(s) added since approval"
        ),
        "approved_body_sha_before": state["before"],
        "approved_body_sha_after": state["after"],
        "tag_lines_added": state["added"],
        "offending_line": None,
    }
