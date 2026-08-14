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

#: U+2014 EM DASH, surrounded by single spaces -- the separator between a Kept
#: path and its reason, exactly as the bash original composed it
#: (`"$p — $reason"`, three UTF-8 bytes 0xE2 0x80 0x94 for the dash itself).
EM_DASH_SEPARATOR = " — "

#: Footer line closing the Step 2.67 blocks section (present iff any block is
#: present -- see `_has_blocks`).
_STEP267_FOOTER = "--- end Step 2.67 blocks ---"

_DELETED_HEADER = "Deleted (Step 2.67):"
_KEPT_HEADER = "Kept (Step 2.67):"


#: Matches one git-trailer-shaped line ("Key: value") -- used only to decide
#: whether an appended trailer/token must join a message's EXISTING trailer
#: block rather than start a new paragraph. Shared home for this predicate
#: (moved here from `commit_pipeline.py`, staff-eng R1 F1,
#: state/review-trail/2026-08-08-landed-commit-close-review/r1-w1.md):
#: `compose_message()` ends a trailered message with exactly ONE trailing
#: "\n" (`"\n" + trailers + "\n"`), so a caller that blindly appends another
#: block after a blank line inserts a NEW paragraph -- git's trailer parser
#: (`interpret-trailers`, `%(trailers:key=...)`) recognises only the LAST
#: paragraph as trailers, so that blank line silently demotes every
#: pre-existing trailer to body prose. `commit_pipeline.commit()`'s
#: `Commit-Token:` mint relies on this same predicate to avoid repeating
#: that mistake; do not fork a second copy -- import from here.
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")


def _ends_with_trailer_block(message: str) -> bool:
    """True iff `message`'s LAST paragraph is entirely "Key: value" lines.

    Walks backward from the end, collecting the trailing run of non-blank
    lines (git's own notion of "the last paragraph") -- stops at the first
    blank line. Empty (no non-blank trailing run) is False, never
    vacuously True. Handles all five message shapes callers must
    distinguish: a trailer block (True, however the message ends -- with a
    bare "\\n", no trailing newline at all, or followed by a blank line
    would already have broken the block itself); prose-only body (False); a
    message ending in a blank line (False, no trailing non-blank run before
    that blank line's own predecessor content still gets evaluated on ITS
    run); a message with no trailing newline (handled the same as any other
    -- `str.split("\\n")` does not require a trailing newline to enumerate
    lines correctly); and a subject-only message shaped like "Key: value"
    (e.g. a conventional-commit subject "wsc: subject only") -- False,
    because that trailing run reaches the START of the message with no
    preceding blank line, so it IS the subject line, never a trailer
    paragraph. Git's own rule is that trailers live in the message's last
    paragraph and that paragraph is never the first one -- a trailing run
    that consumes the whole message cannot be a trailer block under that
    rule.
    """
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
        # The trailing non-blank run consumed the entire message -- it is
        # the subject line (first paragraph), never a trailer block.
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
    if idx < 0:
        # No preceding blank line -- the trailing non-blank run reaches the
        # start of `text`, so it is `text` itself, never a detachable tail.
        return text, ""
    trailer_lines = lines[idx + 1 :]
    if not trailer_lines or not all(_TRAILER_LINE_RE.match(ln) for ln in trailer_lines):
        return text, ""
    body = "\n".join(lines[:idx])
    return body, "\n".join(trailer_lines)


class SweptRenameError(ValueError):
    """Raised on a malformed `--swept-rename '<src>|<dst>'` CLI value.

    Purpose: mirrors the bash original's four rejection cases (no `|`, empty
    src, empty dst, more than one `|`) as a single typed exception, so callers
    in the op layer can catch one exception type and surface a usage error
    instead of letting a raw ValueError/IndexError propagate.
    """


