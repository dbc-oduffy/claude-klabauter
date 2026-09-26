"""coordinator_core.search.sources_powershell -- answer a PowerShell read/listing
command IN-PROCESS, onto `engine.Source`'s existing protocol.

Purpose: closes a hole this plan's own guard chain already opened. `guard_
inprocess_search.MATCHERS` is `_tool_names.COMMAND_TOOL_NAMES` -- `("Bash",
"PowerShell")` -- so every PowerShell read ALREADY reaches this seam, is declined
for lack of recognition (nothing in `coordinator_core.search` speaks
`Get-Content`/`Get-ChildItem` vocabulary), and forks. On a repo whose CLAUDE.md
makes Windows first-class, and where PowerShell is the primary shell, serving
only the bash vocabulary leaves the native shell paying the whole cost this plan
exists to remove.

Shapes recognized (per this chunk's dispatch brief), onto the same read
vocabulary `sources_read.py` (C1) established for bash:
  - `Get-Content FILE` (and its `cat`/`gc`/`type` aliases, which PowerShell
    resolves itself at runtime -- this module still has to recognize each
    literal alias token, since the guard payload carries what the agent typed,
    never the resolved cmdlet name).
  - `Get-Content -TotalCount N FILE` / `-First N FILE` (leading N lines).
  - `Get-Content -Tail N FILE` (trailing N lines).
  - `Get-ChildItem` / `gci` / `ls` / `dir` for a directory listing.

Why this is its OWN module, not a flag on `sources_read`/`sources_listdir`
--------------------------------------------------------------------------
`ls` in a PowerShell payload is `Get-ChildItem`, NOT coreutils `ls` --
`sources_listdir.parse_ls_segment` accepts a byte-sort/locale-collation
GNU/BSD flag grammar (`-1`, `-a`) that means something else, or nothing at all,
under PowerShell's own parameter binding. Parsing a PowerShell `ls` token
stream with that parser would produce a CONFIDENTLY WRONG answer -- the
specific failure mode this whole package exists to refuse rather than risk --
not merely a less-precise one. Same reasoning for `Get-Content` against
`sources_read.parse_read_segment`: PowerShell's `-Tail`/`-TotalCount`
share no grammar with `head`/`tail -n`.

Tokenization discipline -- the real work, not faked
-----------------------------------------------------
`_shape_classifier`'s POSIX `shlex` path never sees a PowerShell payload (see
that module's own docstring); this module's caller is expected to route a
PowerShell command through `coordinator_core.bash_guards._dialect
.tokenize_command(cmd_text, Dialect.POWERSHELL, guard_name=...)` -- the ONE
dialect-aware tokenizer this repo already built for exactly this problem
(`docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md`)
-- and hand this module the resulting token list for ONE segment. This module
itself never calls `shlex` and never hand-rolls a second PowerShell tokenizer;
it only classifies and consumes an already-tokenized argv.

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT accept `-Raw`, `-Encoding`, or `-Stream` on `Get-Content` -- each
    changes the bytes actually produced (a raw single string instead of a
    line-object stream; a non-default codepage; an alternate-data-stream read),
    and this module has no way to reproduce any of the three faithfully without
    spawning the very process this package exists to avoid.
  - Does NOT accept any `Get-ChildItem` flag at all (`-Force`, `-Recurse`,
    `-Filter`, `-Include`, `-Exclude`, `-Hidden`, ...) -- each is either a
    visibility rule (`-Force`) or an entirely different output shape
    (`-Recurse`) this module does not implement. Refusing is strictly better
    than a plausible-looking wrong listing.
  - Does NOT accept a non-filesystem PSDrive provider path (`Registry::...`,
    `Env:...`, `Function:...`, `Cert:...`, `WSMan:...`, `Variable:...`,
    `Alias:...`, or a bare `Provider::path`) -- an `Env:` read is not a file
    read, and this module answers file reads only.
  - Does NOT accept `$(...)`  (subexpression operator), a backtick escape, a
    process-substitution-shaped `<(...)`, a bare `&` call-operator token in
    operand position, a redirection operator, or a `$`-prefixed variable/
    provider-qualified operand -- the same escape discipline `sources_read.py`
    (C1) and `sources_listdir.py` (C2) already apply to their own bash
    operands, applied here to PowerShell's own equivalents.
  - Does NOT expand a glob operand (`*`, `?`, `[`) -- PowerShell expands these
    itself before the cmdlet ever sees them; this seam has no PowerShell host
    to perform that expansion faithfully.
  - Does NOT decide multi-segment pipe/statement structure. Like
    `sources_read.parse_read_segment`, this module parses ONE already-segmented
    argv; whether a segment may be answered stand-alone (not piped into, every
    later segment pipe-connected, an unrecognized downstream cmdlet declines
    the whole plan) is the wiring layer's decision, mirroring `answer.py`'s own
    division of labor from `sources_read.py`.
  - Does NOT assume output-byte fidelity. `Get-Content` emits a line-object
    stream the PowerShell host renders with the HOST's own line ending, not
    necessarily the file's own terminator -- unlike `cat`, which reproduces a
    file's exact bytes. `ContentSpec.produce`'s `newline` parameter makes that
    assumption an explicit, named argument rather than a silently-baked
    default; a caller (C11) that cannot establish which line ending the real
    host would render declines rather than guessing -- this module does not
    invent an "auto-detect" fallback of its own.
  - Does NOT guess `Get-ChildItem`'s own enumeration order. On Windows,
    `os.scandir`/`os.listdir` and .NET's directory enumeration both read
    directly from the NTFS index via the same `FindFirstFileW`/
    `FindNextFileW` Win32 API, with no additional sort layer on either side
    before an explicit `Sort-Object` -- so the OS-returned order is reused
    verbatim rather than re-sorted. On any other platform that equivalence
    does not hold, and `run_childitem` declines rather than serving an order
    this module cannot claim to reproduce.

Test surface: `coordinator_core/search/tests/test_sources_powershell.py`.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union

from coordinator_core.search.engine import MAX_RENDER_BYTES, Unanswerable

#: `_START_PROCESS_NAMES` documents).
_CONTENT_VERBS = frozenset({"get-content", "cat", "gc", "type"})

_CHILDITEM_VERBS = frozenset({"get-childitem", "gci", "ls", "dir"})

_UNSUPPORTED_CONTENT_FLAGS = frozenset({
    "-raw", "-encoding", "-stream", "-delimiter", "-wait", "-force",
    "-filter", "-include", "-exclude", "-credential", "-readcount",
})

_HEAD_FLAGS = frozenset({"-totalcount", "-first"})
_TAIL_FLAGS = frozenset({"-tail", "-last"})

_REDIRECT_EXACT = frozenset({">", ">>", "<", "2>", "2>>", ">&1", "*>", "*>>"})

_SUBSTITUTION_MARKERS = ("$(", "`", "<(", "$")

_STATEMENT_BOUNDARY = frozenset({";", "&", "|"})

_NON_FILESYSTEM_PROVIDER_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_]*::|(?:Env|Registry|Function|Variable|Alias|"
    r"Cert|WSMan)\:)",
    re.IGNORECASE,
)

_GLOB_METACHARS = ("*", "?", "[")


def _reject_escapes(tokens: Sequence[str]) -> None:
    for tok in tokens:
        if tok in _REDIRECT_EXACT:
            raise Unanswerable("redirection token %r in PowerShell read command" % tok)
        if tok in _STATEMENT_BOUNDARY:
            raise Unanswerable("statement/call-operator token %r in operand position" % tok)
        if any(marker in tok for marker in _SUBSTITUTION_MARKERS):
            raise Unanswerable("substitution token in operand %r" % tok)
        if _NON_FILESYSTEM_PROVIDER_RE.match(tok):
            raise Unanswerable("non-filesystem provider path %r not supported" % tok)


def _reject_glob(operand: str) -> None:
    if any(ch in operand for ch in _GLOB_METACHARS):
        raise Unanswerable("glob operand %r not supported" % operand)


def _resolve_operand(operand: str, cwd: str) -> str:
    _reject_glob(operand)
    base = operand if os.path.isabs(operand) else os.path.join(cwd, operand)
    if not os.path.isfile(base):
        raise Unanswerable("operand %r is not a readable regular file" % operand)
    if not os.access(base, os.R_OK):
        raise Unanswerable("operand %r is not readable" % operand)
    return base


@dataclass
class ContentSpec:

    operand: str
    head_count: Optional[int] = None
    tail_count: Optional[int] = None

    def produce(self, cwd: str = ".", newline: str = "\r\n") -> str:
        path = _resolve_operand(self.operand, cwd)
        text = _read_text_strict(path)
        lines = text.splitlines()

        if self.head_count is not None:
            lines = lines[: self.head_count]
        if self.tail_count is not None:
            lines = [] if self.tail_count == 0 else lines[-self.tail_count:]

        if not lines:
            return ""
        return newline.join(lines) + newline


def parse_content_segment(tokens: Sequence[str]) -> ContentSpec:
    if not tokens:
        raise Unanswerable("empty PowerShell read segment")
    verb = tokens[0].lower()
    if verb not in _CONTENT_VERBS:
        raise Unanswerable("unsupported PowerShell read verb %r" % tokens[0])

    args = tokens[1:]
    _reject_escapes(args)

    head_count: Optional[int] = None
    tail_count: Optional[int] = None
    operand: Optional[str] = None
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        low = tok.lower()
        if low in _UNSUPPORTED_CONTENT_FLAGS:
            raise Unanswerable("unsupported Get-Content flag %r" % tok)
        if low in _HEAD_FLAGS:
            if head_count is not None:
                raise Unanswerable("multiple Get-Content head-count flags")
            if i + 1 >= n:
                raise Unanswerable("%s is missing its value" % tok)
            value = args[i + 1]
            if not value.isdigit():
                raise Unanswerable("%s expects a non-negative integer, got %r" % (tok, value))
            head_count = int(value)
            i += 2
            continue
        if low in _TAIL_FLAGS:
            if tail_count is not None:
                raise Unanswerable("multiple Get-Content tail-count flags")
            if i + 1 >= n:
                raise Unanswerable("%s is missing its value" % tok)
            value = args[i + 1]
            if not value.isdigit():
                raise Unanswerable("%s expects a non-negative integer, got %r" % (tok, value))
            tail_count = int(value)
            i += 2
            continue
        if low.startswith("-path") or low == "-literalpath":
            if i + 1 >= n:
                raise Unanswerable("%s is missing its value" % tok)
            if operand is not None:
                raise Unanswerable("multiple Get-Content path operands not supported")
            operand = args[i + 1]
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            raise Unanswerable("unsupported Get-Content flag %r" % tok)
        if operand is not None:
            raise Unanswerable("multiple Get-Content operands not supported")
        operand = tok
        i += 1

    if operand is None:
        raise Unanswerable("Get-Content: no path operand")
    return ContentSpec(operand=operand, head_count=head_count, tail_count=tail_count)


def _read_text_strict(path: str) -> str:
    """Read `path` as strict UTF-8 text, or raise `Unanswerable`. Mirrors
    `sources_read._read_text_strict` -- deliberately not imported from there,
    since that function is private to its own module and this module owes it
    no coupling beyond the shared `engine.MAX_RENDER_BYTES` cap."""
    try:
        size = os.stat(path).st_size
        if size > MAX_RENDER_BYTES:
            raise Unanswerable("%r is %d bytes, above the render cap" % (path, size))
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise Unanswerable("cannot read %r: %s" % (path, exc))
    if b"\x00" in data:
        raise Unanswerable("%r does not decode as text (NUL byte found)" % path)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unanswerable("%r does not decode as UTF-8: %s" % (path, exc))


@dataclass
class ChildItemSpec:

    directory: str = "."


def parse_childitem_segment(tokens: Sequence[str]) -> ChildItemSpec:
    if not tokens:
        raise Unanswerable("empty PowerShell listing segment")
    verb = tokens[0].lower()
    if verb not in _CHILDITEM_VERBS:
        raise Unanswerable("unsupported PowerShell listing verb %r" % tokens[0])

    args = tokens[1:]
    _reject_escapes(args)

    operand: Optional[str] = None
    for tok in args:
        if tok.startswith("-") and tok != "-":
            raise Unanswerable("unsupported Get-ChildItem flag %r" % tok)
        if operand is not None:
            raise Unanswerable("multiple Get-ChildItem operands not supported")
        operand = tok

    directory = operand if operand is not None else "."
    _reject_glob(directory)
    return ChildItemSpec(directory=directory)


def run_childitem(spec: ChildItemSpec, cwd: str = ".") -> List[str]:
    if sys.platform != "win32":
        raise Unanswerable("Get-ChildItem enumeration order is only reproduced on win32")

    base = spec.directory if os.path.isabs(spec.directory) else os.path.join(cwd, spec.directory)
    if not os.path.exists(base):
        raise Unanswerable("Get-ChildItem target %r does not exist" % spec.directory)
    if not os.path.isdir(base):
        raise Unanswerable("Get-ChildItem target %r is not a directory" % spec.directory)

    entries: List[str] = []
    try:
        with os.scandir(base) as it:
            for entry in it:
                if _is_hidden_or_system_windows(entry.path):
                    continue
                entries.append(entry.name)
    except OSError as exc:
        raise Unanswerable("cannot list %r: %s" % (spec.directory, exc))
    return entries


#: `FILE_ATTRIBUTE_HIDDEN` (0x2) | `FILE_ATTRIBUTE_SYSTEM` (0x4) -- the two
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def _is_hidden_or_system_windows(path: str) -> bool:
    import ctypes

    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def parse_powershell_segment(tokens: Sequence[str]) -> Union[ContentSpec, ChildItemSpec]:
    if not tokens:
        raise Unanswerable("empty PowerShell segment")
    verb = tokens[0].lower()
    if verb in _CONTENT_VERBS:
        return parse_content_segment(tokens)
    if verb in _CHILDITEM_VERBS:
        return parse_childitem_segment(tokens)
    raise Unanswerable("unsupported PowerShell verb %r" % tokens[0])
