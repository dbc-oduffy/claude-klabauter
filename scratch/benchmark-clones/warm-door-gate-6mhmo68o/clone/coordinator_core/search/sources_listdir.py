"""coordinator_core.search.sources_listdir -- answer an `ls`-shaped Bash command
IN-PROCESS, reproducing the session's own collation rather than refusing to.

Purpose: given the first segment's tokens of an `ls` invocation, either return an
object that produces the exact lines real `ls` would print to a non-tty stdout (one
entry per line -- the harness never gives `ls` a tty), or raise
``coordinator_core.search.engine.Unanswerable``.

Why this is a SEPARATE module from `sources_read.py` (C1)
-----------------------------------------------------------
`cat`/`head`/`tail`/`sed -n` reproduce a file's own bytes -- the trap there is
metachar/redirect operand parsing. `ls`'s trap is different in KIND: its output
ORDER depends on the session's ambient locale (`LC_ALL`/`LC_COLLATE`/`LANG`), not
on file bytes alone. A `C`/`POSIX`-only implementation would be simpler to write
and to test, but it would silently mis-order every session running a UTF-8 locale
-- which is the common case, not the exception -- so this module reproduces the
locale's collation rather than serving only the byte-sort case.

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT answer `-l`/`-R`/`-t`/`-S`/`-r`/`-F`/`--color`, multiple operands, a
    glob operand, a file operand (rather than a directory), or an unreadable
    directory. Each of these is either a fidelity swamp (`-l`'s mtime/size/perm
    rendering) or an ordering rule other than name (`-t`/`-S`/`-r`) this module
    does not implement -- refusing is strictly better than a plausible-looking
    wrong answer.
  - Does NOT approximate collation. `C`/`POSIX` falls out as the byte-sort case;
    every other locale is resolved via `locale.setlocale(locale.LC_COLLATE, ...)`
    plus `functools.cmp_to_key(locale.strcoll)`, read from `os.environ` at
    RECOGNITION time (the session's actual environment), never the test runner's.
    A locale this Python cannot install, or whose `strcoll` this platform cannot
    reproduce faithfully, is a NAMED decline (`Unanswerable`), never a silent
    fall-back to byte order.
  - Does NOT treat the first-segment redirect/substitution check as optional. The
    shared tokenizer (`_command_tokenizer.segments_from_tokens_with_pipe_flag`)
    splits segments only at unquoted `;`/`&`/`|`, so `ls -1 > f` arrives as one
    ordinary-looking token list. Declining any first segment containing a
    redirection or substitution token, BEFORE parsing flags or the directory
    operand, is what keeps a rewritten `ls` from silently eating a real redirect
    (see `sources_read.py`'s docstring for the fuller incident shape this guards
    against).
  - Does NOT leave the process locale mutated. `setlocale` is process-global and
    not thread-safe; the previous `LC_COLLATE` setting is saved and restored even
    though the hook process is short-lived and single-threaded, because the rest
    of the guard chain runs in the same process afterward.
"""

from __future__ import annotations

import functools
import locale
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from coordinator_core.search.engine import Unanswerable

#: Redirection operators that must decline the whole first segment if present as
#: an exact token -- checked before any flag/operand parsing, same discipline as
#: `sources_read.py` (C1) applies to its own operand list.
_REDIRECT_TOKENS = frozenset({
    ">", ">>", "<", "<<", "<<<", "2>", "&>", "|&",
})

#: Substring markers of command/process substitution -- checked as substrings
#: because the shared tokenizer does not split `$(...)`/`` `...` ``/`<(...)` off
#: from an adjoining token.
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", "$")

#: Locale identifiers that fall out as the plain byte-sort case rather than being
#: routed through `setlocale`/`strcoll` at all.
_BYTE_SORT_LOCALES = frozenset({"C", "POSIX"})


@dataclass
class LsSpec:
    """A parsed `ls` invocation: which directory, and whether dotfiles show."""

    directory: str = "."
    show_all: bool = False


def _reject_redirection_and_substitution(tokens: Sequence[str]) -> None:
    for tok in tokens:
        if tok in _REDIRECT_TOKENS:
            raise Unanswerable("redirection token %r in ls segment" % tok)
        if any(marker in tok for marker in _SUBSTITUTION_MARKERS):
            raise Unanswerable("substitution token in ls operand %r" % tok)


