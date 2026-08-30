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

SECOND SPEC BACKLINK (the scratchpad-script branch, added 2026-08-30):
docs/plans/2026-08-30-the-guard-s-own-remediation-route-hides.md, chunk C1.
`record_write_claims` gained ONE additional branch: `python`/`python3
<scratchpad-script.py>` -- the guard's own remediation route for an inline
`-c`/heredoc denial -- is a write-target shape this module previously could
not see at all, because the path lives inside a FILE the command names, not
in the command's own text. This is still NOT a second detector: the branch
reuses `_write_bump_sink_shapes._python_write_targets_in_text` (the exact
scanner already applied to a heredoc body and a `-c` payload above) against
the script file's own text, and introduces no new tokenizer, no new regex
shape table, and no new write-sink enumeration of its own. See
`_scratchpad_script_write_targets`'s own docstring for the scratchpad-only
scope, the size cap, and why a repo-committed script is deliberately never
scanned.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

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


def _rel_if_inside(resolved_target: str, root: str) -> Optional[str]:
    """`resolved_target` relpathed to `root` with forward slashes, or `None`
    when it is not inside `root` or the relpath cannot be computed (e.g.
    different drives on Windows). Shared containment+relpath tail for both
    `record_write_claims` candidate loops."""
    try:
        if not _is_within(resolved_target, root):
            return None
    except Exception:
        return None
    try:
        return os.path.relpath(resolved_target, root).replace(os.sep, "/")
    except ValueError:
        return None


#: Read-size ceiling for the scratchpad-script branch, in bytes. This is a
#: PreToolUse hot path -- one bounded read, never a stream, never a second
#: pass -- so the cap answers "how much of this file may we read before
#: refusing" rather than "how big may a legitimate scratch script be": a
#: script over this size claims nothing and raises nothing (see
#: `_scratchpad_script_write_targets`), it is never truncated-and-scanned.
#: 64 KiB is generously above any real hand-written scratch fixer script
#: while staying well inside a single-digit-millisecond read on the repo's
#: own drive (AC7's own measured budget for this whole recording module).
_SCRATCHPAD_SCRIPT_READ_CAP_BYTES = 65536


def _python_head_script_operand(cmd: str) -> Optional[str]:
    """The single script-file operand of a bare `python`/`python3
    <script.py>` invocation found at depth 0 of `cmd`, or `None` when no
    depth-0 segment matches that exact shape.

    Reuses `_command_tokenizer.resolve_command_positions` -- the package's
    one resolve-once tokenizer -- and `_write_bump_sink_shapes._PYTHON_C_
    FLAG_INTERPRETERS` for head-verb identity, mirroring `_write_bump_sink_
    shapes._iter_python_dash_c_payloads`'s own depth-0-only, fail-open
    walk. No new tokenizer, no new interpreter set.

    Deliberately narrow: a segment carrying a `-c` flag (an inline payload,
    already covered by `extract_interpreter_payload_write_sink_targets`
    above) or more than one non-flag positional argument is not this shape
    and yields `None` for that segment -- ambiguity here resolves toward
    "not a scratchpad script", never toward guessing which operand is the
    script.
    """
    from coordinator_core.bash_guards._command_tokenizer import (
        ResolutionConfidence,
        normalize_executable_basename,
        resolve_command_positions,
    )
    from coordinator_core.bash_guards._write_bump_sink_shapes import (
        _PYTHON_C_FLAG_INTERPRETERS,
    )

    try:
        segments = resolve_command_positions(
            cmd, preserve_windows_backslashes=(os.name == "nt")
        )
    except Exception:
        return None

    for seg in segments:
        if seg.depth != 0 or seg.confidence == ResolutionConfidence.UNRESOLVED:
            continue
        tokens = seg.tokens
        if not tokens:
            continue
        head_base = normalize_executable_basename(tokens[0])
        if head_base not in _PYTHON_C_FLAG_INTERPRETERS:
            continue
        args = tokens[1:]
        if any(a == "-c" or a.startswith("-c") for a in args):
            continue
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) == 1:
            return positional[0]
    return None


