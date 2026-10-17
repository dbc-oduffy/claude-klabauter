"""coordinator_core.git.action_guard -- the guarded seam every Python-side
git-writing caller routes through, evaluating a deny-capable guard's own
predicate at the ACTION rather than at the harness.

Built for `docs/plans/2026-08-30-every-deny-capable-guard-fires-on-a-tool-
call-the-op-route-never-makes.md` § C2. `commit_paths`
(`coordinator_core.git.commit`) is the first caller, and today (measured at
HEAD before this row) consults nothing: no ownership check, no `dispatch_
checks` import, no claim this seam existed at all.

ONE PREDICATE THIS MODULE EXPOSES: sweeping-pathspec + orphan-adoption +
out-of-repo-absolute shape checking
(`block_subagent_commit._pathspec_shape_permitted`) -- the payload-shape-
agnostic, IDENTITY-FREE core extracted (pure motion, C2's own `writes:`)
out of `_git_commit_agent_may_commit`, which previously took a raw
command STRING and could not be called from a seam that already holds
`paths`. The ~5500 lines of command-string tokenizing / shell unwrapping /
`python -c` AST constant-folding that RESOLVE a command string into
`(paths, include_orphans)` are NOT lifted -- at this seam the caller
already holds that resolved shape, so there is nothing for that bulk to
do.

CALLER IDENTITY IS NOT LIFTABLE (`state/dispatch-briefs/.../C2a.md`,
referenced in the escape table). `block_subagent_commit`'s ownership leg
grants on a HARNESS-SUPPLIED, non-cooperative `agent_id` read off the
PreToolUse payload -- its own module docstring rejects the cooperative
`COORDINATOR_AGENT_CONTEXT` alternative for exactly the reason this seam
must respect: an environment variable, or any other self-reported value,
is something the very caller the guard exists to police can set. C2a
measured, at HEAD, ZERO occurrences of `agent_id`, `session_id`, or
`subagent` in `coordinator_core/git/commit.py` or `coordinator_core/ops/
ceremony/commit_v2.py` -- there is no non-cooperative identity input
reaching this op route at any price payable in-process today.

So this module does NOT expose a function that takes a caller-supplied
`session_id` and trusts it as if it were the harness-stamped `agent_id`.
That would reproduce, at the seam, the exact fail-open shape the guard
exists to close (a caller self-reporting the identity a check is meant to
verify IT against). Instead, `assert_noncooperative_identity_available`
below is the seam's answer to "does this call carry a trustworthy
identity": it always returns `False` today, because no non-cooperative
channel exists to make it return anything else, and any caller consulting
it before acting is following the prime exit criterion's third disjunct
verbatim -- "the op route refuses in the absence of that input rather
than acting on an unresolved or caller-supplied substitute."

NOTE (review: overengineering-reviewer Finding 1, 2026-08-30): this module
used to also expose `assert_ownership_scope` and `assert_git_commit_agent_
pathspec_permitted`, two seams over an ownership-scoped predicate that
took an unverified `session_id`. Both had zero callers repo-wide -- their
own docstrings cited a future caller the section above shows is
permanently impossible on this op route -- and were deleted rather than
kept as seams for a caller that cannot exist. A future verified-identity
caller re-adding a five-line wrapper over `assert_paths_in_session_scope`
or `_git_commit_agent_pathspec_permitted` is the cheap half of that work.

NEGATIVE SPEC:
  - Does NOT accept a `session_id` parameter anywhere in this module that is
    treated as verified.
  - Does NOT shell out. Every function in this module is a plain in-process
    call over already-imported predicates -- no `subprocess`, no `run_git`,
    zero process spawns per call (DR-378's kill bar is not re-earned by a
    predicate that shells out; see the module docstring of `commit.py` for
    the plan that killed `run_commit_pipeline` at 1513ms/31 procs for
    exactly that shape of cost).
  - Does NOT import `block_subagent_commit` (a heavy module). The
    shape-only legs are mirrored in `_pathspec_shape_permitted` here and
    pinned to `block_subagent_commit._pathspec_shape_permitted` by
    `test_action_guard_shape_parity`; the bash-route guard keeps its own
    copy as the single home for command-string callers.

`assert_no_undeclared_staged_deletion` closes the op-route leg of
`state/bug-backlog/2026-08-31-four-bug-blitz-commits-deleted-five-file-
6216c89502b9.yaml`: the bash-route tripwire
(`bash_guards.commit_tripwires.check_undeclared_staged_deletion`) fires only
on a `git commit` the harness's own PreToolUse guard can see, and the four
accident commits that row was filed against landed through `commit_paths`
instead, which no bash guard is ever invoked for. Same predicate, ported
in process: a genuine (HEAD-tracked) staged deletion whose commit message
never says so anywhere in its body is refused rather than landed.
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import Sequence

#: Same word list `bash_guards.commit_tripwires._DELETION_VERBS` validates
#: (0 false positives over the 699-commit measurement recorded in the row
#: above) -- kept as its own copy rather than imported, since this module's
#: own negative spec is zero cross-module coupling beyond the lazy
#: `commit.py` import it already carries, and this predicate needs neither.
_DELETION_VERBS = re.compile(
    r"\b("
    r"delet\w*|remov\w*|rm|retir\w*|drop(s|ped|ping)?|gravestone\w*|"
    r"prun\w*|purg\w*|kill(s|ed|ing)?|untrack\w*|discard\w*|quarantin\w*|"
    r"obsolet\w*|sunset\w*|revert\w*|supersed\w*|retract\w*|withdraw\w*|"
    r"mov(e|es|ed|ing)|mv|renam\w*|relocat\w*|archiv\w*|migrat\w*|"
    r"clean(s|ed|up)?|strip(s|ped|ping)?|excis\w*|unregister\w*"
    r")\b",
    re.IGNORECASE,
)


_LEG_NO_PATHSPEC = "leg:no-pathspec"
_LEG_SWEEPING_PATHSPEC = "leg:sweeping-pathspec"
_LEG_ABSOLUTE_OUT_OF_REPO = "leg:absolute-out-of-repo"
_SWEEPING_FLAG_TOKENS = frozenset({"-A", "-a", "--all"})
_GLOB_CHARS = frozenset("*?[")
_LITERAL_PATHSPEC_PREFIX = ":(literal)"
_DRIVE_ABSOLUTE_PATHSPEC_RE = re.compile(r"^[A-Za-z]:/")


def _literal_inner(candidate: str):
    if candidate.startswith(_LITERAL_PATHSPEC_PREFIX):
        inner = candidate[len(_LITERAL_PATHSPEC_PREFIX):]
        if inner:
            return inner
    return None


def _is_absolute(candidate_posix: str) -> bool:
    return bool(
        posixpath.isabs(candidate_posix) or _DRIVE_ABSOLUTE_PATHSPEC_RE.match(candidate_posix)
    )


def _drive_key(path_posix: str) -> str:
    if _DRIVE_ABSOLUTE_PATHSPEC_RE.match(path_posix):
        return path_posix[0].lower() + path_posix[1:]
    return path_posix


def _bracket_exists_literally(candidate: str, git_root: str) -> bool:
    if not git_root:
        return False
    fs_path = candidate if _is_absolute(candidate.replace("\\", "/")) else os.path.join(git_root, candidate)
    try:
        return os.path.exists(fs_path)
    except OSError:
        return False


def _element_is_sweeping(path, git_root: str) -> bool:
    if not isinstance(path, str):
        return True
    candidate = path.strip()
    if not candidate or candidate in (".", "./"):
        return True
    literal_inner = _literal_inner(candidate)
    if literal_inner is not None:
        candidate = literal_inner
    elif candidate.startswith(":"):
        return True
    if candidate in _SWEEPING_FLAG_TOKENS:
        return True
    if literal_inner is None and any(ch in candidate for ch in _GLOB_CHARS):
        if not (
            "[" in candidate
            and "*" not in candidate
            and "?" not in candidate
            and _bracket_exists_literally(candidate, git_root)
        ):
            return True
    if not git_root:
        return True
    root = posixpath.normpath(git_root.replace("\\", "/"))
    candidate_posix = candidate.replace("\\", "/")
    resolved = (
        candidate_posix
        if posixpath.isabs(candidate_posix)
        else posixpath.normpath(posixpath.join(root, candidate_posix))
    )
    resolved = posixpath.normpath(resolved)
    if resolved == root:
        return True
    parent = root
    while True:
        next_parent = posixpath.dirname(parent)
        if next_parent == parent:
            return False
        if next_parent == resolved:
            return True
        parent = next_parent


def _has_out_of_repo_absolute(paths, git_root: str) -> bool:
    root_prefix = _drive_key(posixpath.normpath(git_root.replace("\\", "/")))
    if not root_prefix.endswith("/"):
        root_prefix += "/"
    for path in paths:
        path = _literal_inner(path) or path
        candidate_posix = path.replace("\\", "/")
        if _is_absolute(candidate_posix) and not _drive_key(
            posixpath.normpath(candidate_posix)
        ).startswith(root_prefix):
            return True
    return False


def _pathspec_shape_permitted(paths, include_orphans: bool, git_root: str):
    """Mirror of ``block_subagent_commit._pathspec_shape_permitted`` (the
    identity-free shape legs), kept in step by
    ``test_action_guard_shape_parity``. ``block_subagent_commit`` is a
    multi-thousand-line module whose import dominated the commit landing,
    and it stays the single home for the bash-route guard.
    """
    if not paths:
        return False, _LEG_NO_PATHSPEC
    if any(_element_is_sweeping(p, git_root) for p in paths):
        return False, _LEG_SWEEPING_PATHSPEC
    if _has_out_of_repo_absolute(list(paths), git_root):
        return False, _LEG_ABSOLUTE_OUT_OF_REPO
    return True, ""


def assert_pathspec_shape_permitted(
    paths,
    include_orphans: bool,
    git_root: str,
) -> None:
    from coordinator_core.git.commit import CommitDeniedByActionGuard

    allowed, reason = _pathspec_shape_permitted(paths, include_orphans, git_root)
    if not allowed:
        raise CommitDeniedByActionGuard(reason or "pathspec shape denied")


def assert_noncooperative_identity_available() -> None:
    """The seam's answer to the guard leg C2a found NOT liftable: caller
    identity. Raises `coordinator_core.git.commit.CommitDeniedByActionGuard`
    unconditionally -- there is no non-cooperative identity channel
    reaching any Python-side git-writing op route today (C2a, measured at
    HEAD: zero occurrences of `agent_id` / `session_id` / `subagent` in
    `coordinator_core/git/commit.py` or `coordinator_core/ops/ceremony/
    commit_v2.py`), so this is decidable with no argument at all (review:
    overengineering-reviewer Finding 2, 2026-08-30 -- the prior `session_id:
    object = None` parameter was never read and its only live behaviour
    was to be ignored before the unconditional raise; dropped along with
    the `commit_paths(restrict_to_session=...)` parameter that was its only
    caller).

    This is not "no channel exists today" -- a wording that invites a future
    reader to go looking for one. Per the binding spike verdict
    (`docs/research/spike-verdicts/2026-08-30-non-cooperative-caller-
    identity-at-the-in-process-op-route.md`, verdict **not-viable**): no
    non-cooperative caller identity CAN exist for an in-process caller of
    `commit_paths` at any price payable in-process -- the harness-stamped
    `agent_id` is presented only on the tool-call channel and reaches
    `warm/hook_http.py`, no subagent marker exists in the process
    environment, and a subagent shares its EM's `CLAUDE_PID` /
    `CLAUDE_CODE_SESSION_ID` / process tree, so ancestry is not even a layer
    here. This function's unconditional raise is therefore the correct
    permanent answer for this op route, not a stub awaiting wiring.

    Matches the prime exit criterion's third disjunct: "for a guard whose
    predicate depends on an input the op route has no non-cooperative
    source for ... the op route refuses in the absence of that input rather
    than acting on an unresolved or caller-supplied substitute."
    """
    from coordinator_core.git.commit import CommitDeniedByActionGuard

    raise CommitDeniedByActionGuard(
        "caller identity is unresolvable on this op route -- no "
        "non-cooperative identity channel can exist for an in-process "
        "caller of coordinator_core.git (C2a measurement; spike verdict "
        "docs/research/spike-verdicts/2026-08-30-non-cooperative-caller-"
        "identity-at-the-in-process-op-route.md, not-viable), so this seam "
        "refuses rather than trust a caller-supplied session_id as a "
        "substitute for a harness-stamped agent_id"
    )


def assert_no_undeclared_staged_deletion(paths: Sequence[str], message: str) -> None:
    if not paths:
        return
    if _DELETION_VERBS.search(message or ""):
        return

    from coordinator_core.git.commit import CommitDeniedByActionGuard

    shown = list(paths)[:10]
    more = len(paths) - len(shown)
    listed = "\n".join("  D  %s" % p for p in shown)
    if more > 0:
        listed += "\n  ... and %d more" % more

    raise CommitDeniedByActionGuard(
        "UNDECLARED STAGED DELETION: this commit removes %d tracked "
        "file(s), and its message does not mention a removal.\n\n"
        "%s\n\n"
        "If that is intended, say so in the message and this stops firing. "
        "If it is not, drop the path(s) from `deleted_paths` before "
        "retrying." % (len(paths), listed)
    )