def parse_ls_segment(tokens: Sequence[str]) -> LsSpec:
    """Parse one `ls`-family argv into an LsSpec, or raise Unanswerable.

    Accepts only: `ls`, `ls DIR`, `ls -1 [DIR]`, `ls -a [DIR]`,
    `ls -1a`/`ls -a1 [DIR]`. Everything else -- `-l`, `-R`, `-t`/`-S`/`-r`,
    `-F`/`--color`, multiple operands, a glob operand -- declines by name.
    """
    if not tokens:
        raise Unanswerable("empty ls segment")
    binary = os.path.basename(tokens[0])
    if binary != "ls":
        raise Unanswerable("not an ls command: %r" % binary)

    _reject_redirection_and_substitution(tokens)

    show_all = False
    operand: Optional[str] = None
    i, n = 1, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            i += 1
            continue
        if tok.startswith("-") and tok != "-" and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "1":
                    continue
                elif ch == "a":
                    show_all = True
                else:
                    # Covers -l/-R/-t/-S/-r/-F and any `--long` option (the `-`
                    # of the second dash is itself an unrecognized flag char).
                    raise Unanswerable("unsupported ls flag -%s" % ch)
            i += 1
            continue
        if operand is not None:
            raise Unanswerable("multiple ls operands not supported")
        operand = tok
        i += 1

    directory = operand if operand is not None else "."
    if any(ch in directory for ch in ("*", "?", "[")):
        raise Unanswerable("glob operand %r not supported" % directory)
    return LsSpec(directory=directory, show_all=show_all)


def _resolve_recognition_locale() -> Optional[str]:
    """Read the session's collation locale from the real process environment.

    Checked in `LC_ALL`, `LC_COLLATE`, `LANG` order -- the same precedence
    `strcoll()` itself resolves under -- at the moment the command is recognized,
    never a value captured earlier or supplied by a test harness.
    """
    for var in ("LC_ALL", "LC_COLLATE", "LANG"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def _ls_collated_sort(entries: List[str]) -> List[str]:
    """Sort `entries` the way real `ls` would under the session's own locale.

    `C`/`POSIX` (including no locale set at all) is the byte-sort case. Any other
    locale is resolved via `setlocale`/`strcoll`, with the previous `LC_COLLATE`
    setting saved and restored regardless of outcome -- `setlocale` is
    process-global, and this hook process is not the only thing that runs in it.
    """
    loc = _resolve_recognition_locale()
    if loc is None or loc.upper() in _BYTE_SORT_LOCALES:
        return sorted(entries)

    previous = locale.setlocale(locale.LC_COLLATE)
    try:
        try:
            locale.setlocale(locale.LC_COLLATE, loc)
        except locale.Error as exc:
            raise Unanswerable(
                "locale %r is not installed in this Python: %s" % (loc, exc)
            )
        try:
            return sorted(entries, key=functools.cmp_to_key(locale.strcoll))
        except (locale.Error, TypeError, ValueError) as exc:
            raise Unanswerable(
                "this platform cannot reproduce strcoll() ordering for "
                "locale %r: %s" % (loc, exc)
            )
    finally:
        locale.setlocale(locale.LC_COLLATE, previous)


def run(spec: LsSpec, cwd: str = ".") -> List[str]:
    """Execute an LsSpec, returning the lines real `ls` would print.

    One entry per line -- what real `ls` does when stdout is not a tty, which is
    always the case under this harness. Declines (raises Unanswerable) rather than
    approximating on: a nonexistent path, a file operand, or an unreadable
    directory.
    """
    base = spec.directory if os.path.isabs(spec.directory) else os.path.join(cwd, spec.directory)
    if not os.path.exists(base):
        raise Unanswerable("ls target %r does not exist" % spec.directory)
    if not os.path.isdir(base):
        raise Unanswerable("ls target %r is not a directory" % spec.directory)
    try:
        entries = os.listdir(base)
    except OSError as exc:
        raise Unanswerable("cannot list %r: %s" % (spec.directory, exc))

    if spec.show_all:
        entries = list(entries) + [".", ".."]
    else:
        entries = [e for e in entries if not e.startswith(".")]

    return _ls_collated_sort(entries)
