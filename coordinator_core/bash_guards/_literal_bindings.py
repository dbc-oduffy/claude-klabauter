"""coordinator_core.bash_guards._literal_bindings -- replace a bare ``$NAME`` /
``${NAME}`` operand with the literal words the same command binds it to.

``bind_literal_operands(segment_argvs)`` takes a command's per-segment argvs in
segment order (the shared tokenizer's output: quotes already removed, before
any prefix peel) and returns a new list of the same length. A segment token that
is a whole bare variable reference becomes the literal words its name is bound
to, when that binding is resolvable; every other token is returned as it was.
Pure string processing: no I/O, no spawn, linear in token count. This module
imports no guard; ``check_test_suite_invocation`` imports it.

A binding is one of:
  * a ``for NAME in w1 ... wn`` header segment;
  * a segment made up only of ``NAME=value`` tokens, optionally led by
    ``export``. The value is split on whitespace.

Resolution rules and traps:
  * A prefix assignment (``FILES=x pytest $FILES``) never binds: bash expands
    ``$FILES`` before the assignment applies. It also marks ``FILES`` as touched
    (see the single-binding rule), because the tokenizer cannot tell it from a
    redirected assignment (``FILES=x >out``).
  * A word is literal only if it holds none of ``$`` backtick ``*`` ``?`` ``[``
    ``{`` ``~`` ``(``. One non-literal word makes the whole binding
    unresolvable: ``$(git ls-files)``, globs and assembled values never bind.
    No table of shapes closes the assembled-variable class.
  * The one exception: a ``for`` list word that is itself a bare ``$NAME``
    resolves once, through ``NAME``'s own literal binding. There is no deeper
    chaining.
  * Single-binding rule: a name that is bound more than once, or touched by any
    other form (``NAME+=``, ``read``, ``declare``, ``local``, ``typeset``,
    ``readonly``, ``mapfile``, ``readarray``, ``unset``, ``getopts``, ``select``,
    ``printf -v``, a ``for`` without ``in``, a second ``for``) is unresolvable.
    A binding written under a reserved word or group opener (``if``, ``do``,
    ``{``, ``(`` ...) is conditional or repeated, so it counts as a touch, not
    a binding.
  * ``eval``, ``source`` and ``.`` can rebind anything: every reference after
    one stays unresolved.
  * A binding applies only to segments after it.
  * ``$env:NAME`` (PowerShell) and a variable embedded in a larger token
    (``tests/$SUITE/``) are out of scope and never replaced.
  * Per-iteration: ``bind_literal_iterations(segment_argvs)`` returns, per
    segment, a non-empty list of argv variants. A ``for``-bound name expands to
    ONE list word per variant (product across distinct such names, capped at
    ``MAX_VARIANTS``; above the cap the ``for``-bound tokens stay unresolved in
    a single variant). Trap: binding all words at once lets one scoped word
    launder a broad one (``for f in tests a/t.py`` runs ``pytest tests`` on the
    first pass). A plain variable is one run, so it keeps all words. An empty
    ``for`` list falls back to the all-words (empty) replacement. Both functions
    share one resolution scan.
  * The argvs carry no ``&&``/``||``/``|`` operators, so a binding after
    ``false &&`` or inside a pipeline stage is treated as unconditional. This is
    an OPEN bypass: ``false && F=tests/a.py; pytest $F`` runs the whole suite
    but binds as scoped. No test or guard enforces it; closing it needs the
    tokenizer to hand operators to ``_resolve``.
"""

from __future__ import annotations

import itertools
import re
from typing import Dict, List, Optional, Sequence

MAX_VARIANTS = 64

_BARE_VAR_RE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.DOTALL)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_LITERAL_CHARS = frozenset("$`*?[{~(")

_RESERVED_PREFIX = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "("})
_TOUCHING_HEADS = frozenset({
    "read", "declare", "local", "typeset", "readonly", "mapfile", "readarray",
    "unset", "getopts", "printf", "export", "select",
})
_POISON_HEADS = frozenset({"eval", "source", "."})


def _is_literal(word: str) -> bool:
    return not any(ch in _NON_LITERAL_CHARS for ch in word)


def _bare_name(token: str) -> Optional[str]:
    m = _BARE_VAR_RE.match(token)
    if m is None:
        return None
    return m.group(1) or m.group(2)


def _peel(argv: Sequence[str]) -> tuple:
    i = 0
    while i < len(argv) and argv[i] in _RESERVED_PREFIX:
        i += 1
    return i, argv[i:]


def _touch_names(head: str, rest: Sequence[str]) -> List[str]:
    """Names a touching builtin may assign, from its non-flag arguments."""
    names: List[str] = []
    for tok in rest:
        if tok.startswith("-"):
            continue
        name = tok.split("=", 1)[0].rstrip("+")
        if _NAME_RE.match(name):
            names.append(name)
    return names


