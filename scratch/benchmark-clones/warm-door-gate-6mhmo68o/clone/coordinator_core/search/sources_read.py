"""coordinator_core.search.sources_read -- answer a file-read Bash command
IN-PROCESS: `cat FILE...`, `head`/`tail [-n N | -N] FILE`, `sed -n '<a>,<b>p' FILE`.

Purpose: parse one of the shapes above, and either return an object that
produces the exact bytes-as-text the command would have printed, or raise
`engine.Unanswerable`. Parallel in spirit to `engine.parse_grep_segment` /
`engine.run`, but for a source class whose deliverable is a file's own bytes
rather than a search result -- see the decoding-policy note below for why
that difference is NOT cosmetic.

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT reuse `engine._read_text`'s decoding policy. That function decodes
    with `errors="replace"` and NUL-scans only the first 8192 bytes -- both
    correct for grep (a mojibake line either matches or doesn't; the file's
    bytes are not the deliverable) and both wrong for a read, whose deliverable
    IS the bytes. A latin-1 file or a stray non-UTF-8 byte would otherwise
    render as a clean-looking body with U+FFFD substituted in, with no reason
    for the agent reading it to doubt it. This module decodes strict UTF-8 and
    NUL-scans the WHOLE file, declining rather than serving a lossy render.
  - Does NOT expand a glob or brace operand. `cat a*.txt` is a shell expansion
    this seam has no shell to perform faithfully; it declines rather than
    guessing which files the shell would have named, in what order.
  - Does NOT accept multi-file `head`/`tail`. Real `head`/`tail` interleave
    `==> path <==` headers across more than one file; reproducing that exactly
    is a fidelity trap for a shape that is rare relative to the single-file
    form, so only the single-file form is accepted.
  - Does NOT accept a `sed` program other than a 1-indexed, inclusive `p`
    range or single line (`$` accepted as the end bound). No `-i`, no other
    sed command letter, no trailing `q` optimisation, no `sed` without `-n`.
  - Does NOT read past `engine.MAX_RENDER_BYTES` unconditionally. The resolved
    path is `stat()`-ed before it is ever opened; a file above the cap declines
    rather than `handle.read()`-ing the whole thing. This module runs inside
    the PreToolUse hook that gates every Bash call on a box running 50-70
    concurrent sessions -- an unbounded read of an 800MB log is an in-hook
    stall or an OOM in the process gating the session's next tool call.
  - Does NOT invent path confinement. Matches `engine.resolve_plain_path_operand`
    (relative-against-cwd, absolute passed through) -- no `commonpath`/`realpath`
    check, because real `cat` has none either and there is nothing to reuse.
  - Does NOT round-trip through a line list that would silently add a trailing
    newline. `cat` of a file with no final newline prints no final newline; the
    source models the file's exact bytes-as-text and lets the caller (C3) decide
    rendering.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from coordinator_core.search.engine import (
    MAX_RENDER_BYTES,
    Unanswerable,
    resolve_plain_path_operand,
)

READ_VERBS = ("cat", "head", "tail", "sed")

#: Exact-match redirection tokens. The shared tokenizer
#: (`_command_tokenizer.segments_from_tokens_with_pipe_flag`) splits segments only
#: at unquoted `;`/`&`/`|`, so `cat a.txt > out.txt` arrives as one ordinary-looking
#: token list -- these must be rejected explicitly, by name, rather than relying on
#: the redirect target happening not to exist on disk (see module docstring on
#: `_reject_redirection_and_substitution`, and the dispatch brief this module was
#: built from, on why that accident is not something a future operand-parsing
#: change can be trusted to preserve).
_REDIRECT_EXACT = frozenset({">", ">>", "<", "<<", "<<<", "2>", "&>", "|&"})

#: Substring markers for command/process substitution. Checked via containment,
#: not exact match, because these can appear glued to other characters
#: (`$(cmd)`, "$VAR", `<(cmd)`).
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", "$")

_SED_RANGE_RE = re.compile(r"^(?P<a>\d+|\$),(?P<b>\d+|\$)p$")
_SED_SINGLE_RE = re.compile(r"^(?P<a>\d+|\$)p$")


def _reject_redirection_and_substitution(tokens: Sequence[str]) -> None:
    """Decline, by name, any operand-position token that is a redirection or
    substitution marker -- checked before any per-shape parser looks at what
    remains of the operand list. See module docstring; this is not a corner
    case, it is what keeps a future operand-parsing change from silently
    serving a rewritten command whose redirect never happens.
    """
    for tok in tokens:
        if tok in _REDIRECT_EXACT:
            raise Unanswerable("redirection token %r in read command" % tok)
        if any(marker in tok for marker in _SUBSTITUTION_MARKERS):
            raise Unanswerable("substitution token in operand %r" % tok)


@dataclass
class ReadSpec:
    """A parsed file-read invocation, not yet resolved against a cwd or read."""

    kind: str
    operands: List[str] = field(default_factory=list)
    count: Optional[int] = None  # head/tail: number of lines
    start: Optional[int] = None  # sed: 1-indexed start line ($ -> None, resolved late)
    end: Optional[int] = None    # sed: 1-indexed end line ($ -> None, resolved late)
    start_is_last: bool = False
    end_is_last: bool = False

    def produce(self, cwd: str = ".") -> str:
        """Resolve operands and return the exact text the command would print.

        Raises `Unanswerable` for anything that cannot be served faithfully:
        a missing/non-regular/unreadable operand, an oversized file, or content
        that does not decode as strict UTF-8.
        """
        paths = [resolve_plain_path_operand(op, cwd) for op in self.operands]

        if self.kind == "cat":
            return "".join(_read_text_strict(p) for p in paths)

        (path,) = paths
        text = _read_text_strict(path)
        lines = text.splitlines(keepends=True)

        if self.kind == "head":
            return "".join(lines[: self.count])
        if self.kind == "tail":
            if self.count == 0:
                return ""
            return "".join(lines[-self.count :])
        if self.kind == "sed":
            n = len(lines)
            start = n if self.start_is_last else self.start
            end = n if self.end_is_last else (self.end if self.end is not None else start)
            if start < 1 or end < 1 or start > n or end < start:
                # A range/line that does not exist in the file. Real `sed -n` prints
                # nothing and exits 0 for a start beyond EOF, but distinguishing that
                # from an authoring mistake here is not worth the fidelity risk --
                # decline rather than confidently print an empty body.
                raise Unanswerable("sed line range %r,%r out of bounds for %r"
                                   % (self.start, self.end, path))
            return "".join(lines[start - 1 : end])

        raise Unanswerable("unsupported read kind %r" % self.kind)  # pragma: no cover


def parse_read_segment(tokens: Sequence[str]) -> ReadSpec:
    """Parse one `cat`/`head`/`tail`/`sed` argv into a ReadSpec, or raise
    `Unanswerable`. Does not touch the filesystem -- see `ReadSpec.produce`.
    """
    if not tokens:
        raise Unanswerable("empty read segment")
    verb = os.path.basename(tokens[0])
    if verb not in READ_VERBS:
        raise Unanswerable("unsupported read verb %r" % verb)

    if verb == "cat":
        return _parse_cat(tokens[1:])
    if verb in ("head", "tail"):
        return _parse_head_tail(verb, tokens[1:])
    return _parse_sed(tokens[1:])


def _reject_stdin_operand(operands: Sequence[str]) -> None:
    if not operands or any(op == "-" for op in operands):
        raise Unanswerable("stdin operand (`-` or absent) not supported")


def _parse_cat(args: Sequence[str]) -> ReadSpec:
    operands: List[str] = []
    for tok in args:
        if tok.startswith("-") and tok != "-":
            raise Unanswerable("unsupported cat flag %r" % tok)
        operands.append(tok)
    _reject_redirection_and_substitution(operands)
    _reject_stdin_operand(operands)
    return ReadSpec(kind="cat", operands=operands)


def _parse_head_tail(verb: str, args: Sequence[str]) -> ReadSpec:
    count: Optional[int] = None
    operands: List[str] = []
    it = list(args)
    i = 0
    while i < len(it):
        tok = it[i]
        if tok == "-n":
            if count is not None:
                raise Unanswerable("multiple %s -n flags" % verb)
            if i + 1 >= len(it):
                raise Unanswerable("%s -n is missing its value" % verb)
            value = it[i + 1]
            if not value.isdigit():
                raise Unanswerable("%s -n expects a non-negative integer, got %r"
                                   % (verb, value))
            count = int(value)
            i += 2
            continue
        if tok.startswith("-") and tok != "-" and tok[1:].isdigit():
            if count is not None:
                raise Unanswerable("multiple %s count flags" % verb)
            count = int(tok[1:])
            i += 1
            continue
        if tok.startswith("-"):
            raise Unanswerable("unsupported %s flag %r" % (verb, tok))
        operands.append(tok)
        i += 1

    _reject_redirection_and_substitution(operands)
    _reject_stdin_operand(operands)
    if len(operands) != 1:
        # Brief: multi-file head/tail prints `==> path <==` headers -- a fidelity
        # trap relative to how rarely the shape is used. Only single-file accepted.
        raise Unanswerable("%s: only the single-file form is supported" % verb)
    return ReadSpec(kind=verb, operands=operands, count=10 if count is None else count)


def _parse_sed(args: Sequence[str]) -> ReadSpec:
    it = list(args)
    if len(it) != 3 or it[0] != "-n":
        raise Unanswerable("sed: only `-n '<a>,<b>p'`/`'<a>p'` FILE is supported")
    program, operand = it[1], it[2]

    m = _SED_RANGE_RE.match(program)
    if m:
        a, b = m.group("a"), m.group("b")
        start_is_last = a == "$"
        end_is_last = b == "$"
        start = None if start_is_last else int(a)
        end = None if end_is_last else int(b)
    else:
        m = _SED_SINGLE_RE.match(program)
        if not m:
            raise Unanswerable("sed program %r not supported" % program)
        a = m.group("a")
        start_is_last = a == "$"
        start = None if start_is_last else int(a)
        end, end_is_last = start, start_is_last

    _reject_redirection_and_substitution([operand])
    _reject_stdin_operand([operand])
    return ReadSpec(kind="sed", operands=[operand], start=start, end=end,
                    start_is_last=start_is_last, end_is_last=end_is_last)


def _read_text_strict(path: str) -> str:
    """Read `path` as strict UTF-8 text, or raise `Unanswerable`.

    Deliberately stricter than `engine._read_text` -- see module docstring.
    Reuses only that function's OSError discipline: an unreadable file must
    refuse, never render as an empty/absent body.
    """
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
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unanswerable("%r does not decode as UTF-8: %s" % (path, exc))