def _scratchpad_script_write_targets(cmd: str, root: str) -> List[str]:
    """Raw candidate write-target strings found INSIDE the text of a
    scratchpad Python SCRIPT FILE that `cmd` names -- the guard's own
    remediation route for an inline `-c`/heredoc denial, and the one shape
    `extract_interpreter_payload_write_sink_targets` cannot see because the
    path never appears in the command's own text at all.

    Scoped to the session's OWN scratchpad on purpose, never any `.py` file
    the command might name -- see the module docstring's SECOND SPEC
    BACKLINK for why a repo-committed script is deliberately never scanned.

    The scratchpad root is resolved the way the guards in this package
    already resolve "is this under the harness-designated per-session
    scratchpad" -- `_write_bump_applicability._all_temp_roots` (which closes
    the macOS `TMPDIR`-vs-`/private/tmp` gap `gettempdir()` alone misses;
    see that function's own docstring) -- rather than a second, independently
    -derived notion of the scratchpad. Containment against each candidate
    root uses THIS module's own `_is_within` (the same normcase/normpath
    form the common path already applies), not a new comparison.

    Never raises: every step here is wrapped in this function's own
    `try/except Exception: return []`, and `record_write_claims`' own outer
    `try` is the backstop above that -- a failure here must cost this branch
    its candidates, never the common path's.

    A substring pre-filter (`"python" not in cmd`) is checked before any
    tokenizing, so the overwhelming majority of commands -- which do not
    invoke a Python interpreter at all -- never pay for
    `resolve_command_positions`. Pure text rejection: it can only return
    `[]` early, never open a new detection path.

    Exactly ONE existence/stat probe plus one bounded read
    (`_SCRATCHPAD_SCRIPT_READ_CAP_BYTES`), on this branch only, never a
    directory walk: a `.py` operand outside the scratchpad, a nonexistent or
    unreadable file, or a file over the size cap all yield `[]` here, no
    exception ever escapes.
    """
    try:
        if "python" not in cmd:
            return []
        operand = _python_head_script_operand(cmd)
        if not operand or not operand.lower().endswith(".py"):
            return []

        candidate = operand if os.path.isabs(operand) else os.path.join(root, operand)
        candidate = os.path.normpath(candidate)

        from coordinator_core.bash_guards._write_bump_applicability import (
            _all_temp_roots,
        )

        temp_roots = _all_temp_roots()
        if not any(_is_within(candidate, r) for r in temp_roots):
            return []

        try:
            if not os.path.isfile(candidate):
                return []
            if os.path.getsize(candidate) > _SCRATCHPAD_SCRIPT_READ_CAP_BYTES:
                return []
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(_SCRATCHPAD_SCRIPT_READ_CAP_BYTES)
        except OSError:
            return []

        from coordinator_core.bash_guards._write_bump_sink_shapes import (
            _python_write_targets_in_text,
        )

        return _python_write_targets_in_text(text)
    except Exception:
        return []


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

    ALONGSIDE that loop (never replacing it), also consults
    `_scratchpad_script_write_targets(cmd, root)` for the `python`/`python3
    <scratchpad-script.py>` shape, pushed through the same `_rel_if_inside`
    tail as every other candidate. See the module docstring's SECOND SPEC
    BACKLINK and that function's own docstring for the scratchpad-only scope.

    Failure posture copied verbatim from `dispatch_checks._rm_flush_touch`
    (its own docstring is the reference): no path here may raise: a
    recording failure must never turn an otherwise-ALLOWED command into a
    denied one. `session_id`/`root` are the caller's own already-resolved
    values -- this function never re-derives either (no `rev-parse`, no
    `getcwd`). No subprocess spawn and no filesystem walk beyond what
    `_iter_write_sink_candidates` itself performs (it reads only `cmd`'s own
    text) plus, on the scratchpad branch only, one bounded file read.

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
            if not _is_claimable_target(raw_target, head_base, resolved_target):
                continue
            rel = _rel_if_inside(resolved_target, root)
            if rel is not None:
                rels.append(rel)

        for raw_target in _scratchpad_script_write_targets(cmd, root):
            resolved_target = (
                raw_target if os.path.isabs(raw_target) else os.path.join(root, raw_target)
            )
            rel = _rel_if_inside(resolved_target, root)
            if rel is not None:
                rels.append(rel)

        append_touch_claims(rels, session_id, root)
    except Exception:
        return
