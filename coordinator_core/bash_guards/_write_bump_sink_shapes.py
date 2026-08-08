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
    consumers -- see example-doctrine-repo `coordinator/docs/wiki/bash-on-windows-
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
