"""
coordinator_core.ops.ceremony.commit_message -- native port of the deleted
wsc-commit.sh commit-message composer + dual path-set computation.

Purpose: reconstructs, byte-for-byte, the message-composition logic the OLD
`wsc-commit.sh` (recovered for behavior from `DoE:85006468^:coordinator/bin/wsc-commit.sh`,
since deleted by the 2026-07-15 kill) implemented in bash. Pure functions only --
no I/O, no subprocess, no git. This is the C2 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Message shape (subject + optional prose + optional Deleted/Kept Step-2.67 blocks +
optional footer):

    <subject>

    <prose>

    Deleted (Step 2.67):
    <path>
    ...

    Kept (Step 2.67):
    <path> -- <reason>
    ...
    --- end Step 2.67 blocks ---

The blank lines are NOT a fixed template -- each one is independently conditional
on which of {prose, deleted block, kept block} are present (four separate
conditionals in the original bash, preserved here as four separate `if`s rather
than collapsed into a single f-string, so a future edit to one conditional cannot
silently perturb an unrelated one):

  1. blank line after subject      iff prose OR any block is present
  2. prose body                    iff prose is present
  3. blank line after prose        iff prose AND (any block) are BOTH present
  4. blank line between blocks     iff BOTH Deleted and Kept blocks are present

The optional trailer block (git-trailer convention, e.g. "Nature: ..." /
"Plan: ..." lines) is appended at the very end, separated by its own blank line,
when supplied. This module does NOT derive the trailer text itself -- callers
obtain it from the existing `commit.anchors` COMPUTE_ONLY op and pass the
resulting `trailers` string straight through (do NOT re-derive trailers here;
see `coordinator_core/ops/commit_anchors.py`).

Also ports the dual path-set computation and the `--swept-rename` CLI value
parser, both load-bearing for the explicit-pathspec commit (AC5) and the
gate-scoping rule (skip-gate-when-empty):

    gate_paths   = commit_paths_in + deleted_paths      (EM-authored only)
    commit_paths = gate_paths + swept_srcs + swept_dsts (full rename included)

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C2 (AC4).

Negative-spec (hard-won, preserved from the bash original):
  - Does NOT re-derive git trailers -- `commit.anchors` is the sole trailer source.
  - Does NOT collapse the four blank-line conditionals into one -- they are
    independently triggered (a message with prose but no blocks does not get the
    inter-block blank line; a message with only a Deleted block gets no
    prose-to-block blank line).
  - Does NOT touch the filesystem, git, or any subprocess -- pure string
    functions only; C4's `commit_pipeline` module owns writing the composed
    message to the PID-scoped temp file and issuing the commit.
  - Does NOT allow a `--swept-rename` value with zero, or more than one, `|`
    separator, or an empty src/dst -- all four are rejected exactly as the bash
    original rejected them (exit 2 there; `SweptRenameError` here).
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

EM_DASH_SEPARATOR = " — "

_STEP267_FOOTER = "--- end Step 2.67 blocks ---"

_DELETED_HEADER = "Deleted (Step 2.67):"
_KEPT_HEADER = "Kept (Step 2.67):"


#: whether an appended trailer/token must join a message's EXISTING trailer
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")


def _ends_with_trailer_block(message: str) -> bool:
    stripped = message.rstrip("\n")
    if not stripped:
        return False
    lines = stripped.split("\n")
    block: List[str] = []
    reached_start = True
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if line.strip() == "":
            reached_start = False
            break
        block.append(line)
    if not block:
        return False
    if reached_start:
        return False
    return all(_TRAILER_LINE_RE.match(ln) is not None for ln in block)


def _split_trailer_tail(text: str) -> Tuple[str, str]:
    """Split `text` into `(body, trailer_tail)`, detaching a trailing run of
    "Key: value" lines (git-trailer-shaped) that forms `text`'s own LAST
    paragraph, iff that run is preceded by a blank line (i.e. is genuinely a
    separate trailing paragraph, not the whole of `text` from its start --
    same "reached_start" carve-out as `_ends_with_trailer_block`, so a bare
    subject/prose line that happens to look like "Key: value" is never
    misread as a detachable trailer).

    Purpose: `compose_message()` uses this ONLY when Step-2.67 blocks are
    present (`has_blocks`), to pull a trailer-shaped tail out of `prose`
    BEFORE splicing the Deleted/Kept blocks + `_STEP267_FOOTER` in after it
    -- otherwise that tail is left stranded mid-message, never the message's
    terminal paragraph, and so invisible to `git log --format=%(trailers)`
    (the exact silent-attribution-loss bug this function exists to close;
    see module docstring). When no detachable tail exists, returns
    `(text, "")` unchanged -- every other `compose_message()` call shape is
    untouched by this function's existence.

    When `text` (`prose`) is ENTIRELY
    trailer-shaped lines with no preceding blank line, the trailing non-
    blank run "reaches the start" -- the SAME shape `_ends_with_trailer_
    block`'s reached-start carve-out exists to protect (there, a message's
    SUBJECT line happening to look like "Key: value" must never be misread
    as a detachable trailer paragraph, because the subject is always the
    message's first paragraph, never a trailer one). That carve-out does
    not apply here: this function only ever sees `prose`, a free-form body
    paragraph passed separately from `subject` -- there is no subject line
    in `text` for an all-trailer `prose` to be confused with. Reproducing
    the reached-start carve-out here instead reproduced the R4 stranding
    bug for the narrower shape of a `prose` that is nothing BUT trailer
    lines: `Deliverable-Id: x` alone with `has_blocks=True` used to come
    back unchanged (`(text, "")`), leaving it stranded ahead of the
    Deleted/Kept blocks + footer instead of joining the message's terminal
    trailer paragraph. Trailers are load-bearing here (`Deliverable-Id`,
    `Session-Id` are parsed by other engine ops), so an all-trailer `prose`
    is now still detached as `("", text)` -- an empty body, matching
    `compose_message()`'s own `if prose_body:` guard which already treats a
    falsy body as "no prose body to render".
    """
    if not text:
        return text, ""
    stripped = text.rstrip("\n")
    if not stripped:
        return text, ""
    lines = stripped.split("\n")
    idx = len(lines) - 1
    while idx >= 0 and lines[idx].strip() != "":
        idx -= 1
    trailer_lines = lines[idx + 1 :]
    if not trailer_lines or not all(_TRAILER_LINE_RE.match(ln) for ln in trailer_lines):
        return text, ""
    body = "\n".join(lines[:idx]) if idx >= 0 else ""
    return body, "\n".join(trailer_lines)


class SweptRenameError(ValueError):
    pass


def _has_blocks(deleted_paths: Sequence[str], kept_entries: Sequence[str]) -> bool:
    return bool(deleted_paths) or bool(kept_entries)


def compose_message(
    *,
    subject: str,
    prose: str = "",
    deleted_paths: Sequence[str] = (),
    kept_entries: Sequence[str] = (),
    trailers: str = "",
) -> str:
    """Compose the Step-2.67 commit message, byte-reproducing the bash original.

    Purpose: the C2 AC4 port target -- see module docstring for the exact
    conditional-blank-line rules this function implements. `trailers`, when
    non-empty, is appended as a final block after its own blank-line separator
    (git-trailer convention); it is NOT part of the original bash-era format and
    has no bearing on the deleted parity test's golden fixture, which always
    passes `trailers=""`.

    Params:
        subject        -- commit subject line (no trailing newline).
        prose          -- optional free-form prose body paragraph.
        deleted_paths  -- bare repo-relative paths, one per line, for the
                           "Deleted (Step 2.67):" block.
        kept_entries   -- pre-formatted "<path> EM_DASH_SEPARATOR <reason>"
                           strings for the "Kept (Step 2.67):" block (the
                           caller composes the em-dash entry; this function
                           does not re-split path from reason).
        trailers       -- pre-derived git-trailer text (from `commit.anchors`),
                           newline-joined "Key: value" lines, or "" to omit.

    Returns the composed message as a single string, each content line
    terminated by "\\n" (matching the bash original's `msg+="...\\n"`
    accumulation), with no extraneous trailing blank line beyond what the
    conditional rules produce.
    """
    has_blocks = _has_blocks(deleted_paths, kept_entries)

    if has_blocks:
        prose_body, prose_trailer_tail = _split_trailer_tail(prose)
    else:
        prose_body, prose_trailer_tail = prose, ""

    msg = subject + "\n"

    if prose or has_blocks:
        msg += "\n"

    if prose_body:
        msg += prose_body + "\n"

    if prose_body and has_blocks:
        msg += "\n"

    if deleted_paths:
        msg += _DELETED_HEADER + "\n"
        for p in deleted_paths:
            msg += p + "\n"

    if deleted_paths and kept_entries:
        msg += "\n"

    # Kept (Step 2.67): block -- "<path> EM_DASH_SEPARATOR <reason>" per entry.
    if kept_entries:
        msg += _KEPT_HEADER + "\n"
        for e in kept_entries:
            msg += e + "\n"

    if has_blocks:
        msg += _STEP267_FOOTER + "\n"

    combined_trailers = (
        prose_trailer_tail + "\n" + trailers
        if prose_trailer_tail and trailers
        else prose_trailer_tail or trailers
    )

    if combined_trailers:
        if _ends_with_trailer_block(msg):
            base = msg if msg.endswith("\n") else msg + "\n"
            msg = base + combined_trailers + "\n"
        else:
            msg += "\n" + combined_trailers + "\n"

    return msg


def format_kept_entry(path: str, reason: str) -> str:
    """Compose a single Kept-block entry: "<path> EM_DASH_SEPARATOR <reason>".

    Purpose: centralizes the em-dash formatting so callers never hand-splice
    the separator (a common source of the wrong dash character -- hyphen
    instead of U+2014 -- being silently substituted by an editor/terminal).
    """
    return f"{path}{EM_DASH_SEPARATOR}{reason}"


def parse_swept_rename(value: str) -> Tuple[str, str]:
    if "|" not in value:
        raise SweptRenameError(f"no | separator in value: {value}")
    src, dst = value.split("|", 1)
    if not src:
        raise SweptRenameError(f"empty src in value: {value}")
    if not dst:
        raise SweptRenameError(f"empty dst in value: {value}")
    if "|" in dst:
        raise SweptRenameError(f"more than one | in value: {value}")
    return src, dst


def compute_gate_paths(
    commit_paths: Sequence[str], deleted_paths: Sequence[str]
) -> List[str]:
    return [*commit_paths, *deleted_paths]


def compute_commit_paths(
    gate_paths: Sequence[str],
    swept_srcs: Sequence[str],
    swept_dsts: Sequence[str],
) -> List[str]:
    return [*gate_paths, *swept_srcs, *swept_dsts]