def _resolve(segments: List[List[str]]) -> tuple:
    """One scan for both public functions: ``(resolved, poison_from)`` where
    ``resolved[name] = (segment_index, words, from_for_header)``."""
    # events[name] -> list of (segment_index, words or None); None = touch.
    events: Dict[str, List[tuple]] = {}
    for_names: set = set()
    poison_from: Optional[int] = None

    def note(name: str, idx: int, words: Optional[List[str]]) -> None:
        events.setdefault(name, []).append((idx, words))

    for idx, seg in enumerate(segments):
        if not seg:
            continue
        peeled, rest = _peel(seg)
        if not rest:
            continue
        head = rest[0]
        if head in _POISON_HEADS:
            if poison_from is None:
                poison_from = idx
            continue
        if head == "for":
            if len(rest) >= 2 and _NAME_RE.match(rest[1]):
                name = rest[1]
                if peeled == 0 and len(rest) >= 3 and rest[2] == "in":
                    note(name, idx, rest[3:])
                    for_names.add((name, idx))
                else:
                    note(name, idx, None)
            continue
        if head in _TOUCHING_HEADS:
            args = rest[1:]
            if head == "export":
                matches = [m for m in map(_ASSIGN_RE.match, args) if m]
                pure = peeled == 0 and all(
                    _ASSIGN_RE.match(t) or _NAME_RE.match(t) for t in args
                )
                for m in matches:
                    if pure and m.group(2) == "":
                        note(m.group(1), idx, m.group(3).split())
                    else:
                        note(m.group(1), idx, None)
                continue
            if head == "printf" and "-v" in args:
                at = args.index("-v")
                if at + 1 < len(args) and _NAME_RE.match(args[at + 1]):
                    note(args[at + 1], idx, None)
                continue
            if head == "printf":
                continue
            for name in _touch_names(head, args):
                note(name, idx, None)
            continue
        assigns = [_ASSIGN_RE.match(t) for t in rest]
        if all(assigns):
            if peeled == 0 and all(m.group(2) == "" for m in assigns):
                for m in assigns:
                    note(m.group(1), idx, m.group(3).split())
            else:
                for m in assigns:
                    note(m.group(1), idx, None)
            continue
        for tok in rest:
            m = _ASSIGN_RE.match(tok)
            if m is None:
                break
            note(m.group(1), idx, None)

    # Resolve in event order so a `for` list word `$NAME` sees its own binding.
    resolved: Dict[str, tuple] = {}
    for name, evs in sorted(events.items(), key=lambda kv: kv[1][0][0]):
        if len(evs) != 1 or evs[0][1] is None:
            continue
        idx, words = evs[0]
        flat: List[str] = []
        ok = True
        for w in words:
            ref = _bare_name(w)
            if ref is not None:
                dep = resolved.get(ref)
                if dep is None or dep[0] >= idx:
                    ok = False
                    break
                flat.extend(dep[1])
            elif _is_literal(w):
                flat.append(w)
            else:
                ok = False
                break
        if ok:
            resolved[name] = (idx, flat, (name, idx) in for_names)
    return resolved, poison_from


def _hit(resolved: Dict[str, tuple], tok: str, idx: int) -> Optional[tuple]:
    ref = _bare_name(tok)
    hit = resolved.get(ref) if ref is not None else None
    return hit if hit is not None and hit[0] < idx else None


def bind_literal_operands(segment_argvs: Sequence[Sequence[str]]) -> List[List[str]]:
    """Return ``segment_argvs`` with each resolvable bare ``$NAME`` operand
    replaced by its same-command literal words. Same length as the input;
    unresolvable tokens are left untouched."""
    segments = [list(seg) for seg in segment_argvs]
    resolved, poison_from = _resolve(segments)
    out: List[List[str]] = []
    for idx, seg in enumerate(segments):
        if poison_from is not None and idx > poison_from:
            out.append(seg)
            continue
        new: List[str] = []
        for tok in seg:
            hit = _hit(resolved, tok, idx)
            if hit is not None:
                new.extend(hit[1])
            else:
                new.append(tok)
        out.append(new)
    return out


def bind_literal_iterations(
    segment_argvs: Sequence[Sequence[str]],
) -> List[List[List[str]]]:
    """Per-segment argv variants: a segment referencing a resolved ``for``-bound
    name yields one variant per list word (product across distinct such names);
    every other segment yields the single ``bind_literal_operands`` argv."""
    segments = [list(seg) for seg in segment_argvs]
    resolved, poison_from = _resolve(segments)
    out: List[List[List[str]]] = []
    for idx, seg in enumerate(segments):
        if poison_from is not None and idx > poison_from:
            out.append([seg])
            continue
        loops: Dict[str, List[str]] = {}
        for tok in seg:
            hit = _hit(resolved, tok, idx)
            if hit is not None and hit[2]:
                loops[_bare_name(tok)] = hit[1]
        lists = list(loops.values())
        count = 1
        for words in lists:
            count *= len(words)
        iterate = bool(lists) and 0 < count <= MAX_VARIANTS
        names = list(loops)
        combos = itertools.product(*lists) if iterate else [()]
        variants: List[List[str]] = []
        for combo in combos:
            pick = dict(zip(names, combo))
            new: List[str] = []
            for tok in seg:
                hit = _hit(resolved, tok, idx)
                if hit is None:
                    new.append(tok)
                elif iterate and hit[2]:
                    new.append(pick[_bare_name(tok)])
                elif hit[2] and count > MAX_VARIANTS:
                    new.append(tok)
                else:
                    new.extend(hit[1])
            variants.append(new)
        out.append(variants)
    return out
