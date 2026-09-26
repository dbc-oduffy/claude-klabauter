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

THIRD SPEC BACKLINK (the read-shape extractor, added 2026-09-02):
docs/plans/2026-09-02-a-write-that-discards-what-you-never-saw.md, chunk
C1. `resolve_read_targets` is this module's READ-shape twin of its own
write-shape extractor, living beside it (not in a new module -- see that
chunk's body) because command-text tokenization on the wrong side of the
plane boundary is exactly what a new `session/`-level module would put it
on. It answers a different question than everything else here (which
paths does `command_text` NAME as a read source, not a write sink) and
consumes the SAME tokenizer this module already calls
(`_command_tokenizer.resolve_command_positions`), introducing no second
dialect. See its own docstring for the bounded shapes it resolves and the
under-claim-rather-than-guess rule it inherits from this module.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

#: as COMMAND, DELIMITER, ..., same DELIMITER, optional trailing flag
#: WHY THIS FILTER IS LOAD-BEARING RATHER THAN TIDY -- measured 2026-08-30,
_SED_SCRIPT_RE = re.compile(r"^[sy](.).*\1[a-zA-Z]*$")

_UNEXPANDED_TOKEN_RE = re.compile(r"[$`]")

#: `_REDIRECT_OP_RE` only recognizes an operator and its target as TWO
_LEAKED_REDIRECT_RE = re.compile(r"^\d*>{1,2}")

#: (`dispatch_checks._BT_REDIRECTION_TOKEN_RE`'s own note). Either shape
_LEAKED_HEREDOC_OPENER_RE = re.compile(r"^<<-?")


def _is_bare_heredoc_opener(raw: str) -> bool:
    return raw in ("<<", "<<-")


def _is_claimable_target(raw: str, head_base: str, resolved: str) -> bool:
    """True when `raw` (the literal token the command carried) is a real path
    candidate rather than an operand the extractor mis-read as one.

    Checked for EVERY candidate, regardless of `head_base`, before the
    `sed`-specific rule below ever runs:

    - `_UNEXPANDED_TOKEN_RE` -- an unexpanded shell variable or command
      substitution is never a real path; claiming it fabricates a claim for
      a file this session never touched while leaving the real, expanded
      target unclaimed.
    - `_LEAKED_REDIRECT_RE` -- a redirection operator the tokenizer left
      glued to its own target (`2>&1`, `2>/dev/null`) is an operator, not a
      file.
    - `_LEAKED_HEREDOC_OPENER_RE` -- a heredoc opener the tokenizer left
      glued to its own marker (`<<EOF`, `<<'MSG'`) is likewise an operator,
      not a file. The bare form (`<<` with the marker as a SEPARATE
      following token) is rejected the same way here; the caller
      additionally skips that following token itself (see
      `_is_bare_heredoc_opener`, consulted by `record_write_claims`) since
      this function only ever sees one candidate at a time.
    - `resolved` is not an existing directory -- `mkdir state/some-dir`
      names a directory, not a file this session wrote content to; a
      directory claim is junk the same way a redirect operator is.

    Then, unchanged from before, three conditions must ALL hold before a
    `sed` candidate specifically is rejected, and each one is here because
    the previous shape of this function was wrong without it:

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
    is written to fail toward CLAIMING rather than toward rejecting -- except
    the three checks above, which exist precisely because the shape they
    reject is never a real write target under any interpretation.

    The `resolved` stat is existence-and-directory-ness only -- never mtime,
    size, or content. It reads no attribution signal and so cannot
    reintroduce the race DR-258 refused; it is `os.path.isdir`/
    `os.path.exists`, both on `resolved` alone, never a second probe of the
    filesystem beyond what this function already did.

    Containment against the repo root is the CALLER's separate `_is_within`
    check and is deliberately not repeated here.
    """
    if not raw or not raw.strip():
        return False
    if _UNEXPANDED_TOKEN_RE.search(raw):
        return False
    if _LEAKED_REDIRECT_RE.match(raw):
        return False
    if _LEAKED_HEREDOC_OPENER_RE.match(raw):
        return False
    try:
        if os.path.isdir(resolved):
            return False
    except Exception:
        pass
    if head_base != "sed":
        return True
    if not (_SED_SCRIPT_RE.match(raw) and len(raw) >= 4 and raw.count(raw[1]) >= 3):
        return True
    try:
        return os.path.exists(resolved)
    except Exception:
        return True


def _is_within(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _rel_if_inside(resolved_target: str, root: str) -> Optional[str]:
    try:
        if not _is_within(resolved_target, root):
            return None
    except Exception:
        return None
    try:
        return os.path.relpath(resolved_target, root).replace(os.sep, "/")
    except ValueError:
        return None


_SCRATCHPAD_SCRIPT_READ_CAP_BYTES = 65536


#: `python2.7`. Checked ALONGSIDE (never instead of, never by editing)
#: `_write_bump_sink_shapes._PYTHON_C_FLAG_INTERPRETERS`, which
_VERSIONED_PYTHON_BASENAME_RE = re.compile(r"^python[23]?(\.\d+)?$")

#: Interpreter flags that consume a SEPARATE following token as their value
_PYTHON_VALUE_TAKING_FLAGS = frozenset({"-W", "-X", "-Q"})


def _python_head_script_operands(cmd: str) -> List[str]:
    """Every script-file operand of a bare `python`/`python3 <script.py>`
    invocation found at depth 0 of `cmd`, in left-to-right encounter order
    across ALL matching depth-0 segments -- a chained command running two
    scripts (`python a.py && python b.py`) yields both, not just the first.

    Reuses `_command_tokenizer.resolve_command_positions` -- the package's
    one resolve-once tokenizer -- and `_write_bump_sink_shapes._PYTHON_C_
    FLAG_INTERPRETERS` for head-verb identity, mirroring `_write_bump_sink_
    shapes._iter_python_dash_c_payloads`'s own depth-0-only, fail-open
    walk. No new tokenizer, no new interpreter set -- `_VERSIONED_PYTHON_
    BASENAME_RE` above is an ADDITIONAL local check, not an edit to that
    shared table.

    Deliberately narrow: a segment carrying a `-c` flag (an inline payload,
    already covered by `extract_interpreter_payload_write_sink_targets`
    above) or more than one non-flag positional argument (after consuming
    each `_PYTHON_VALUE_TAKING_FLAGS` flag's own value token) is not this
    shape and contributes nothing for that segment -- ambiguity here
    resolves toward "not a scratchpad script", never toward guessing which
    operand is the script.
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
        return []

    operands: List[str] = []
    for seg in segments:
        if seg.depth != 0 or seg.confidence == ResolutionConfidence.UNRESOLVED:
            continue
        tokens = seg.tokens
        if not tokens:
            continue
        head_base = normalize_executable_basename(tokens[0])
        if (
            head_base not in _PYTHON_C_FLAG_INTERPRETERS
            and not _VERSIONED_PYTHON_BASENAME_RE.match(head_base)
        ):
            continue
        args = tokens[1:]
        if any(a == "-c" or a.startswith("-c") for a in args):
            continue
        positional = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in _PYTHON_VALUE_TAKING_FLAGS:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            positional.append(a)
        if len(positional) == 1:
            operands.append(positional[0])
    return operands


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

    A substring pre-filter (`"python" not in cmd.lower()`) is checked before
    any tokenizing, so the overwhelming majority of commands -- which do not
    invoke a Python interpreter at all -- never pay for
    `resolve_command_positions`. Pure text rejection, and case-INSENSITIVE
    on purpose: Windows PATH/`cmd.exe` resolution is case-insensitive
    (`normalize_executable_basename`'s own docstring), so `Python3 script.py`
    and `PYTHON3 script.py` are real, executable invocations this filter
    must not silently drop ahead of the tokenizer ever seeing them. Still
    pure text rejection: it can only return `[]` early, never open a new
    detection path, and costs no extra `resolve_command_positions` pass --
    `record_write_claims`'s two loops (this one and the common
    `_iter_write_sink_candidates` path) between them still make exactly two
    tokenizer passes per call, not three.

    Exactly one existence/stat probe plus one bounded read
    (`_SCRATCHPAD_SCRIPT_READ_CAP_BYTES`) PER matching operand, on this
    branch only, never a directory walk: a `.py` operand outside the
    scratchpad, a nonexistent or unreadable file, or a file over the size
    cap all contribute nothing for that operand, no exception ever escapes.
    """
    try:
        if "python" not in cmd.lower():
            return []

        from coordinator_core.bash_guards._write_bump_applicability import (
            _all_temp_roots,
        )
        from coordinator_core.bash_guards._write_bump_sink_shapes import (
            _python_write_targets_in_text,
        )

        temp_roots = _all_temp_roots()
        all_targets: List[str] = []
        for operand in _python_head_script_operands(cmd):
            if not operand or not operand.lower().endswith(".py"):
                continue

            candidate = (
                operand if os.path.isabs(operand) else os.path.join(root, operand)
            )
            candidate = os.path.normpath(candidate)

            if not any(_is_within(candidate, r) for r in temp_roots):
                continue

            try:
                if not os.path.isfile(candidate):
                    continue
                if os.path.getsize(candidate) > _SCRATCHPAD_SCRIPT_READ_CAP_BYTES:
                    continue
                with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(_SCRATCHPAD_SCRIPT_READ_CAP_BYTES)
            except OSError:
                continue

            all_targets.extend(_python_write_targets_in_text(text))

        return all_targets
    except Exception:
        return []


_READ_HEAD_VERBS = frozenset({"cat", "head", "tail", "sed", "less"})

#: Flags that consume a SEPARATE following token as their value rather than
_READ_VALUE_TAKING_FLAGS_BY_VERB = {
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "sed": frozenset({"-e", "-f"}),
}


def _is_literal_read_token(token: str) -> bool:
    if not token:
        return False
    return not any(ch in token for ch in "$`*?[]~")


def resolve_read_targets(command_text: str) -> List[str]:
    """C1: every literal read-target path `command_text` names, in the
    bounded shapes `cat P`, `head P`, `tail P`, `sed -n ... P`, `less P` --
    the read-shape twin of this module's own write-shape extractor
    (`_iter_write_sink_candidates`), living beside it rather than in a new
    module (see this chunk's own plan body). Pure by construction: reads
    only `command_text`, performs no I/O, does no filesystem probe or
    containment check of its own -- that is the caller's job, exactly as it
    already is for every candidate `_iter_write_sink_candidates` yields.

    Reuses `_command_tokenizer.resolve_command_positions` -- the package's
    one resolve-once tokenizer, the same seam `_python_head_script_operands`
    above already walks -- rather than inventing a second dialect. Only
    depth-0, RESOLVED segments are consulted; an `UNRESOLVED` segment (an
    unparseable quote, a construct past `_MAX_RESOLVE_DEPTH`) contributes
    nothing, the same fail-toward-nothing posture every other extractor in
    this module already takes.

    A target reached through a variable, a glob, a subshell, or a wrapper
    script is NOT resolved -- the moment ANY positional token in a segment
    fails `_is_literal_read_token`, that WHOLE segment contributes nothing,
    rather than resolving the literal tokens around it and silently
    dropping just the one that failed: a partial result here would read as
    "these are all the reads", which is the guessed-target failure this
    function exists to avoid.

    `sed`'s own shape is asymmetric from the other four verbs: its first
    positional token is the edit SCRIPT (`'1,40p'`), never a file, and is
    always dropped; any positional after it is a file operand. An `sed -i`
    invocation is a WRITE, not a read, and is deliberately never resolved
    here -- `_iter_write_sink_candidates` already owns that shape, and a
    path claimed by both extractors would double-claim it.

    Never raises: any tokenizer failure yields `[]`, the same posture as
    `_python_head_script_operands` immediately above.
    """
    from coordinator_core.bash_guards._command_tokenizer import (
        ResolutionConfidence,
        normalize_executable_basename,
        resolve_command_positions,
    )

    try:
        segments = resolve_command_positions(
            command_text, preserve_windows_backslashes=(os.name == "nt")
        )
    except Exception:
        return []

    targets: List[str] = []
    for seg in segments:
        if seg.depth != 0 or seg.confidence == ResolutionConfidence.UNRESOLVED:
            continue
        tokens = seg.tokens
        if not tokens:
            continue
        head_base = normalize_executable_basename(tokens[0])
        if head_base not in _READ_HEAD_VERBS:
            continue
        args = tokens[1:]
        if head_base == "sed" and any(a in ("-i", "--in-place") for a in args):
            continue

        value_taking_flags = _READ_VALUE_TAKING_FLAGS_BY_VERB.get(
            head_base, frozenset()
        )
        positional: List[str] = []
        skip_next = False
        literal = True
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in value_taking_flags:
                skip_next = True
                continue
            if a.startswith("-") and a not in ("-", "--"):
                continue
            if not _is_literal_read_token(a):
                literal = False
                break
            positional.append(a)
        if not literal or not positional:
            continue

        if head_base == "sed":
            positional = positional[1:]

        targets.extend(positional)

    return targets


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
        from coordinator_core.session.touch_record import KIND_WRITE, append_touch_claims

        rels = []
        skip_next = False
        for resolved_target, head_base, raw_target in _iter_write_sink_candidates(
            cmd, root
        ):
            if skip_next:
                skip_next = False
                continue
            if _is_bare_heredoc_opener(raw_target):
                skip_next = True
                continue
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

        # KIND_WRITE: every target reaching here came from a write-shaped
        append_touch_claims(rels, session_id, root, kind=KIND_WRITE)
    except Exception:
        return
