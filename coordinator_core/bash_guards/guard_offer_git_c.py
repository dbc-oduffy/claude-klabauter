"""coordinator_core.bash_guards.guard_offer_git_c -- ``check_offer_git_c``
(offer-git-c-over-cd.sh port), extracted out of ``dispatch_checks.py``.

Pure move (M1, 2026-07-29): this module's own logic is unchanged from its
prior home. The extraction exists so band membership (advisory-rewrite) is
answerable by reading a file rather than by locating one function inside a
~5500-line confinement-heavy module -- see ``dispatch.py``'s ``guard_chain``
band-model comment and DoC-C9's own O19 rationale.

Shared helpers this module still imports from ``dispatch_checks``/
``_command_tokenizer`` rather than duplicating (this file's own private
helpers below are used ONLY by ``check_offer_git_c`` and each other):
``_crlf_strip``, ``_override``, ``_deny``, ``_allow_rewrite``,
``_strip_q``/``_strip_leading_env_and_wrappers``/
``_skip_leading_env_and_wrappers_idx`` (also consumed by
``check_runaway_find``, which stays behind), and the package's shared
tokenizer (``tokenize_full_command``/``token_matches_binary``).

Spec backlink: docs/plans/2026-07-29-bash-guard-merged-execution-shape.md M1
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _allow_rewrite,
    _crlf_strip,
    _deny,
    _override,
    _skip_leading_env_and_wrappers_idx,
    _strip_leading_env_and_wrappers,
    _strip_q,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._command_tokenizer import (
    find_git_segment as _bt_find_git_segment,
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)


def _offer_strip_q(t: str) -> str:
    return _strip_q(t)


def _offer_trim(t: str) -> str:
    return t.strip()


def _offer_normalize_path(p: str) -> str:
    if not p:
        return ""
    p = os.path.expanduser(p) if p.startswith("~") else p
    m = re.match(r"^([A-Za-z]):[/\\](.*)", p)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        p = "/%s/%s" % (drive, rest)
    try:
        return str(Path(p).resolve())
    except OSError:
        return p.rstrip("/") or "/"


def _offer_quote_aware_segments(buf: str) -> List[str]:
    """Split ``buf`` on top-level ``&&``/``;`` separators only, ignoring any
    that fall inside a single- or double-quoted span (backslash-escaping
    honored inside double quotes, not inside single quotes -- POSIX shell
    semantics). Mirrors the quote-tracking state machine in
    ``_offer_awk_parse`` below so the two never disagree about where a
    command's top-level separators actually are.

    This closes the quoted-semicolon hole: the naive ``re.split(r"&&|;",
    cmd)`` this replaces does not know a `;` inside `"..."` is quoted, so a
    command like `cd DIR && git commit -m "fix; thing"` was split into a
    truncated `git commit -m "fix` segment with an odd (=1) double-quote
    count. The odd-count guard immediately below existed to bail out on a
    genuinely malformed command, but it could not tell that shape apart from
    this one, so it silently bailed (returned ``None``, no rewrite offered
    and no deny raised) on a perfectly well-formed `cd && git` command --
    letting it fall through unrecognized to whatever guard is registered
    behind `offer-git-c` in the dispatch chain instead of being rewritten or
    denied here."""
    segments: List[str] = []
    n = len(buf)
    i = 0
    start = 0
    in_sq = in_dq = False
    while i < n:
        c = buf[i]
        if in_sq:
            if c == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            if c == "\\" and i < n - 1:
                i += 2
                continue
            if c == '"':
                in_dq = False
            i += 1
            continue
        if c == "'":
            in_sq = True
            i += 1
            continue
        if c == '"':
            in_dq = True
            i += 1
            continue
        if c == "&" and i + 1 < n and buf[i + 1] == "&":
            segments.append(buf[start:i])
            i += 2
            start = i
            continue
        if c == ";":
            segments.append(buf[start:i])
            i += 1
            start = i
            continue
        i += 1
    segments.append(buf[start:])
    return segments


#: Characters a shlex punctuation-token can be made of, per
#: `tokenize_full_command`'s own `punctuation_chars=";&|"` -- used below to
#: recognize a separator token in its output. Deliberately NOT a regex (no
#: new parsing primitive): membership against this set is a property check
#: on a token that `_bt_tokenize_full_command` (shlex, already quote-aware)
#: has already produced, not a re-scan of raw command text.
_OFFER_SEP_TOKEN_CHARS = frozenset(";&|")


def _offer_anchor_followers(followers: str, qt: str) -> Tuple[str, Optional[List[str]]]:
    """Splice ``-C <qt>`` into every top-level follower segment (the text
    starting at the separator right after the first git segment -- ``&& git
    B ; git C`` and so on) that is itself a git invocation. Returns
    ``(rewritten_followers, unanchored)``.

    Segmentation and quoting are delegated ENTIRELY to the package's shared
    tokenizer (`_bt_tokenize_full_command`, shlex-based) and its
    boundary-anchored identity matcher (`_bt_token_matches_binary`) -- this
    function does no character-level quote/separator scanning of its own.
    That is deliberate: this package's own history is repeated quote-blind
    matchers found and fixed one at a time, and `dispatch_checks.py`
    already carries an explicit warning against growing a third (now
    fourth) independent copy of that scanning logic. Reuse the one the
    confinement band already owns and tests, don't add a peer to it.

    ``unanchored`` is ``None`` when `_bt_tokenize_full_command` itself fails
    (unterminated quote / trailing backslash) -- the same fail-closed
    contract every other consumer of that tokenizer already honors: give up
    and hand `followers` back verbatim rather than guess at a boundary.
    Otherwise ``unanchored`` lists the exact (token-rejoined) text of every
    follower segment that was NOT a bare git invocation and therefore was
    left untouched; an empty list means every segment anchored.

    Provable-equivalence argument (this is what makes rung-A safe here, and
    the boundary the next reader must not push past): ``cd T && git A &&
    git B`` and ``git -C T A && git -C T B`` run every command against the
    same repository either way -- ``-C T`` and an ambient cwd of ``T`` are
    the same input to git's own path resolution, and since every segment is
    itself ``git``, no OTHER command's relative-path resolution is ever in
    play. That argument breaks the moment a segment is not ``git`` -- e.g.
    ``ls subdir/`` resolves ``subdir/`` against cwd, and cwd after dropping
    the leading ``cd`` is the ORIGINAL cwd, not ``T``. A non-git segment is
    therefore never anchored here and is reported back via ``unanchored`` so
    the caller can name it explicitly rather than silently anchoring it
    (wrong) or silently leaving it unremarked (the original defect this
    function exists to close). Do not extend the anchoring to non-git
    segments -- there is no general relative-path rewrite that is safe
    without knowing what each command does with its arguments.

    A segment carrying its own env-assignment/wrapper prefix (``FOO=1 git
    ...``) is treated as non-git for this purpose too, for free: its FIRST
    token is ``FOO=1``, not a git spelling, so `_bt_token_matches_binary`
    already rejects it -- the same prefix-is-not-provably-inert reasoning
    that keeps the leading ``cd``/first-``git`` prefix case out of
    auto-rewrite above applies identically here, with no extra code.
    """
    tokens = _bt_tokenize_full_command(followers)
    if tokens is None:
        return (followers, None)

    groups: List[Tuple[Optional[str], List[str]]] = []
    current: List[str] = []
    pending_sep: Optional[str] = None
    for tok in tokens:
        if tok and set(tok) <= _OFFER_SEP_TOKEN_CHARS:
            if current:
                groups.append((pending_sep, current))
            pending_sep = tok
            current = []
            continue
        current.append(tok)
    if current:
        groups.append((pending_sep, current))

    pieces: List[str] = []
    unanchored: List[str] = []
    for sep, seg_tokens in groups:
        if sep is None or not seg_tokens:
            # `followers` always starts at a separator, and a separator
            # never immediately repeats in a well-formed command (that
            # invariant is `_offer_awk_parse`'s own TAIL contract) -- either
            # one breaking means the input isn't the shape this function
            # was handed to anchor. Same fail-closed exit as a tokenizer
            # failure: give up, don't guess.
            return (followers, None)
        if _bt_token_matches_binary(seg_tokens[0], "git"):
            rest = " ".join(shlex.quote(t) for t in seg_tokens[1:])
            body = "%s -C %s" % (seg_tokens[0], qt)
            if rest:
                body = "%s %s" % (body, rest)
            pieces.append("%s %s" % (sep, body))
        else:
            seg_text = " ".join(seg_tokens)
            pieces.append("%s %s" % (sep, " ".join(shlex.quote(t) for t in seg_tokens)))
            unanchored.append(seg_text)

    return (" ".join(pieces), unanchored)


def check_offer_git_c(
    cmd: str,
    session_id: str = "",
    cwd: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    original_cmd = cmd
    cmd = cmd.replace("\\\n", ";")

    if not re.search(r"\bcd\b", cmd) or not re.search(r"\bgit\b", cmd):
        return None
    if _override("COORDINATOR_ALLOW_CD_PREFIX"):
        return None

    _cd_note = operator_override_note(
        "COORDINATOR_ALLOW_CD_PREFIX", payload=payload, git_root=cwd or None
    )
    _cd_note_suffix = (" " + _cd_note) if _cd_note else ""

    segments = _offer_quote_aware_segments(cmd)
    seg_count = sum(1 for s in segments if s.strip())

    seg0 = seg1 = ""
    n = 0
    for line in segments:
        t = _offer_trim(line)
        if not t:
            continue
        n += 1
        if n == 1:
            seg0 = t
        elif n == 2:
            seg1 = t
            break

    seg0_stripped = _strip_leading_env_and_wrappers(seg0)
    prefix0 = seg0[:len(seg0) - len(seg0_stripped)]
    seg1_stripped = _strip_leading_env_and_wrappers(seg1)

    # `prefix1` and `git_prefix` (below) used to be two independently-
    # derived walks over the same substring -- this one over `seg1` (itself
    # produced by `_offer_quote_aware_segments`'s own top-level split), the
    # other via `_offer_awk_parse`'s raw-text `PREFIX` walk over the whole
    # `cmd`. `find_git_segment` (`_command_tokenizer.py`, landed by M2) is
    # the single quote-aware walk that now supplies both -- see that
    # function's own docstring, which names this exact two-derivation shape
    # as the drift risk it exists to remove. It returns its `prefix`
    # unstripped (confirmed by its own test,
    # `test_prefix_captures_leading_cd_and_wrapper`), so this call site does
    # the `lstrip()` the old `_offer_awk_parse`'s `PREFIX` did.
    # `git_body`/`followers` stay on `_offer_awk_parse` -- see the comment
    # at that call site below for why they are not folded into this walk
    # too.
    _git_seg = _bt_find_git_segment(cmd)
    if _git_seg:
        prefix1 = _git_seg.get("prefix", "").lstrip()
    else:
        # Bare-newline case `find_git_segment` cannot walk past (see the
        # comment at its other call site below) -- fall back to the
        # segment-split derivation, which does not depend on it.
        prefix1 = seg1[:len(seg1) - len(seg1_stripped)]
    has_prefix = bool(prefix0) or bool(prefix1)

    if not (seg0_stripped.split() and seg0_stripped.split()[0] == "cd"):
        return None
    if not re.match(r"^cd\s+\S", seg0_stripped):
        return None
    if not re.match(r"^git\s", seg1_stripped):
        return None

    for s in (seg0, seg1):
        dq = s.count('"')
        sq = s.count("'")
        if dq % 2 == 1 or sq % 2 == 1:
            return None

    target = _offer_trim(seg0_stripped[2:])
    target = _offer_strip_q(target)
    if target.startswith("-"):
        return None

    # Expand a leading `~` BEFORE quoting. `cd ~/X/peer` works because the
    # shell expands the tilde; `shlex.quote` below then emits `'~/X/peer'`,
    # and a QUOTED tilde is never expanded -- git receives the literal four
    # characters and dies with "cannot change to '~/X/peer'", naming a path
    # the operator did read as valid. Expansion happens here, on the
    # unquoted token, so the suggestion carries the real absolute path.
    # Only a leading `~`/`~user` is touched; `os.path.expanduser` returns
    # the input unchanged when the user is unresolvable, which correctly
    # leaves an unexpandable token alone rather than fabricating a home.
    if target.startswith("~"):
        target = os.path.expanduser(target)

    # Always shell-quote `target`, not only when it contains whitespace --
    # an unquoted Windows path with backslashes (e.g. `C:\Users\x\tmp`) is
    # de-escaped by the shell that later runs this suggestion (backslash is
    # an escape character to POSIX bash, which is what git-bash's `bash.exe`
    # is), silently collapsing to `C:Usersxtmp` and producing a confusing
    # "cannot change to" error that names a path the operator never typed.
    # `shlex.quote` leaves an already-safe token (no whitespace, no
    # backslash, no other shell-special character) completely unquoted, so
    # every existing plain-path suggestion is byte-identical to before.
    qt = shlex.quote(target)

    # `git_body`/`followers` stay on `_offer_awk_parse` deliberately --
    # `find_git_segment`'s separator set (`;`/`&`/`|`, any run) is wider
    # than `_offer_awk_parse`'s (`;`/`&&`/newline only, matching
    # `_offer_quote_aware_segments`'s own separator set above). Consuming
    # `find_git_segment`'s `body`/`tail` here would silently truncate a
    # trailing `&` (background) or `|`/`||` tail into `followers` instead
    # of `git_body` -- e.g. `cd X && git status &` would lose the `&` from
    # the rewrite suggestion entirely, changing what the offered command
    # actually does. Only `prefix`/`git_prefix` -- the substring this
    # chunk's brief actually names -- come from the single `find_git_segment`
    # walk above; `BODY`/`TAIL` keep the narrower, already-consistent
    # separator set.
    parsed = _offer_awk_parse(cmd)
    git_body = parsed.get("BODY", "")
    followers = parsed.get("TAIL", "")
    git_prefix = prefix1

    # `anchored_followers`/`unanchored` anchor EVERY follower segment that is
    # itself a bare `git ...` invocation with `-C <target>` -- see
    # `_offer_anchor_followers` for the provable-equivalence argument. When
    # `unanchored` comes back empty, every follower is git and the whole
    # chain is safe to auto-rewrite (rung A); when it doesn't, the entries
    # name exactly which follower segment(s) do NOT run at the cd target, so
    # even the rung-B offer text below is never silently wrong about them.
    if followers:
        anchored_followers, unanchored = _offer_anchor_followers(followers, qt)
    else:
        anchored_followers, unanchored = "", []

    if git_body:
        git_args = git_body[3:] if git_body.startswith("git") else git_body
        suggestion = "%sgit -C %s%s%s" % (git_prefix, qt, git_args, anchored_followers)
    else:
        gitrest = _offer_trim(seg1_stripped[3:]) if seg1_stripped.startswith("git") else _offer_trim(seg1_stripped)
        suggestion = "%sgit -C %s %s" % (prefix1, qt, gitrest)

    if seg_count >= 3 and not followers:
        return None

    ml_bail = "\n" in original_cmd

    # A leading env-assignment/wrapper prefix on the `cd` segment itself
    # (`prefix0`, e.g. `FOO=1 cd X && git Y`) never gets auto-applied --
    # `prefix0` is semantically inert past the `cd` command in POSIX shells
    # (a `VAR=val simple-command` assignment scopes only to that one
    # command) so it is technically safe to drop when the `cd` itself is
    # replaced wholesale, but relying on that distinction inside an
    # auto-rewrite is exactly the kind of cleverness that reintroduces a
    # silent-drop bug later. `prefix0` therefore always falls through to
    # `_deny` below with the prefix-preserving suggestion already embedded,
    # so the operator applies it themselves rather than the guard silently
    # mutating a prefixed command. Do not promote this case to rung A -- if
    # this restraint reads as wrong, that is a finding to report, not act
    # on.
    #
    # `prefix1` (between the separator and `git`, e.g. `cd X && nice -19
    # git Y`) is a different case, and IS promoted to rung A below when
    # `prefix0` is absent. `prefix1` is meant to apply to the git
    # invocation and it still does after the rewrite: a wrapper/env-
    # assignment prefix scopes to the single command it precedes, and that
    # command is `git` either way -- `nice -19 git -C X Y` wraps the exact
    # same process `nice -19 git Y` did, because `-C X` and an ambient cwd
    # of `X` are the same input to git's own path resolution (the same
    # argument `_offer_anchor_followers` makes one position over, for
    # anchoring bare-git followers). `suggestion` above already carries
    # `prefix1` forward verbatim, so no new rewrite text is needed here --
    # only the gate has to widen.
    prefix0_present = bool(prefix0)
    prefix1_only = bool(prefix1) and not prefix0_present
    carried = prefix1.strip()

    if not has_prefix or prefix1_only:
        if not followers:
            if not ml_bail:
                if prefix1_only:
                    note = (
                        "Auto-rewritten: 'cd %s && %s git' -> '%s git -C %s' "
                        "(prompt-free; the wrapper/env prefix on the git "
                        "segment scopes to that command either way, so it "
                        "carries forward verbatim)."
                        % (target, carried, carried, target)
                    ) + _cd_note_suffix
                else:
                    note = (
                        "cd+git stalls; auto-rewritten."
                    ) + _cd_note_suffix
                return _allow_rewrite(suggestion, note)

        # Every follower is itself a bare `git ...` invocation -- anchor
        # ALL of them and auto-rewrite the whole chain, same rung-A bar as
        # the no-follower case above. This is the provably-equivalent case
        # named in the docstring on `_offer_anchor_followers`: do not widen
        # it to cover a non-git follower. `unanchored == []` (not a bare
        # `not unanchored`) is deliberate -- `unanchored` is `None` when
        # `_offer_anchor_followers` itself couldn't tokenize `followers`,
        # and `not None` is also `True` in Python, which would silently
        # treat "couldn't tell" as "fully anchored" and auto-rewrite an
        # unverified chain.
        elif followers and unanchored == [] and not ml_bail:
            if prefix1_only:
                note = (
                    "Auto-rewritten: 'cd %s && %s git ... && git ...' -> "
                    "'%s git -C %s ... && git -C %s ...' (every follower is "
                    "itself a git invocation, and the wrapper/env prefix on "
                    "the first one carries forward verbatim -- prompt-free)."
                    % (target, carried, carried, target, target)
                ) + _cd_note_suffix
            else:
                note = (
                    "Auto-rewritten: 'cd %s && git ... && git ...' -> "
                    "'git -C %s ... && git -C %s ...' (every follower is itself "
                    "a git invocation, so anchoring each one is equivalent to "
                    "the original 'cd' -- prompt-free)." % (target, target, target)
                ) + _cd_note_suffix
            return _allow_rewrite(suggestion, note)

    if not has_prefix:
        if cwd and git_body:
            norm_target = _offer_normalize_path(target)
            norm_cwd = _offer_normalize_path(cwd)
            if norm_target and norm_cwd and norm_target == norm_cwd:
                if ml_bail:
                    return None
                stripped_cmd = "%s%s" % (git_body, followers)
                return _allow_rewrite(
                    stripped_cmd,
                    (
                        "Auto-rewritten: leading 'cd %s' stripped (cwd "
                        "already matches target; followers unchanged)." % target
                    )
                    + _cd_note_suffix,
                )

    residual_note = ""
    if unanchored:
        residual_note = (
            "\n\nNot anchored -- runs at your ORIGINAL cwd, not '%s', "
            "because it is not itself a 'git' invocation (a relative path "
            "in it would resolve against the wrong directory if you paste "
            "the suggestion above as-is): %s"
            % (target, "; ".join("'%s'" % s for s in unanchored))
        )

    return _deny(
        (
            "Use 'git -C <path>' instead of a 'cd <path> && git ...' prefix.\n\n"
            "A leading 'cd' makes this a compound command, which trips a "
            "permission prompt that renders as a non-returning 'Waiting...' — "
            "the stall that gets misread as a flaky channel and drives the "
            "probe-spray loop (docs/wiki/tool-output-flakiness-protocol.md § "
            "Not this protocol — blocked / no-return). 'git -C' is the exact, "
            "prompt-free equivalent.\n\n"
            "Did you mean:\n  %s\n%s\n\n"
            "Note: the follower commands after the first ';' / '&&' / newline "
            "no longer run with '%s' as cwd. If a follower references a "
            "relative path that was anchored at the cd target, prefix the path "
            "with '%s/'." % (suggestion, residual_note, target, target)
        )
        + _cd_note_suffix
    )


def _offer_awk_parse(buf: str) -> Dict[str, str]:
    """Port of the quote-aware awk BODY/TAIL extractor in
    offer-git-c-over-cd.sh -- finds the git segment body immediately after
    the leading `cd <path> <op>` and the verbatim tail starting at the next
    unquoted `&&`/`;`/newline. Also returns PREFIX: any env-assignment/
    wrapper-word text found between the separator and `git` (e.g. the
    `FOO=1 ` in `cd X && FOO=1 git ...`), verbatim, so a caller building a
    "did you mean" suggestion can carry it forward rather than silently
    dropping it."""
    n = len(buf)
    i = _skip_leading_env_and_wrappers_idx(buf, 0)
    if buf[i:i + 2] != "cd":
        return {}
    i += 2
    in_sq = in_dq = False
    found = False
    while i < n:
        c = buf[i]
        if in_sq:
            if c == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            if c == "\\" and i < n - 1:
                i += 2
                continue
            if c == '"':
                in_dq = False
            i += 1
            continue
        if c == "'":
            in_sq = True
            i += 1
            continue
        if c == '"':
            in_dq = True
            i += 1
            continue
        if c == "&" and i + 1 < n and buf[i + 1] == "&":
            i += 2
            found = True
            break
        if c in (";", "\n"):
            i += 1
            found = True
            break
        i += 1
    if not found:
        return {}
    prefix_start = i
    i = _skip_leading_env_and_wrappers_idx(buf, i)
    if buf[i:i + 3] != "git":
        return {}
    git_prefix = buf[prefix_start:i].lstrip()
    seg1_start = i
    i += 3
    in_sq = in_dq = False
    tail_start = -1
    while i < n:
        c = buf[i]
        if in_sq:
            if c == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            if c == "\\" and i < n - 1:
                i += 2
                continue
            if c == '"':
                in_dq = False
            i += 1
            continue
        if c == "'":
            in_sq = True
            i += 1
            continue
        if c == '"':
            in_dq = True
            i += 1
            continue
        if c == "&" and i + 1 < n and buf[i + 1] == "&":
            tail_start = i
            break
        if c in (";", "\n"):
            tail_start = i
            break
        i += 1
    if tail_start > 0:
        body = buf[seg1_start:tail_start]
        tail = buf[tail_start:]
    else:
        body = buf[seg1_start:]
        tail = ""
    return {"BODY": body, "TAIL": tail, "PREFIX": git_prefix}
