"""coordinator_core.bash_guards._write_bump_sink_shapes -- the ONE enumerated
plain-bash write-sink shape table shared by the cross-repo bump guard
(``bump_foreign_repo_write.py``, C4) and the outside-repo bump guard
(``bump_outside_repo_write.py``, C5, landing in the NEXT wave) -- named here,
by reference, so the two chunks classify the identical shape set rather than
each hand-rolling its own list that can silently drift from the other's.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C4 ("the enumerated plain-bash write-sink set is owned by chunk
C5 and shared here by reference so the two cannot drift -- put the set in one
shared helper you own now, structured so C5 imports it rather than restating
it") and chunk C5's own body (the canonical enumeration this module ports:
output redirection (`>`, `>>`), `tee`, `cp`, `mv`, `mkdir -p`, `install`,
`sed -i`, heredocs (`cat > ... <<EOF`), `rsync`, `tar -x -C`).

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY -- same posture as every other
module in this wave. This module's classification is deliberately
best-effort: a candidate path this module fails to recognise, or recognises
too broadly (e.g. `sed`'s own edit-script argument mistaken for a file
operand when it happens not to start with `-`), is HARMLESS in the fail-open
direction the calling guards already apply -- an over-broad candidate that
does not resolve to a real git repo is simply dropped by the caller's own
`resolve_gitdir` check, and an under-recognised shape just means the bump
does not fire (never a false deny). Do not "tighten" this into a strict
shell-argument parser; see the plan's Anti-scope, "do not enumerate
evasions".

Deliberately NOT covered here, per the plan's own enumeration: any shape
requiring adversarial-style interpreter indirection beyond the single inline
`-c` case AC4 names (C5's own scope, not this module's) -- out of scope by
design, not an oversight (§ Design posture, "do not enumerate evasions").

Also carries `nearest_existing_ancestor()` (lifted from C4 during C5's own
landing) -- the shared "walk up to the nearest real directory" helper both
`bump_foreign_repo_write.py` and `bump_outside_repo_write.py` need before
calling `resolve_gitdir`, since a write-sink TARGET is very often a path
that does not exist yet. See that function's own docstring for the
latent-bug history.

Negative-spec:
  - Does NOT resolve a candidate path to an absolute location, a git root, or
    anything else -- this module returns RAW candidate strings exactly as
    they appear in the tokenized command; every calling guard resolves them
    against its own notion of "current effective cwd" (which differs between
    C4's cross-repo cwd-tracking and C5's outside-repo cwd-tracking) before
    doing anything with them.
  - Does NOT itself decide whether a candidate is "foreign" or "outside" --
    purely a target-EXTRACTION helper, never a verdict.
  - Does NOT attempt to be a general shell-quoting-aware argument parser --
    callers hand this module an ALREADY-TOKENIZED segment (typically
    `_command_tokenizer.ResolvedCommand.tokens`), so quoting is someone
    else's job by the time a token stream reaches here.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

#: Redirection operator token shape shlex's tokenizer emits when `>`/`>>`
#: (optionally fd-prefixed, e.g. `2>`, `1>>`) is surrounded by whitespace --
#: this is a punctuation-adjacent WORD token (not one of this package's own
#: `;`/`&`/`|` `punctuation_chars`), so it appears as an ordinary token in a
#: `tokenize_full_command` stream and is recognised here by shape, not by a
#: dedicated tokenizer feature.
_REDIRECT_OP_RE = re.compile(r"^\d*>{1,2}$")

#: The plain-bash write-sink BINARY names this table classifies (the
#: redirection-operator shape above is handled independently of binary
#: name, since `echo x > /elsewhere/file` is a write sink regardless of
#: `echo` itself never appearing in this set). Every entry here is a
#: basename-normalized (see `_command_tokenizer.normalize_executable_
#: basename`) lowercase binary name -- callers must normalize their own
#: head token the same way before consulting this set, exactly as every
#: other binary-identity check in this package already does.
WRITE_SINK_BINARIES = frozenset(
    {"tee", "cp", "mv", "mkdir", "install", "sed", "rsync", "tar"}
)


def extract_write_sink_targets_for_segment(tokens: List[str], head_base: str) -> List[str]:
    """Raw candidate write-target strings found in ONE already-tokenized,
    already wrapper/env-peeled segment (`tokens`), given `head_base` --
    `tokens[0]`, basename-normalized by the caller (see
    `_command_tokenizer.normalize_executable_basename`).

    Two independent sources of candidates, per the module docstring:

      1. A redirection operator (`_REDIRECT_OP_RE`) anywhere in `tokens`,
         regardless of `head_base` -- the token immediately following it is
         a candidate. This covers `cat > ... <<EOF`-shaped heredoc writes
         too: `_command_tokenizer.resolve_command_positions` already strips
         heredoc BODIES upstream of this function (leaving the `<<WORD`
         opener token in place), so the `>` target this function needs is
         already sitting in `tokens` unchanged by that stripping.
      2. `head_base`'s own positional-argument shape, ONLY when `head_base`
         is a member of `WRITE_SINK_BINARIES` -- see each branch below for
         which argument position is the write target for that binary.

    Best-effort positional-argument extraction (see module docstring for why
    over-broad candidates here are harmless): a token starting with `-` is
    treated as a flag/option and never a candidate target, for every binary
    below -- this is deliberately coarse (it does not know which flags take
    a separate-token value of their own) but never UNDER-includes a real
    target for the binaries this table names, only occasionally
    OVER-includes a non-path argument (e.g. `sed`'s own edit script), which
    the caller's own git-root resolution drops harmlessly.
    """
    targets: List[str] = []

    for i, tok in enumerate(tokens):
        if _REDIRECT_OP_RE.match(tok) and i + 1 < len(tokens):
            targets.append(tokens[i + 1])

    if head_base not in WRITE_SINK_BINARIES:
        return targets

    args = tokens[1:]
    positional = [t for t in args if not t.startswith("-")]

    if head_base == "tee":
        # Every positional argument is a target file; `tee` fans out to all
        # of them (plus stdout, not a filesystem write this module cares
        # about).
        targets.extend(positional)
    elif head_base in ("cp", "mv", "install"):
        # Last positional argument is the destination -- the ordinary
        # `cp SRC... DEST` / `mv SRC DEST` / `install [-options] SRC DEST`
        # shape. A single positional argument (destination only, no source
        # named -- malformed for these binaries) is skipped rather than
        # guessed at.
        if len(positional) >= 2:
            targets.append(positional[-1])
    elif head_base == "mkdir":
        # `mkdir` can create multiple directories in one invocation; every
        # positional argument is its own target. Triggered regardless of
        # `-p` presence -- a bare `mkdir /elsewhere/dir` is exactly as
        # real a foreign-repo write as `mkdir -p /elsewhere/dir`, and the
        # plan's own enumeration ("mkdir -p") names the OBSERVED shape from
        # the two cited incidents, not an exhaustive gate on the flag.
        targets.extend(positional)
    elif head_base == "sed":
        # Only `-i` (in-place edit, optionally `-iSUFFIX` / `-i SUFFIX`)
        # turns `sed` into a write sink at all -- without it, `sed` reads
        # stdin/files and writes to stdout, never touching a target path.
        has_inplace = any(a == "-i" or a.startswith("-i") for a in args)
        if has_inplace:
            targets.extend(positional)
    elif head_base == "rsync":
        # Last positional argument is the destination, same shape as
        # `cp`/`mv` above.
        if len(positional) >= 2:
            targets.append(positional[-1])
    elif head_base == "tar":
        # Only extraction (`-x`/`--extract`, or a bundled short-flag form
        # containing `x`, e.g. `-xf`) writes to the filesystem at all; the
        # plan's own enumeration names the `-C <dir>` (or `--directory`)
        # extraction-target form specifically, not tar's own default-cwd
        # extraction (which this module deliberately does not attempt to
        # resolve, since it is not a repo-crossing shape without an
        # explicit `-C`).
        has_extract = any(
            a in ("-x", "--extract")
            or (a.startswith("-") and not a.startswith("--") and "x" in a)
            for a in args
        )
        if has_extract:
            for i, a in enumerate(args):
                if a in ("-C", "--directory") and i + 1 < len(args):
                    targets.append(args[i + 1])
                elif a.startswith("--directory="):
                    targets.append(a.split("=", 1)[1])

    return targets


def nearest_existing_ancestor(path: str) -> Optional[str]:
    """Walk `path` up to the nearest EXISTING directory ancestor, or `None`
    if none exists at all (fail open -- never raises).

    Shared by both C4 (`bump_foreign_repo_write.py`) and C5
    (`bump_outside_repo_write.py`) -- lifted out of C4's own module (where it
    first landed as a latent-bug fix) into this shared shapes module so the
    two guards do not carry two copies of the identical helper, matching the
    plan's own instruction for C5 to "reuse that approach" and, if private to
    C4, "lift it into `_write_bump_sink_shapes.py` so both guards share one
    copy".

    Latent-bug fix (originally landed in C4, restated here verbatim):
    `resolve_gitdir` shells out with `cwd=<candidate>`, which REQUIRES an
    existing directory -- a write-sink TARGET from either guard's own
    candidate-extraction loop is very often a file that does not exist yet
    (`echo x > /repo/new-file.txt`, a `cp`/`tee`/`sed -i` destination) or a
    directory `mkdir -p`/`tar -x -C` is about to create. Probing the raw
    candidate directly always fails (`NotADirectoryError`/
    `FileNotFoundError`, an `OSError` `resolve_gitdir` already swallows into
    `None`), which would silently fail-open on exactly the write-sink shapes
    AC1/AC3 name -- not a hypothetical, confirmed by direct probe against
    `resolve_gitdir` before this fix landed. Walking to the nearest real
    ancestor answers the same "which git root would this land in" question
    without requiring the leaf to already exist -- once cd'd to that
    ancestor, `git rev-parse --git-dir` resolves the identical root a real
    shell command targeting the (not-yet-existing) leaf would see.
    """
    if not path:
        return None
    candidate = os.path.normpath(path)
    seen = set()
    while candidate not in seen:
        seen.add(candidate)
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    return None
