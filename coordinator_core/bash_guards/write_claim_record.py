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
#: `sed` branch).
#:
#: WHY THIS FILTER IS LOAD-BEARING RATHER THAN TIDY -- measured 2026-08-30,
#: because "it is not a path this session wrote to" is an aesthetic reason and
#: the real one is worse. `claim_index.commit_set` does NOT filter claims by
#: dirtiness, so a junk claim reaches `safe_paths` and lands in the commit
#: pathspec. Probed on a scratch repo: `git add -- real.txt 's/a/b/'` exits
#: 128 (`fatal: pathspec 's/a/b/' did not match any files`) and
#: `git commit -m x -- real.txt 's/a/b/'` exits 1, with the real change NOT
#: committed. One `sed -i` in a session would therefore destroy that
#: session's entire commit -- strictly worse than the dropped-file bug this
#: module exists to fix. Deleting this filter and letting
#: `reconciliation.claimed_absent` name the junk afterwards was considered and
#: is NOT viable for that reason.
#:
#: APPLIED ONLY WHEN THE HEAD VERB IS `sed`, and only together with
#: a genuinely recurring delimiter -- see `_is_claimable_target`. This pattern ALONE is
#: far too greedy in the one direction that must never be taken: judged
#: against any token it rejected `state/e2e-probe-bash-write.txt` (leading
#: `s`, a `t` recurring inside the trailing `.txt`, letters to the end), and
#: by extension most of `state/*.txt`. A dropped claim is invisible -- the
#: file simply fails to make the commit, which is the very bug this module
#: exists to fix -- so the head-verb gate, not the pattern, is what makes
#: this sound.
_SED_SCRIPT_RE = re.compile(r"^[sy](.).*\1[a-zA-Z]*$")


def _is_claimable_target(raw: str, head_base: str, resolved: str) -> bool:
    """True when `raw` (the literal token the command carried) is a real path
    candidate rather than an operand the extractor mis-read as one.

    Three conditions must ALL hold before anything is rejected, and each one
    is here because the previous shape of this function was wrong without it:

    1. `head_base == "sed"` -- judged against any token, `_SED_SCRIPT_RE`
       rejected `state/e2e-probe-bash-write.txt` and by extension most of
       `state/*.txt`.
    2. the s///-shape matches AND its delimiter genuinely recurs -- three or
       more occurrences of the candidate delimiter, which `s/a/b/` and
       `y/abc/xyz/` carry and a filename does not (`state/x.txt` has one `/`).
    3. `resolved` DOES NOT EXIST on disk. This is the one that makes it
       sound rather than merely narrower: `sed -i` can only edit a file that
       is already there, so a real `sed` file operand always exists and an
       edit script never does. Without it, `sed -i 's/a/b/' state/x.txt`
       still silently dropped its own file operand -- conditions 1 and 2 both
       hold for that path.

    A dropped claim is invisible: the file simply fails to make the commit,
    which is the exact bug this module exists to fix, so every condition here
    is written to fail toward CLAIMING rather than toward rejecting.

    The `resolved` stat is existence only -- never mtime, size, or content.
    It reads no attribution signal and so cannot reintroduce the race
    DR-258 refused; it is one `os.path.exists` on the `sed` branch alone,
    never on the common path.

    Containment against the repo root is the CALLER's separate `_is_within`
    check and is deliberately not repeated here.
    """
    if not raw or not raw.strip():
        return False
    if head_base != "sed":
        return True
    if not (_SED_SCRIPT_RE.match(raw) and len(raw) >= 4 and raw.count(raw[1]) >= 3):
        return True
    try:
        return os.path.exists(resolved)
    except Exception:
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
    relpaths each to forward slashes, and hands the list to
    `session.touch_record.append_touch_claims` -- the shared sink tail both
    this and C9's deletion-side recorder append through.

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
        from coordinator_core.session.touch_record import append_touch_claims

        rels = []
        for resolved_target, head_base, raw_target in _iter_write_sink_candidates(
            cmd, root
        ):
            try:
                if not _is_within(resolved_target, root):
                    continue
            except Exception:
                continue
            if not _is_claimable_target(raw_target, head_base, resolved_target):
                continue
            try:
                rels.append(os.path.relpath(resolved_target, root).replace(os.sep, "/"))
            except ValueError:
                continue
        append_touch_claims(rels, session_id, root)
    except Exception:
        return
