"""coordinator_core.bash_guards._write_bump_sink_shapes -- the ONE enumerated
plain-bash write-sink shape table shared by the cross-repo bump guard
(``bump_foreign_repo_write.py``, C4) and the outside-repo bump guard
(``bump_outside_repo_write.py``, C5, landing in the NEXT wave) -- named here,
by reference, so the two chunks classify the identical shape set rather than
each hand-rolling its own list that can silently drift from the other's.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 [DoE-claude
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

PM-RATIFIED REVERSAL, 2026-08-14 (`docs/plans/2026-08-14-interpreter-body-
write-sinks.md`) -- record this, do not re-derive the old rule. This module
previously stated that interpreter indirection beyond the single inline
`-c` case was out of scope BY DESIGN ("do not enumerate evasions",
DoE-claude:pln-write-confinement-guards-cross-996567 § Design posture). That
exclusion was written against *adversarial* evasion. The reversal covers
only the ACCIDENTAL shape underneath it -- a write target that exists
solely inside a heredoc body or a `python`/`python3 -c` payload string, the
shape two independent EMs hit while holding the scratchpad rule in context,
not someone routing around this guard on purpose. See
`extract_interpreter_payload_write_sink_targets` below, a NEW,
separately-named C5-only extractor -- `extract_write_sink_targets_for_
segment` above (C4's shared table) is untouched by this reversal, and every
other clause of "do not enumerate evasions" (base64, `exec`, assembled
paths, other interpreters) still stands.

Also carries `nearest_existing_ancestor()` (lifted from C4 during C5's own
landing) -- the shared "walk up to the nearest real directory" helper both
`bump_foreign_repo_write.py` and `bump_outside_repo_write.py` need before
calling `resolve_gitdir`, since a write-sink TARGET is very often a path
that does not exist yet. See that function's own docstring for the
latent-bug history.

Also carries `translate_msys_path()` / `resolve_relative()` (C1,
`docs/plans/2026-08-07-guard-posix-path-rerooting.md`) -- the MSYS/MinGW
drive-mount path translator and its `_resolve_relative` twin, LIFTED here
from `bump_outside_repo_write.py:275` / `bump_foreign_repo_write.py:277`
(previously duplicated verbatim in both) so C2/C4 import one copy rather
than restating it, mirroring `nearest_existing_ancestor`'s own precedent
above. See each function's own docstring for the defect this fixes.

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

from ._dialect import _strip_ps_quotes
from ._command_tokenizer import (
    _HEREDOC_OPEN_RE,
    _extract_dash_c_payload,
    ResolutionConfidence,
    normalize_executable_basename,
    resolve_command_positions,
)

#: Redirection operator token shape shlex's tokenizer emits when `>`/`>>`
#: (optionally fd-prefixed, e.g. `2>`, `1>>`) is surrounded by whitespace --
#: this is a punctuation-adjacent WORD token (not one of this package's own
#: `;`/`&`/`|` `punctuation_chars`), so it appears as an ordinary token in a
#: `tokenize_full_command` stream and is recognised here by shape, not by a
#: dedicated tokenizer feature.
_REDIRECT_OP_RE = re.compile(r"^\d*>{1,2}$")

#: A plain redirect to exactly this literal token is never a write-sink
#: candidate (2026-08-13, out-of-repo write-bump `/dev/null` false positive,
#: `state/subagent-share/8d387a4c-8595-4b90-8714-f5775401fcb3/`). `/dev/null`
#: is a null sink, not a write destination -- nothing is written, nothing
#: leaves the repo, so there is no out-of-repo write to bump. Mirrors the
#: sibling carve-out already shipped on
#: `block_reviewer_bash_outside_allowlist._match_devnull_redirect`
#: (Divergence 8, 2026-07-28) on the identical reasoning, narrowed the same
#: way: EXACT match only, never a prefix or `/dev/*` pattern, and it exempts
#: only THIS ONE redirect-operator token pair from candidate extraction --
#: every other token in the same segment (another redirect to a real path, a
#: `cp`/`mv`/`tee`/etc. positional target) is still extracted and evaluated
#: normally, so `echo hi > /dev/null; cp x /elsewhere/y` still bumps on the
#: `cp` leg.
_DEVNULL_TARGET = "/dev/null"

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
            redirect_target = tokens[i + 1]
            if redirect_target == _DEVNULL_TARGET:
                # Exact `/dev/null` only -- see `_DEVNULL_TARGET`'s own
                # docstring. Every other token in this segment is still
                # scanned normally by this same loop and by the
                # `head_base`-driven branches below.
                continue
            targets.append(redirect_target)

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


#: PowerShell cmdlet-shaped write-sink table (C4e follow-up, 2026-08-07,
#: `docs/reference/guard-dialect-coverage.md` row 15). ADDITIVE to the
#: `WRITE_SINK_BINARIES` table above, not a replacement -- `cp`/`mv`/`tee`/
#: `mkdir`/`install`/`sed`/`rsync`/`tar` stay a BASH-only table exactly as
#: before; this is a SEPARATE, PowerShell-only table for the cmdlets C3's
#: triage named as the genuinely unmatched gap (row 15's own worked list):
#: `New-Item`, `Set-Content`, `Add-Content`, `Copy-Item`, `Move-Item`,
#: `Out-File`, `Tee-Object`. `cp`/`mv` PowerShell ALIASES are deliberately
#: NOT duplicated here (C3's triage: they already fire via alias collision
#: on the bash-shaped classifier reused elsewhere in this fleet); `>`/`>>`
#: are the same operator characters in both dialects and are likewise left
#: alone here, not re-derived. Every entry is a lowercased FULL cmdlet name
#: only -- short aliases (`ni`, `sc`, `ac`, `cpi`, `mi`) are NOT covered,
#: same "do not enumerate evasions" posture the rest of this module
#: applies; a caller consulting this table must lowercase its own head
#: token first, mirroring `WRITE_SINK_BINARIES`'s own basename-normalize
#: contract.
PS_WRITE_SINK_CMDLETS = frozenset(
    {"new-item", "set-content", "add-content", "copy-item", "move-item", "out-file", "tee-object"}
)


def _ps_flag_value(args: List[str], prefixes: tuple) -> Optional[str]:
    """First token immediately following a `-`-prefixed arg whose lowercased
    text starts with one of `prefixes` (PowerShell parameter PREFIX-MATCH --
    see module-level callers' own docstrings for why a literal flag name
    would have near-zero recall), or `None` if no such flag/value pair is
    present. Each prefix below is chosen to be UNAMBIGUOUS against every
    other named parameter the same cmdlet accepts (see
    `extract_write_sink_targets_powershell`'s own per-cmdlet comments)."""
    for i, tok in enumerate(args):
        low = tok.lower()
        if low.startswith("-") and any(low.startswith(p) for p in prefixes) and i + 1 < len(args):
            return _strip_ps_quotes(args[i + 1])
    return None


def extract_write_sink_targets_powershell(tokens: List[str], head_low: str) -> List[str]:
    """Raw candidate write-target strings for ONE already-tokenized
    PowerShell segment (`tokens`, from `_dialect.resolve_segments_for_dialect`),
    given `head_low` -- `tokens[0].lower()`, mirroring `extract_write_sink_
    targets_for_segment`'s own `head_base` contract but for the PowerShell
    cmdlet table (`PS_WRITE_SINK_CMDLETS`) rather than the bash binary table.

    Best-effort, same fail-open direction as the bash-leg function above: an
    over-broad candidate is dropped harmlessly by the caller's own
    `resolve_gitdir` check; an under-recognised shape just means no bump,
    never a false deny. Does NOT attempt `cd`/`Set-Location` cwd-tracking --
    that parity gap is the caller's to note, not this extraction helper's.
    """
    if head_low not in PS_WRITE_SINK_CMDLETS:
        return []

    args = tokens[1:]
    # Flag-shaped detection happens on the RAW (still-quoted) token
    # deliberately -- a quoted literal like `"-Path"` is data, not a real
    # PowerShell parameter name (quoting it is exactly how a script would
    # spell "the string `-Path`, not the flag"), so filtering BEFORE
    # stripping is the correct order: stripping first would make a quoted
    # literal indistinguishable from an actual `-Path` flag and wrongly
    # exclude it from the positional set.
    positional = [_strip_ps_quotes(t) for t in args if not t.startswith("-")]
    targets: List[str] = []

    if head_low == "new-item":
        # `-Path` (prefix `-pa`, unambiguous against `-ItemType`/`-Value`/
        # `-Force`/`-Name`) or the first positional argument.
        v = _ps_flag_value(args, ("-pa",))
        if v is not None:
            targets.append(v)
        elif positional:
            targets.append(positional[0])
    elif head_low in ("set-content", "add-content"):
        # `-Path` (`-pa`) or `-LiteralPath` (`-li`), unambiguous against
        # `-Value`/`-Encoding`/`-Force`; else first positional.
        v = _ps_flag_value(args, ("-pa", "-li"))
        if v is not None:
            targets.append(v)
        elif positional:
            targets.append(positional[0])
    elif head_low in ("copy-item", "move-item"):
        # `-Destination` (`-de`, unambiguous against `-Path`/`-Force`/
        # `-Recurse`) or the LAST positional when two-or-more are present --
        # the same `SRC... DEST` shape the bash-leg `cp`/`mv` branch uses.
        v = _ps_flag_value(args, ("-de",))
        if v is not None:
            targets.append(v)
        elif len(positional) >= 2:
            targets.append(positional[-1])
    elif head_low == "out-file":
        # `-FilePath` (`-fi`, unambiguous against `-InputObject`/`-Encoding`/
        # `-Append`/`-Force`) or the first positional -- `Out-File` accepts
        # `FilePath` positionally (position 0), including the common
        # `... | Out-File dest.txt` pipeline-tail shape.
        v = _ps_flag_value(args, ("-fi",))
        if v is not None:
            targets.append(v)
        elif positional:
            targets.append(positional[0])
    elif head_low == "tee-object":
        # `-FilePath` (`-fi`) or the first positional -- but ONLY when no
        # `-Variable` (`-va`) flag is present. `Tee-Object`'s `-FilePath`
        # and `-Variable` parameter sets are mutually exclusive and share
        # the SAME position-0 positional slot; `Tee-Object -Variable foo`
        # writes to an in-memory PowerShell variable, not the filesystem,
        # and `foo` there is `-Variable`'s own value, not a bare positional
        # `-FilePath` argument -- treating it as one would be a false
        # write-sink candidate (confirmed live: without this guard, `foo`
        # was wrongly extracted as a target).
        v = _ps_flag_value(args, ("-fi",))
        has_variable_flag = any(t.lower().startswith("-va") for t in args)
        if v is not None:
            targets.append(v)
        elif positional and not has_variable_flag:
            targets.append(positional[0])

    return targets


#: `Set-Location` and its built-in aliases (`cd`, `sl`, `chdir`) -- the
#: PowerShell-leg cwd-tracking parity fix (2026-08-07/08, backlog row
#: `2026-08-07-bump-foreign-repo-write-s-powershell-leg-3254b856d676`).
#: Lowercased full names only, matching `PS_WRITE_SINK_CMDLETS`'s own
#: "caller lowercases its head token first" contract.
PS_SET_LOCATION_ALIASES = frozenset({"set-location", "cd", "sl", "chdir"})


def extract_set_location_target_powershell(tokens: List[str], head_low: str) -> Optional[str]:
    """The raw (unresolved) target string of a `Set-Location`/`cd`/`sl`/
    `chdir` segment, or `None` when `head_low` is not one of
    `PS_SET_LOCATION_ALIASES` or the segment carries no target at all (a
    bare `cd` with no argument -- real PowerShell then goes to `$HOME`, but
    this module has no reliable `$HOME` resolution for the invoking shell
    and, per this package's fail-open posture, a caller that gets `None`
    back MUST treat this as "cwd unchanged", never as "cwd is now unknown"
    -- see `-Path`/`-LiteralPath`.

    `-Path` (`-pa`) or `-LiteralPath` (`-li`) -- unambiguous against
    `Set-Location`'s only other parameters (`-PassThru`/`-Stack`/
    `-StackName`) -- or the first positional argument, mirroring
    `extract_write_sink_targets_powershell`'s own per-cmdlet flag-then-
    positional shape.
    """
    if head_low not in PS_SET_LOCATION_ALIASES:
        return None
    args = tokens[1:]
    v = _ps_flag_value(args, ("-pa", "-li"))
    if v is not None:
        return v
    # Same filter-before-strip ordering as `extract_write_sink_targets_
    # powershell` above -- see that function's own comment.
    positional = [_strip_ps_quotes(t) for t in args if not t.startswith("-")]
    if positional:
        return positional[0]
    return None


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


#: Windows drive-letter absolute form, e.g. `C:\Users\...` or `C:/Users/...`.
#: Regex SHAPE cited (not imported) from `coordinator_core.ops.goal_append`'s
#: `_WINDOWS_DRIVE_ABSOLUTE_RE` -- `bash_guards` must not import `coordinator_
#: core.ops` (see `translate_msys_path`'s own docstring for why).
_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: Git-for-Windows MSYS toplevel drive-mount form, e.g. `/c/Users/...`.
#: Regex SHAPE cited (not imported) from `coordinator_core.ops.goal_append`'s
#: `_MSYS_ABSOLUTE_RE`, same provenance note as above.
_MSYS_ABSOLUTE_RE = re.compile(r"^/[A-Za-z]/")

#: A bare drive-mount with no trailing path segment at all, e.g. `/c` or
#: `/C` -- `_MSYS_ABSOLUTE_RE` requires a trailing `/`, so this second
#: pattern exists purely to catch that one-segment edge the first misses.
_MSYS_BARE_DRIVE_RE = re.compile(r"^/[A-Za-z]$")


def _host_is_windows() -> bool:
    """`os.name == "nt"`. A monkeypatchable seam -- tests drive both the
    Windows and POSIX branches of `translate_msys_path`/`resolve_relative`
    through this one function rather than mocking `os.name` directly."""
    return os.name == "nt"


def translate_msys_path(path: str) -> Optional[str]:
    """MSYS/MinGW drive-mount form (`/c/Users/...`, the shape Git-for-Windows'
    bash hands to tools as `$PWD`/argument expansion) translated to a native
    Windows path (`C:\\Users\\...`), or `None` if `path` is a leading-slash  # abs-path-ok: illustrative example shape, not a machine-specific citation
    form this function deliberately does not attempt to resolve.

    Pattern: normalize at the single get seam, not at each of the N
    consumers -- see DoE-claude `coordinator/docs/wiki/bash-on-windows-
    gotchas.md` §10/§14. This function IS that single seam for the two
    `bump_*_write.py` guards (C2, C4): `resolve_relative` below calls it on
    both `base` and `target` before either ever reaches `os.path`.

    Rules, in order:

    - POSIX host (`_host_is_windows()` False): IDENTITY, always, for every
      input -- `/x/foo` is a real directory on a POSIX host, never an MSYS
      drive-mount spelling to decode. No translation, ever.
    - Windows, `^/[A-Za-z]/rest` or a bare `^/[A-Za-z]$` (no trailing
      `rest`): `<LETTER>:\\rest` (drive letter upper-cased, backslash
      separator; the bare form maps to `<LETTER>:\\`).
    - Windows, already `_WINDOWS_DRIVE_ABSOLUTE_RE`-shaped, or no leading
      `/` at all: returned unchanged -- not this function's business, the
      caller's `os.path.isabs`/`os.path.join` already handle native forms
      and plain relative segments correctly.
    - Windows, any OTHER leading-slash form: `None` (untranslatable; the
      caller drops the candidate -- these guards FAIL OPEN, never invent a
      fail-closed default for a shape this function cannot decode). Three
      shapes are DELIBERATELY unhandled here, not by oversight:
        * `/tmp/x`, `/usr/...` -- no MSYS install-root guessing and no
          `cygpath` shell-out: the repo's shell-out ban forbids the latter,
          and MSYS2's install-root is environment-dependent, not a fixed
          algorithm safe to reimplement.
        * `//server/share` (MSYS-spelled UNC only -- the forward-slash form
          bash hands us, NOT the native backslash `\\\\server\\share` spelling,
          which starts with neither `/` nor a drive letter and so never
          reaches this function's leading-slash branches at all) -- `os.path.
          isabs` returns True for a double-slash path on Windows, so an
          unhandled `//c/foo` would otherwise fall through to the
          "unchanged" branch above and have `os.path.realpath` treat `c` as
          a UNC SERVER NAME -- a different wrong resolution than the one
          this module exists to fix, which `_WINDOWS_DRIVE_ABSOLUTE_RE`
          (single leading slash only) does not catch on its own.
        * `/cygdrive/...` -- Cygwin's own drive-mount spelling, a different
          convention from MSYS's; not attempted.

    Negative-spec (bounding this against the sibling `foreign-platform-
    path-guard.md` "detect-only, never auto-repair" doctrine -- which does
    NOT bar this translator; that doctrine is scoped to mutating a
    persisted, bidirectionally-synced artifact, and nothing here is
    persisted, nor is decoding this host's own shell's spelling a "repair"):
      (1) the result is used ONLY to compute a verdict -- the guard never
          rewrites, persists, or hands back the user's command;
      (2) an emitter-side fix is unavailable because the emitter is the
          harness Bash tool, outside this repo, and `_command_tokenizer`
          was rejected as the seam for blast radius.
    Negative-spec (environment-variable suppression is a SCOPE MISMATCH):
    `MSYS2_ARG_CONV_EXCL` / `MSYS2_ENV_CONV_EXCL` govern MSYS2's OWN
    conversion when MSYS2 invokes a native process; this defect lives
    entirely inside OUR `isabs`/`join`/`realpath` call on a string we
    already hold, a different layer those variables cannot reach.
    """
    if not _host_is_windows():
        return path
    if _WINDOWS_DRIVE_ABSOLUTE_RE.match(path) or not path.startswith("/"):
        return path
    m = _MSYS_ABSOLUTE_RE.match(path)
    if m:
        # `rest` still carries MSYS-style forward slashes past the drive
        # segment (bash never emits backslashes) -- normalize those to the
        # native separator too, not just the drive prefix, so the WHOLE
        # result is a well-formed Windows path a subsequent `os.path.join`
        # (native `ntpath.join`) treats consistently.
        rest = path[3:].replace("/", "\\")
        return f"{path[1].upper()}:\\{rest}"
    if _MSYS_BARE_DRIVE_RE.match(path):
        return f"{path[1].upper()}:\\"
    return None


def resolve_relative(base: str, target: str) -> Optional[str]:
    """`target` resolved against `base` if relative, else `target` itself --
    the lifted, translation-aware twin of the `_resolve_relative` previously
    duplicated verbatim in `bump_outside_repo_write.py:275` and
    `bump_foreign_repo_write.py:277` (C2 replaces both local defs with
    module-level aliases onto this one, the same precedent
    `nearest_existing_ancestor` above already set: imported and aliased,
    never copied).

    `None` means untranslatable -- the caller drops the candidate, per this
    guard family's FAIL OPEN posture (see `translate_msys_path`).

    THE FIX this function exists for: on Windows, handing a POSIX-absolute
    string like `/x/claude-klabauter/scratch/t.txt` to bare `os.path` anchors
    it onto the process's current drive (`os.path.isabs` True on Python
    3.11/3.12 -> returned verbatim -> `os.path.realpath` anchors it later;
    `os.path.isabs` False on 3.13+ -> `os.path.join(base, expanded)`
    re-roots it onto `base`'s drive) -- both routes converge on the same
    wrong, nonexistent path, denying a write that is actually inside the
    session's own repo. Translating BOTH `base` and `target` to native form
    BEFORE either reaches `os.path.isabs`/`os.path.join` makes the fix
    version-independent by construction: neither of those two calls ever
    sees a drive-relative string, so there is no per-interpreter branch left
    to diverge on. (One narrow exception: a `~`-prefixed `target` is first
    handed to `os.path.expanduser` -- itself an `os.path` call -- BEFORE
    `translate_msys_path` sees it, since expansion has to happen before
    MSYS-drive-mount translation can even recognise the resulting string.
    Harmless on Windows because native `ntpath.expanduser` reads
    `USERPROFILE`, not the invoking shell's MSYS `HOME`, so it already
    returns native-drive-letter form that `translate_msys_path` then passes
    through unchanged -- but the "neither call ever sees a drive-relative
    string" guarantee below is therefore about `isabs`/`join`, not literally
    every `os.path` call in this function.)

    `base` is translated too, deliberately: `base` is the effective cwd
    threaded from the payload, and on Windows the Bash tool hands us
    `/x/claude-klabauter` there just as readily as in `target` -- joining a
    relative target onto an untranslated POSIX `base` would reproduce the
    identical defect one level up.
    """
    if not target:
        return translate_msys_path(base)
    expanded = os.path.expanduser(target) if target.startswith("~") else target
    t = translate_msys_path(expanded)
    if t is None:
        return None
    b = translate_msys_path(base)
    if b is None:
        return None
    if os.path.isabs(t):
        return t
    return os.path.join(b, t)


# ---------------------------------------------------------------------------
# Interpreter-payload write-sink extraction (C5 plan "the outside-repo bump
# never looks inside an interpreter body", 2026-08-14, see the PM-ratified
# reversal recorded in this module's own top docstring). SEPARATE from
# `extract_write_sink_targets_for_segment` above and NEVER consumed by C4
# (`bump_foreign_repo_write.py`) -- opted in by `bump_outside_repo_write.py`
# (C5) only. Operates on the RAW command STRING, not an already-tokenized
# segment: by the time `_command_tokenizer.resolve_command_positions` hands
# back tokenized segments, a heredoc BODY has already been stripped
# (`_strip_heredocs`, deliberately, for its 33 other consumers), so the
# write-sink shapes this section recognizes are only ever visible in the
# raw text.
# ---------------------------------------------------------------------------

#: `python`/`python3` only -- the one PM-ratified interpreter (see module
#: docstring). A second interpreter (`node -e`, `ruby -e`, `perl -e`) is
#: explicitly out of scope for this pass (plan's own "Out of scope").
_PYTHON_C_FLAG_INTERPRETERS = frozenset({"python", "python3"})

#: Quoted-string literal, single- or double-quoted, with escape support --
#: matched with a NEGATIVE LOOKBEHIND against an immediately preceding
#: identifier character so a prefixed string (`f'...'`, `r'...'`, `b'...'`,
#: or a bare variable name butted up against a quote) never matches. This is
#: the fail-open direction the plan's own AC5 names ("an f-string ... yields
#: NOTHING") -- this module has no Python parser and does not evaluate an
#: f-string's interpolated value, so treating one as a literal path would be
#: simply wrong, not merely imprecise.
_PY_QUOTED_LITERAL = r"(?<![A-Za-z0-9_])('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"

#: `open(<path>, <mode>)` -- positional `mode` argument, checked below
#: (`_mode_allows_write`) for a `w`/`a`/`x` character; a read-only mode
#: (`'r'`, `'rb'`) is not a write-sink shape at all.
_PY_OPEN_CALL_RE = re.compile(
    r"\bopen\(\s*" + _PY_QUOTED_LITERAL + r"\s*,\s*" + _PY_QUOTED_LITERAL
)

#: `<path-literal>).write_text(` / `<path-literal>).write_bytes(` -- the
#: `Path(...)`/`PurePath(...)`-constructed-then-chained shape (`pathlib.
#: Path('../mine.patch').write_text('x')`). Matches on the closing paren
#: immediately before the method call, not on the constructor name itself,
#: since `Path`/`pathlib.Path`/a re-assigned alias all share this identical
#: tail shape and this module does not track import aliases.
_PY_WRITE_TEXT_BYTES_RE = re.compile(
    _PY_QUOTED_LITERAL + r"\s*\)\s*\.\s*(?:write_text|write_bytes)\("
)

#: `<path-literal>).open(<mode>)` -- `Path(...).open('w')`, the `Path`-object
#: sibling of the builtin `open(<path>, <mode>)` shape above. Same
#: mode-must-contain-w/a/x check.
_PY_DOT_OPEN_RE = re.compile(
    _PY_QUOTED_LITERAL + r"\s*\)\s*\.\s*open\(\s*" + _PY_QUOTED_LITERAL
)

#: `os.makedirs(<path>)` / `os.mkdir(<path>)`.
_PY_MAKEDIRS_RE = re.compile(r"\bos\.\s*(?:makedirs|mkdir)\(\s*" + _PY_QUOTED_LITERAL)

#: `shutil.copy*(<src>, <dst>)` / `shutil.move(<src>, <dst>)` -- only
#: `<dst>` (the second positional argument) is captured; `<src>` is matched
#: but never captured, as a run of non-comma/non-paren characters --
#: deliberately coarse (does not balance nested calls in the `<src>`
#: position) since an unmatched `<src>` shape simply yields no candidate
#: here, the fail-open direction this whole module already applies
#: everywhere else.
_PY_SHUTIL_RE = re.compile(
    r"\bshutil\.\s*(?:copy\w*|move)\(\s*[^,()]*\s*,\s*" + _PY_QUOTED_LITERAL
)


#: ONE alternation, tried in encounter order at each scan position -- see
#: `_strip_comments_and_docstrings` for why this replaced two sequential
#: passes (code-reviewer sidecar `ffbcb84d`, findings 1 and 2, EM-directed
#: fix). `re.sub` scans left to right and, at each position, tries
#: alternatives in the order written, so whichever construct's opener
#: STARTS FIRST in the text wins -- exactly the ordering semantics a
#: two-pass strip cannot express, since the second pass has no memory of
#: what the first pass already consumed:
#:
#:   1. `"""..."""` / `'''...'''` -- a PAIRED triple-quoted block, non-greedy.
#:   2. `#...` to end of line -- a comment, tried only once no paired
#:      triple-quote opens earlier at this position.
#:   3. `"""...` / `'''...` to END OF TEXT -- an UNTERMINATED triple-quote
#:      opener with no matching close anywhere in the remaining text. Only
#:      reached when neither #1 nor #2 matched at this position, i.e. this
#:      is a real string-literal opener, not a comment containing a stray
#:      `"""`/`'''` token (that case is caught by #2 first, since the `#`
#:      that starts the comment necessarily precedes the stray quote chars
#:      inside it).
_PY_COMMENT_OR_STRING_RE = re.compile(
    r'"""[\s\S]*?"""'
    r"|'''[\s\S]*?'''"
    r"|#[^\n]*"
    r'|"""[\s\S]*'
    r"|'''[\s\S]*"
)


def _strip_comments_and_docstrings(text: str) -> str:
    """Best-effort removal of Python comments and triple-quoted strings from
    `text` before the write-shape regexes below scan it -- deliberately in
    the FALSE-DENY-AVOIDING direction, not the parser-building direction the
    plan's Anti-scope forbids: this is plain text stripping, no AST, no
    execution model.

    ONE regex, ONE pass (`_PY_COMMENT_OR_STRING_RE`) -- not two sequential
    `re.sub` calls. The original two-pass version (triple-quotes stripped
    first, then `#`-comments) had no ordering constraint between the two
    passes: a triple-quote token appearing INSIDE a `#` comment paired with a
    LATER, genuine triple-quoted string before the comment regex ever got a
    chance to neutralize it, silently erasing every real line of code in
    between -- including a real outside-repo write (code-reviewer sidecar
    `ffbcb84d`, finding 1, P1: a false MISS on a real write, the direction
    this module's whole design already tolerates being over-cautious about
    but never silently drops). A single alternation tried left-to-right at
    each position fixes this because whichever construct's opener occurs
    FIRST in the text is the one `re.sub` matches, matching real Python
    tokenization order for this purpose.

    ACCEPTED TRADE, on purpose, do not "fix" this back: naive `#`-comment
    stripping can truncate a line where a `#` sits inside a quoted path
    (`open('../x#y', 'w')`), turning that case into a MISS. Misses are
    tolerable under this module's fail-open posture; false denies (a
    docstring/comment merely describing a write call producing a real bump)
    are not -- that asymmetry is the entire reason this function exists.

    SECOND ACCEPTED TRADE, same asymmetry, now also handled correctly: an
    UNTERMINATED triple-quote (no matching close anywhere in `text`) means
    every remaining character is lexically INSIDE that string literal --
    this function erases it to end of text (alternatives 3/4 above) rather
    than leaving it unstripped. Leaving it unstripped was the false-deny
    bug: a bare mention of a write-call shape inside that dangling
    "docstring" text would otherwise reach the write-shape regexes and bump
    on text that never executes. Erasing to end of text can only ever
    produce a MISS (the tolerated direction), never a false deny."""
    return _PY_COMMENT_OR_STRING_RE.sub(
        lambda m: "" if m.group(0).startswith("#") else " ", text
    )


def _mode_allows_write(mode_literal: str) -> bool:
    """`mode_literal` is a quoted Python string INCLUDING its quotes (e.g.
    `"'wb'"`); True if its content contains any of `w`/`a`/`x` -- the write,
    append, or exclusive-create mode characters. A read-only mode (`'r'`,
    `'rb'`, `'r+'`) never matches -- this closed set does not distinguish
    `'r+'`'s own read-and-write semantics, matching the plan's own AC table,
    which names no `r+` case."""
    content = mode_literal[1:-1]
    return any(c in content for c in "wax")


def _python_write_targets_in_text(text: str) -> List[str]:
    """Scan `text` (a heredoc body or an inline `-c` payload string) for the
    CLOSED set of Python write shapes this plan ratifies -- see this
    section's own module-docstring addendum. Every match's path/destination
    group is a LITERAL quoted string only (`_PY_QUOTED_LITERAL`'s own
    negative lookbehind already excludes an f-string/r-string/b-string
    prefix); a concatenation, a variable, or any shape this closed set does
    not name yields nothing for that occurrence, never a guess."""
    text = _strip_comments_and_docstrings(text)
    targets: List[str] = []

    for m in _PY_OPEN_CALL_RE.finditer(text):
        if _mode_allows_write(m.group(2)):
            targets.append(m.group(1)[1:-1])

    for m in _PY_WRITE_TEXT_BYTES_RE.finditer(text):
        targets.append(m.group(1)[1:-1])

    for m in _PY_DOT_OPEN_RE.finditer(text):
        if _mode_allows_write(m.group(2)):
            targets.append(m.group(1)[1:-1])

    for m in _PY_MAKEDIRS_RE.finditer(text):
        targets.append(m.group(1)[1:-1])

    for m in _PY_SHUTIL_RE.finditer(text):
        targets.append(m.group(1)[1:-1])

    return targets


def _iter_heredoc_bodies(text: str) -> List[str]:
    """Every TERMINATED heredoc body in raw command text `text`, in
    left-to-right encounter order -- reuses `_command_tokenizer.
    _HEREDOC_OPEN_RE` (per this module's own reuse contract, never
    re-implemented) to locate each opener, mirroring `_command_tokenizer.
    _strip_heredocs`'s own terminator-scan exactly, but COLLECTING the body
    instead of discarding it. An unterminated heredoc (`_strip_heredocs`'s
    own fail-open case) is skipped, not returned partially -- half a
    heredoc body is not reliable source text to scan.

    KEEP IN SYNC: this hand-mirrors `_command_tokenizer._strip_heredocs`'s
    terminator-walk rather than calling it, because `_strip_heredocs`
    discards the body this function needs to return -- any change to that
    function's opener/terminator/`<<-`-tab-stripping semantics must be
    ported here by hand."""
    if "<<" not in text:
        return []
    bodies: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _HEREDOC_OPEN_RE.search(text, i)
        if not m:
            break
        strip_tabs = text[m.start(): m.start() + 3] == "<<-"
        marker = m.group(2)
        nl = text.find("\n", m.end())
        if nl == -1:
            break
        j = nl + 1
        lines: List[str] = []
        terminated = False
        while j < n:
            line_end = text.find("\n", j)
            raw_line = text[j: line_end if line_end != -1 else n]
            check_line = raw_line.lstrip("\t") if strip_tabs else raw_line
            j = (line_end + 1) if line_end != -1 else n
            if check_line == marker:
                terminated = True
                break
            lines.append(raw_line)
        if terminated:
            bodies.append("\n".join(lines))
        i = j
    return bodies


def _iter_python_dash_c_payloads(raw_cmd: str) -> List[str]:
    """Every inline `-c` payload string handed to a `python`/`python3`
    interpreter at DEPTH 0 of `raw_cmd` -- tokenizes via `_command_tokenizer.
    resolve_command_positions` (the package's one resolve-once entry point,
    per this module's own "reuses rather than reimplements" precedent) and
    reuses `_command_tokenizer._extract_dash_c_payload` (the SAME
    bundled/standalone `-c` scan `bump_outside_repo_write._extract_inline_c_
    payload` itself mirrors) rather than re-deriving that pattern a third
    time. Depth 0 only -- a `-c` payload nested inside another interpreter's
    own payload is already out of this module's reach without a second
    recursive unwrap, and this closed set stays narrow on purpose (see
    module docstring addendum)."""
    try:
        segments = resolve_command_positions(
            raw_cmd, preserve_windows_backslashes=_host_is_windows()
        )
    except Exception:  # noqa: BLE001 -- fail open, never propagate a parse error
        return []
    payloads: List[str] = []
    for rc in segments:
        if rc.depth != 0 or rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        head_base = normalize_executable_basename(rc.tokens[0])
        if head_base not in _PYTHON_C_FLAG_INTERPRETERS:
            continue
        payload = _extract_dash_c_payload(rc.tokens[1:])
        if payload:
            payloads.append(payload)
    return payloads


def extract_interpreter_payload_write_sink_targets(raw_cmd: str) -> List[str]:
    """Raw candidate write-target strings found INSIDE an interpreter
    PAYLOAD carried by `raw_cmd` -- a heredoc body, or a `python`/`python3
    -c` argument string -- rather than a shell token operand
    (`extract_write_sink_targets_for_segment`'s own territory). See this
    section's own module-docstring addendum for the PM-ratified scope this
    function exists to close.

    Exactly like its sibling: no resolution, no verdict -- the caller
    resolves each candidate against its own effective cwd and applies its
    own no-git-root predicate, which is also why an over-broad candidate
    here is harmless (anything resolving under a git root is dropped by the
    caller before it can bump).

    FAILS OPEN: any exception at any step of this function -- heredoc
    scanning, `-c`-payload extraction, or the Python-shape regex scan --
    yields no candidates for that step rather than propagating; this
    function itself never raises. C5-only, opt-in (see
    `bump_outside_repo_write._iter_write_sink_candidates`) -- C4
    (`bump_foreign_repo_write.py`) does not call this function and this
    module's own shared bash-shape table above (`extract_write_sink_
    targets_for_segment`) is untouched by its addition."""
    targets: List[str] = []

    try:
        for body in _iter_heredoc_bodies(raw_cmd):
            targets.extend(_python_write_targets_in_text(body))
    except Exception:  # noqa: BLE001 -- fail open
        pass

    try:
        for payload in _iter_python_dash_c_payloads(raw_cmd):
            targets.extend(_python_write_targets_in_text(payload))
    except Exception:  # noqa: BLE001 -- fail open
        pass

    return targets