def _has_blocks(deleted_paths: Sequence[str], kept_entries: Sequence[str]) -> bool:
    """True iff at least one of the Deleted/Kept Step-2.67 blocks is non-empty."""
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

    # Detach a trailer-shaped tail from `prose` BEFORE splicing the blocks in
    # -- only when blocks are present, since that is the only shape where the
    # blocks+footer would otherwise land between that tail and the message's
    # true end, severing it from git's trailer parser (see
    # `_split_trailer_tail`'s docstring). When no blocks follow, `prose`'s own
    # trailing paragraph -- trailer-shaped or not -- is already terminal
    # (modulo the explicit `trailers` param handling below), so this step
    # must not fire there.
    if has_blocks:
        prose_body, prose_trailer_tail = _split_trailer_tail(prose)
    else:
        prose_body, prose_trailer_tail = prose, ""

    msg = subject + "\n"

    # 1. Blank line after subject -- iff prose OR any block follows.
    if prose or has_blocks:
        msg += "\n"

    # 2. Prose body.
    if prose_body:
        msg += prose_body + "\n"

    # 3. Blank line between prose and the Step 2.67 blocks -- iff BOTH present.
    if prose_body and has_blocks:
        msg += "\n"

    # Deleted (Step 2.67): block -- one bare path per line.
    if deleted_paths:
        msg += _DELETED_HEADER + "\n"
        for p in deleted_paths:
            msg += p + "\n"

    # 4. Blank line between Deleted and Kept blocks -- iff BOTH present.
    if deleted_paths and kept_entries:
        msg += "\n"

    # Kept (Step 2.67): block -- "<path> EM_DASH_SEPARATOR <reason>" per entry.
    if kept_entries:
        msg += _KEPT_HEADER + "\n"
        for e in kept_entries:
            msg += e + "\n"

    # Footer closes the Step 2.67 blocks section -- present iff any block present.
    if has_blocks:
        msg += _STEP267_FOOTER + "\n"

    # Trailer block -- new (not in the bash original); appended last, only
    # when the caller supplied non-empty trailers, OR `prose` carried a
    # trailer-shaped tail detached above (`prose_trailer_tail`). Both are
    # joined into ONE combined trailer paragraph so a caller-embedded
    # trailer (e.g. `prose` closing with "Deliverable-Id: ...") and an
    # explicit `trailers=` value never end up as two separate paragraphs --
    # only the LAST paragraph is ever trailer-parseable, so two would demote
    # the first to body prose exactly as the blocks/footer would have.
    combined_trailers = (
        prose_trailer_tail + "\n" + trailers
        if prose_trailer_tail and trailers
        else prose_trailer_tail or trailers
    )

    # When `subject`/`prose_body` ALREADY ends in a trailer block (e.g. no
    # Step-2.67 blocks were present, so `prose`'s own trailing trailer-shaped
    # lines were never detached above), joining with a blank line would start
    # a NEW paragraph and demote that existing block to body prose for git's
    # trailer parser (see `_ends_with_trailer_block`'s docstring) -- join it
    # onto the same paragraph instead (single "\n", no blank line). Only
    # when no trailer block already terminates the message does today's
    # blank-line-separated convention apply.
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
    """Parse a `--swept-rename '<src>|<dst>'` CLI value into `(src, dst)`.

    Purpose: byte-reproduces the bash original's four validation cases (see
    `SweptRenameError`). The caller has already run `git mv src dst` before
    this value reaches the composer -- this function only validates and
    splits the string; it does not touch git or the filesystem.

    Raises `SweptRenameError` when:
        - `value` contains no `|` separator.
        - the src (text before the first `|`) is empty.
        - the dst (text after the first `|`) is empty.
        - the dst itself contains another `|` (more than one separator total).
    """
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
    """gate_paths = commit_paths + deleted_paths (EM-authored only, swept excluded).

    Purpose: the scope the deletion-block/dirty-tree gates (C3) inspect. Swept
    paths are excluded because the gate has no Step 2.67 block to validate them
    against -- they are caller-named archival residues, not EM-authored
    judgment deletions.
    """
    return [*commit_paths, *deleted_paths]


def compute_commit_paths(
    gate_paths: Sequence[str],
    swept_srcs: Sequence[str],
    swept_dsts: Sequence[str],
) -> List[str]:
    """commit_paths = gate_paths + swept_srcs + swept_dsts (full commit pathspec).

    Purpose: the explicit `-- <paths>` pathspec passed to `git commit -F`
    (AC5). Including swept src and dst folds a caller-managed archival rename
    into the commit atomically without widening the gate's inspection scope.
    """
    return [*gate_paths, *swept_srcs, *swept_dsts]
