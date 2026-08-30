"""coordinator_core.bash_guards.write_claim_record -- the write-side twin of
``dispatch_checks._rm_flush_touch`` (C9, 2026-08-27): best-effort recording
of a plain ``VERB_TOUCH`` claim for every in-repo path THIS session's own
Bash call is about to write, so the committing ceremony
(``session.safe_commit_offer``) can see work that landed through a heredoc,
a redirect, a `sed -i`, or an interpreter payload -- shapes that fire no
PostToolUse Write/Edit hook and today leave no claim behind at all.

Spec backlink: docs/plans/2026-08-30-a-bash-write-reaches-the-ledger-that-
decides-what-gets-committed.md, chunk C1.

THIS IS RECORDING ONLY -- it does not change what the guard chain allows or
denies, and it must never be able to. See ``record_write_claims``'s own
docstring for the failure posture this module copies verbatim from
``_rm_flush_touch``.

NEGATIVE SPEC -- do not widen this module into a second detector. It
consumes ``bump_outside_repo_write._iter_write_sink_candidates`` exactly as
that guard already resolves it (no new tokenizer, no new shape table); the
one addition here, ``_is_claimable_target``, exists solely to reject a
candidate that extractor is known to over-include for the outside-repo
question but must not be claimed for THIS one (a `sed` edit-script operand
mistaken for a file). Do not "fix" that over-inclusion inside
``_write_bump_sink_shapes`` -- the outside-repo guard also consumes that
table and its behaviour there is correct for its own question.
"""

from __future__ import annotations

import os
import re
from typing import Optional

#: A `sed` edit-script operand -- `s/a/b/`, `s|a|b|g`, `y/abc/xyz/` -- shaped
#: as COMMAND, DELIMITER, ..., same DELIMITER, optional trailing flag
#: letters. `_iter_write_sink_candidates` yields this alongside the real
#: file operand for any `sed -i '<script>' <file>` invocation (both are
#: bare positional tokens once `-i` is present -- see
#: `_write_bump_sink_shapes.extract_write_sink_targets_for_segment`'s own
#: `sed` branch) and it is NOT a path this session wrote to; claiming it
#: would append a claim for a string that is not a repo-relative path at
#: all. A real path essentially never starts with a bare `s`/`y` followed
#: immediately by a non-alphanumeric delimiter that recurs later in the
#: same token, so this heuristic is sound in the direction that matters
#: here (under-claiming, never over-claiming a real file).
_SED_SCRIPT_RE = re.compile(r"^[sy](.).*\1[a-zA-Z]*$")


def _is_claimable_target(raw: str) -> bool:
    """True when `raw` (the literal token the command carried) looks like a
    real path candidate rather than an operand the extractor mis-read as
    one. The ONLY rejection performed today is the `sed` edit-script shape
    above -- see the module docstring for why that is the one known
    false-positive the shared extractor produces for the claiming question
    specifically.

    Takes the raw token and nothing else, deliberately: containment against
    the repo root is the CALLER's separate `_is_within` check, and a
    `resolved`/`root` pair threaded through here for a caller that might
    one day want root-relative reasoning would be two parameters this
    function never reads.
    """
    if not raw or not raw.strip():
        return False
    if _SED_SCRIPT_RE.match(raw):
        return False
    return True


def _is_within(path: str, root: str) -> bool:
    """True when `path` is `root` or lies underneath it -- pure string/
    normcase work, no filesystem probe. A local twin of
    `dispatch_checks._is_within` rather than an import of it: this module
    must not couple to that file's private surface."""
    p = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def record_write_claims(
    cmd: str,
    session_id: str,
    root: Optional[str],
    *,
    denied: bool,
) -> None:
    """Best-effort recording of a `VERB_TOUCH` claim for every in-repo write
    target `cmd` names, appended to THIS session's own touch-record sink.
    Returns `None` always, raises never.

    When `denied` is true this returns immediately, having done nothing --
    a claim for work that never happened is a lie the committing ceremony
    would act on (see the plan's own Anti-scope). Otherwise it consumes
    `bump_outside_repo_write._iter_write_sink_candidates(cmd, root)` (the
    SAME extractor the outside-repo guard already runs over this same
    command, on this same PreToolUse call -- no second tokenizer), keeps
    only targets that resolve inside `root` and pass `_is_claimable_target`,
    relpaths each to forward slashes, and appends one `VERB_TOUCH` per path
    via `session.touch_record.append_event`.

    Failure posture copied verbatim from `dispatch_checks._rm_flush_touch`
    (its own docstring is the reference): no path here may raise: a
    recording failure must never turn an otherwise-ALLOWED command into a
    denied one. `session_id`/`root` are the caller's own already-resolved
    values -- this function never re-derives either (no `rev-parse`, no
    `getcwd`). No subprocess spawn and no filesystem walk beyond what
    `_iter_write_sink_candidates` itself performs (it reads only `cmd`'s own
    text).

    Never over-claims: a path `cmd` does not name gets no claim, ever -- no
    mtime check, no `git status`, no before/after comparison. See the
    plan's Anti-scope, "Never over-claim" -- that property is inherited
    from the extractor (command-text only, no filesystem race) and this
    function adds no probe of its own that could reintroduce one.
    """
    if denied:
        return
    if not cmd or not cmd.strip() or not session_id or not root:
        return
    try:
        from coordinator_core.bash_guards.bump_outside_repo_write import (
            _iter_write_sink_candidates,
        )
        from coordinator_core.session.touch_record import (
            VERB_TOUCH,
            append_event,
            sink_path,
        )

        sid_dir = os.path.join(root, ".git", "coordinator-sessions", session_id)
        sink = sink_path(sid_dir)

        for resolved_target, _head_base, raw_target in _iter_write_sink_candidates(
            cmd, root
        ):
            try:
                if not _is_within(resolved_target, root):
                    continue
            except Exception:
                continue
            if not _is_claimable_target(raw_target):
                continue
            try:
                rel = os.path.relpath(resolved_target, root).replace(os.sep, "/")
            except ValueError:
                continue
            try:
                append_event(
                    sink,
                    session_id=session_id,
                    agent_id=None,
                    verb=VERB_TOUCH,
                    path=rel,
                )
            except Exception:
                continue
    except Exception:
        return
