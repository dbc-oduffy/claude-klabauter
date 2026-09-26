
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
from coordinator_core.conservatism import SafeDirection, declares_safe_direction


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


@declares_safe_direction(
    SafeDirection.FALL_BACK,
    because=(
        "offering (or auto-rewriting to) a 'git -C <target>' suggestion "
        "built from quoting this guard cannot confirm is balanced risks "
        "reconstructing a broken shell command and handing the operator a "
        "suggestion that fails differently than the command they typed; "
        "staying silent leaves the original 'cd && git' stall as the only "
        "cost, which is the exact prompt-stall this guard exists to relieve, "
        "not a new failure mode"
    ),
    anchor=lambda result: result is None,
)
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

    _git_seg = _bt_find_git_segment(cmd)
    if _git_seg:
        prefix1 = _git_seg.get("prefix", "").lstrip()
    else:
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

    if target.startswith("~"):
        target = os.path.expanduser(target)

    qt = shlex.quote(target)

    parsed = _offer_awk_parse(cmd)
    git_body = parsed.get("BODY", "")
    followers = parsed.get("TAIL", "")
    git_prefix = prefix1

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
            "the stall that gets misread as a flaky channel and drives a "
            "retry-and-retool loop (docs/wiki/tool-output-flakiness-protocol.md § "
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
