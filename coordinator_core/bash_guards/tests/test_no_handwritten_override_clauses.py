"""Standing enforcement for M17: no guard message may hand-write an
override-env-var instruction (`"...export COORDINATOR_OVERRIDE_X=1..."`,
`"...COORDINATOR_ALLOW_Y=1..."`, `"To bypass: ..."`) instead of routing
through `_helpers.operator_override_note` -- the ONE builder every such
sentence must go through (see that function's own docstring for why: the
old phrasing named an action unreachable from inside a live session, and
this class has been declared "closed" twice (once package-wide, once by a
commit literally titled "retire the LAST hand-written 'To bypass: export'
clauses") while ~10 sites still survived both times. A prior session's
CLAIM that the class was closed is not evidence it was closed -- this file
is the artifact that actually discharges the rule, checked by RUNNING it,
not by reading a commit message.

DETECTION STRATEGY -- why this catches a NEW file, not just today's set.
This walks the AST of every ``*.py`` file DISCOVERED by directory listing
under ``coordinator_core/bash_guards/`` (excluding ``tests/`` and
``__pycache__``) -- never a hardcoded file list. A guard module added
next week is picked up automatically the next time this test runs, exactly
like ``discover_module_guard_names()`` in ``_alternative_liveness.py``
already does for guard registration; this is the same "discover, don't
enumerate" discipline applied to message-text hygiene.

For each discovered file, every STRING CONSTANT in the AST (both plain
literals and the constant parts of an f-string) is checked against
``_VIOLATION_RE`` -- ``COORDINATOR_(ALLOW|OVERRIDE|DISABLE)_[A-Z0-9_]+=1``,
the literal "set this env var to 1" instruction that is the actual
fingerprint of a hand-written clause (a bare mention of the env var NAME
with no ``=1`` -- e.g. a module-level ``_OVERRIDE_ENV = "COORDINATOR_
OVERRIDE_X"`` constant, or architectural prose merely NAMING the
variable -- is not itself an instruction and does not match).

MODULE/CLASS/FUNCTION DOCSTRINGS ARE EXEMPT, on purpose, not as a loophole:
they are developer-facing documentation, never emitted to a caller as a
deny/advisory message, and several modules' own docstrings legitimately
QUOTE the old, wrong phrasing while explaining why it was wrong (e.g.
``_blanket_disarm.py``'s own module docstring quotes ``"To bypass: export
COORDINATOR_ALLOW_X=1"`` as the anti-pattern being fixed). A detector that
flagged docstrings would either force-rewrite historical explanation text
into something less clear, or need a per-file allowlist entry for every
module that explains its own history -- both defeat the point. Excluding
docstrings is the correct, narrow carve-out: it exempts EXPLANATION,
never a live message-construction site (a return value, an argument to
``_deny``/``_advisory``/``_allow_rewrite``, a module-level constant later
concatenated into one).

NEGATIVE SPEC -- do not "simplify" this into a hardcoded list of the ~10
sites this dispatch fixed. A fixed list passes vacuously the moment a
NEW file (or a reverted old one) reintroduces the pattern; enumerating
the current file set as the check IS the anti-pattern the team lead's
brief named explicitly ("a hardcoded list of twelve known-good files is
not it"). The whole file set is re-derived by `_discover_guard_files()`
on every run.

ALLOWLIST -- explicit, named, and currently EMPTY. If a future site has a
genuine reason to hand-write this pattern (none identified as of this
dispatch), add a `(relative_path, matched_text)` tuple to `_ALLOWLIST`
below WITH an inline comment naming the reason -- never widen `_VIOLATION_
RE` or add a file-level skip to make an exception, both of which reopen
the pattern for everything else in that file too.

SHADOWED-NAME AMBIGUITY (H3 follow-up, 2026-07-30) -- `_module_string_
bindings` resolves a bare `Name` reference by looking the name up in a
file-wide dict, with no scoping/shadowing awareness, built by walking
every assignment in the file via `ast.walk` (breadth-first, NOT source
order). Two DIFFERENT bindings sharing a name (a real `_ENV =
"COORDINATOR_OVERRIDE_REAL"` in one function, an unrelated `_ENV =
"something_else"` in another) therefore have a traversal-order-dependent
winner -- and if the wrong one wins, a real override clause folds away
silently. This is the FALSE-NEGATIVE mirror of the false-positive risk
the docstring used to admit alone; for a detection gate the false
negative is strictly worse (a real violation passes clean) and it is the
one this module now refuses to let past quietly.

Fix taken: when a name has MORE THAN ONE DISTINCT string value bound to
it across the file, AND at least one of those values contains the
substring `COORDINATOR` (i.e. it is plausibly an env-var-name constant,
not incidental prose), every reference to that name is treated as
UNRESOLVABLE and is itself reported as a violation needing manual
review (`matched="<ambiguous-shadowed-binding>"`) rather than silently
folded one way or the other. This lands the fail-loud direction the
2026-07-30 review asked for: "I cannot tell, look at this" rather than a
gate that guesses and stays quiet.

The `COORDINATOR`-substring gate is a deliberate scope narrowing, not
laziness: names get reused constantly for ordinary local variables
(`reason`, `msg`, ...) across ~20+ files sharing a `check(payload)`
shape, and treating EVERY same-named, differently-valued pair as
ambiguous would flag most of the package on prose that has nothing to
do with an override clause -- noise that would bury the real findings
this gate exists to surface. Scoping to values that look like a
`COORDINATOR_*` constant keeps the check aimed at the actual risk named
above without flooding the gate. A name bound to the SAME value more
than once (no real ambiguity) is not flagged.

OPEN GAP, NAMED NOT SILENT -- a constant imported from another module
(`from _shared import NAME`) is NOT resolved; this scanner is file-local
by construction and closing it would mean parsing every other module a
guard file imports from, which is a materially larger undertaking than
the rest of this file's static folding. If a guard ever hand-writes an
override clause behind a cross-module import alias, this gate will not
catch it. Recorded here deliberately, in the same spirit as the symlink
deferral elsewhere in this package, so a future reader inherits a
bounded claim rather than an implied-total one.

Spec backlink: coordinator_core/bash_guards/_helpers.py (`operator_override_note`)
Prior (false) closure claims: claude-klabauter `fa34b13c` ("retire the LAST
hand-written 'To bypass: export' clauses") -- title and package-wide
closure claim both incorrect; ~10 sites across 5 files survived it.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import List, NamedTuple, Set

import pytest

_BASH_GUARDS_DIR = Path(__file__).resolve().parent.parent

#: The actual fingerprint of a hand-written override clause: the env var
#: NAME immediately followed by `=1` -- i.e. an instruction to literally set
#: it, not merely a mention of its name. See module docstring for why a bare
#: name (no `=1`) must NOT match.
_VIOLATION_RE = re.compile(r"\bCOORDINATOR_(?:ALLOW|OVERRIDE|DISABLE)_[A-Z0-9_]+=1\b")

#: Explicit, named allowlist -- see module docstring "ALLOWLIST". Each entry
#: is `(relative_path_from_bash_guards_dir, matched_text)`. EMPTY as of this
#: dispatch: every site found was routed through `operator_override_note`
#: rather than granted an exception.
_ALLOWLIST: Set[tuple] = set()

#: The substring that marks a bound string value as "plausibly an env-var-
#: name constant" for the shadowed-name ambiguity check -- see module
#: docstring "SHADOWED-NAME AMBIGUITY".
_COORDINATOR_SUBSTR = "COORDINATOR"

#: Sentinel returned by `_fold_string`/`_module_string_bindings` for a `Name`
#: reference whose binding is ambiguous (more than one distinct, COORDINATOR-
#: shaped value shares the name across the file) -- distinct from `None`
#: ("declines to fold, not relevant") and from a concrete `str` ("folded
#: cleanly"). Every combination site in `_fold_string` propagates this
#: sentinel rather than crashing on a non-string operand or silently
#: swallowing it, so an ambiguous reference anywhere inside a composite
#: expression surfaces the whole expression as needing manual review.
class _Ambiguous:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<ambiguous-binding>"


_AMBIGUOUS = _Ambiguous()


class Violation(NamedTuple):
    path: str
    lineno: int
    matched: str
    snippet: str


def _docstring_node_ids(tree: ast.Module) -> Set[int]:
    """Return `id()` of every AST string-constant node that is a MODULE,
    CLASS, or FUNCTION docstring (the first statement of that scope's
    body, when it is a bare string expression) -- these are exempt from
    the scan. See module docstring "MODULE/CLASS/FUNCTION DOCSTRINGS ARE
    EXEMPT"."""
    exempt: Set[int] = set()
    scopes: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node)
    for scope in scopes:
        body = getattr(scope, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            exempt.add(id(first.value))
    return exempt


def _bind(bindings: dict, seen_values: dict, name: str, folded) -> None:
    """Record `folded` as (one of) the binding(s) of `name`, applying the
    shadowed-name ambiguity rule (module docstring "SHADOWED-NAME
    AMBIGUITY"): once a name is marked `_AMBIGUOUS` it stays that way for
    the rest of the file -- a later plain binding of the same name must
    not un-flag it. `folded is None` (declined to fold) is a no-op: it
    neither binds nor clears an existing binding."""
    if folded is _AMBIGUOUS or bindings.get(name) is _AMBIGUOUS:
        bindings[name] = _AMBIGUOUS
        return
    if folded is None:
        return
    distinct = seen_values.setdefault(name, set())
    if distinct and folded not in distinct and (
        _COORDINATOR_SUBSTR in folded
        or any(_COORDINATOR_SUBSTR in v for v in distinct)
    ):
        distinct.add(folded)
        bindings[name] = _AMBIGUOUS
        return
    distinct.add(folded)
    bindings[name] = folded


def _module_string_bindings(tree: ast.Module) -> dict:
    """Best-effort `{name: folded_string_value}` map for every simple
    assignment (`ast.Assign`/`ast.AnnAssign` with a single `Name` target,
    or `ast.AugAssign` with `+=`) whose RHS statically folds to a string
    via `_fold_string`. Deliberately scope-blind (collected across the
    whole module, not per-function, per-`ast.walk`'s own traversal) -- a
    name reused in a different scope with a DIFFERENT, COORDINATOR-shaped
    value is not silently resolved either way; `_bind` marks it
    `_AMBIGUOUS` instead (see module docstring "SHADOWED-NAME AMBIGUITY").
    This is what closes the real evasion shape: `_ENV = "COORDINATOR_
    OVERRIDE_X"` declared once, then interpolated into a deny message
    elsewhere in the same file (see H3, 2026-07-30 M13/M19 review finding)
    -- while a same-named, differently-valued, non-COORDINATOR local
    variable (`reason`, `msg`, ...) is left alone rather than flooding the
    gate with unrelated ambiguity noise."""
    bindings: dict = {}
    seen_values: dict = {}
    for node in ast.walk(tree):
        targets = None
        value = None
        is_aug = False
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            targets, value, is_aug = [node.target], node.value, True
        if targets is None:
            continue
        target = targets[0]
        if not isinstance(target, ast.Name):
            continue
        folded = _fold_string(value, bindings)
        if is_aug:
            # `+=` MUTATES the current binding of the same name -- it is not
            # a second, independent declaration competing for the shadow-
            # ambiguity ledger (that would flag ordinary accumulation, e.g.
            # `msg = "..."; msg += "=1."`, as if it shadowed itself). Update
            # `bindings` directly rather than going through `_bind`.
            current = bindings.get(target.id)
            if current is _AMBIGUOUS or folded is _AMBIGUOUS:
                bindings[target.id] = _AMBIGUOUS
            elif current is not None and folded is not None:
                bindings[target.id] = current + folded
            # else: not statically determinable -- leave any existing
            # binding untouched rather than guess.
            continue
        _bind(bindings, seen_values, target.id, folded)
    return bindings


def _fold_mod_rhs(node: ast.AST, bindings: dict):
    """Fold the right-hand operand of `%`-style string formatting: a
    single foldable value, a parenthesized tuple of foldable values
    (`"...%s=1" % name` vs `"...%s=1" % (name,)`), or a `dict` literal of
    foldable string keys/values (`"...%(name)s=1" % {"name": NAME}` --
    the mapping-RHS form the tuple case alone used to miss, H3 2026-07-30
    review finding). Propagates `_AMBIGUOUS` (rather than treating it as
    an ordinary fold failure) the moment any element resolves to it."""
    if isinstance(node, ast.Tuple):
        values = []
        for elt in node.elts:
            folded = _fold_string(elt, bindings)
            if folded is _AMBIGUOUS:
                return _AMBIGUOUS
            if folded is None:
                return None
            values.append(folded)
        return tuple(values)
    if isinstance(node, ast.Dict):
        result = {}
        for key_node, val_node in zip(node.keys, node.values):
            if key_node is None:
                return None  # `**expansion` -- decline, not statically foldable
            key = _fold_string(key_node, bindings)
            if key is _AMBIGUOUS:
                return _AMBIGUOUS
            if key is None:
                return None
            val = _fold_string(val_node, bindings)
            if val is _AMBIGUOUS:
                return _AMBIGUOUS
            if val is None:
                return None
            result[key] = val
        return result
    return _fold_string(node, bindings)


def _fold_string(node: ast.AST, bindings: dict):
    """Best-effort static evaluation of a string-producing expression to its
    concrete value; `None` if it is not statically determinable, or
    `_AMBIGUOUS` if it resolves through a shadowed `Name` binding that
    cannot be safely resolved either way (module docstring "SHADOWED-NAME
    AMBIGUITY"). Covers the evasion shapes H3 (2026-07-30 M13/M19 review
    finding) named: an f-string interpolating a foldable sub-expression,
    `+`-concatenation of two foldable operands, `%`-style formatting
    (bare value, tuple, or dict RHS), `str.format()`, `str.join()`,
    `str.replace()`, and a `bytes` literal's `.decode()` -- each of which
    can carry the full ``NAME=1`` instruction with no SINGLE
    ``ast.Constant`` node containing it whole, which is exactly what let
    the plain per-Constant scan miss them. Recurses through nested
    combinations of the same shapes (e.g. a `.format()` call whose
    template is itself a `+`-concatenation).

    Every combination site checks for `_AMBIGUOUS` in its operands BEFORE
    checking for `None`, and propagates it immediately -- an ambiguous
    sub-expression makes the whole composite un-resolvable, not merely
    "not this specific shape".

    Not a general-purpose partial evaluator -- returns `None` (declines to
    fold) rather than guess at anything involving a call this function does
    not enumerate, an f-string conversion/format-spec it cannot resolve, or
    any other dynamic construct. A missed fold is a false negative for THIS
    detector's extended reach, not a false negative for the base per-
    Constant scan, which still runs independently and unconditionally.

    KNOWN OPEN GAP: a name imported from another module (`from _shared
    import NAME`) is not resolved -- this function and `_module_string_
    bindings` are both file-local by construction. See module docstring
    "OPEN GAP, NAMED NOT SILENT"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                if value.format_spec is not None:
                    return None
                folded = _fold_string(value.value, bindings)
                if folded is _AMBIGUOUS:
                    return _AMBIGUOUS
                if folded is None:
                    return None
                parts.append(folded)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _fold_string(node.left, bindings)
            if left is _AMBIGUOUS:
                return _AMBIGUOUS
            right = _fold_string(node.right, bindings)
            if right is _AMBIGUOUS:
                return _AMBIGUOUS
            if left is None or right is None:
                return None
            return left + right
        if isinstance(node.op, ast.Mod):
            left = _fold_string(node.left, bindings)
            if left is _AMBIGUOUS:
                return _AMBIGUOUS
            if left is None:
                return None
            rhs = _fold_mod_rhs(node.right, bindings)
            if rhs is _AMBIGUOUS:
                return _AMBIGUOUS
            if rhs is None:
                return None
            try:
                return left % rhs
            except Exception:
                return None
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "decode":
            base_node = func.value
            if not (isinstance(base_node, ast.Constant) and isinstance(base_node.value, bytes)):
                return None
            encoding = "utf-8"
            if node.args:
                encoding = _fold_string(node.args[0], bindings)
                if encoding is _AMBIGUOUS:
                    return _AMBIGUOUS
                if encoding is None:
                    return None
            try:
                return base_node.value.decode(encoding)
            except Exception:
                return None
        if not isinstance(func, ast.Attribute) or node.keywords and any(
            kw.arg is None for kw in node.keywords
        ):
            return None
        if func.attr == "format":
            base = _fold_string(func.value, bindings)
            if base is _AMBIGUOUS:
                return _AMBIGUOUS
            if base is None:
                return None
            args = []
            for arg in node.args:
                folded = _fold_string(arg, bindings)
                if folded is _AMBIGUOUS:
                    return _AMBIGUOUS
                if folded is None:
                    return None
                args.append(folded)
            kwargs = {}
            for kw in node.keywords:
                folded = _fold_string(kw.value, bindings)
                if folded is _AMBIGUOUS:
                    return _AMBIGUOUS
                if folded is None:
                    return None
                kwargs[kw.arg] = folded
            try:
                return base.format(*args, **kwargs)
            except Exception:
                return None
        if func.attr == "join":
            sep = _fold_string(func.value, bindings)
            if sep is _AMBIGUOUS:
                return _AMBIGUOUS
            if sep is None or not node.args:
                return None
            seq_node = node.args[0]
            if not isinstance(seq_node, (ast.List, ast.Tuple)):
                return None
            elements = []
            for elt in seq_node.elts:
                folded = _fold_string(elt, bindings)
                if folded is _AMBIGUOUS:
                    return _AMBIGUOUS
                if folded is None:
                    return None
                elements.append(folded)
            try:
                return sep.join(elements)
            except Exception:
                return None
        if func.attr == "replace":
            if len(node.args) != 2:
                return None  # `count=` or other non-2-positional-arg form -- decline
            base = _fold_string(func.value, bindings)
            if base is _AMBIGUOUS:
                return _AMBIGUOUS
            if base is None:
                return None
            old = _fold_string(node.args[0], bindings)
            if old is _AMBIGUOUS:
                return _AMBIGUOUS
            new = _fold_string(node.args[1], bindings)
            if new is _AMBIGUOUS:
                return _AMBIGUOUS
            if old is None or new is None:
                return None
            try:
                return base.replace(old, new)
            except Exception:
                return None
        return None
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    return None


#: AST node types `find_handwritten_override_clauses` attempts to fold as a
#: COMPOSITE string expression, in addition to the plain per-`ast.Constant`
#: scan. Checked at every node visited by `ast.walk` -- a composite may
#: nest inside another (a `.format()` call whose template is itself a `+`
#: BinOp), and `ast.walk` already visits every node regardless of nesting,
#: so no separate recursive descent is needed here beyond `_fold_string`'s
#: own recursion into sub-expressions.
#:
#: `ast.Name` is included so a BARE reference to a bound name (e.g.
#: `return msg` after `msg = "..."; msg += "=1."`) is itself scanned, not
#: only a Name nested inside one of the other composite shapes -- see the
#: `+=`-accumulation evasion form in the module docstring. The scan loop
#: restricts this to `ast.Load` context so an assignment TARGET is never
#: independently re-scanned as if it were a use.
_COMPOSITE_NODE_TYPES = (ast.JoinedStr, ast.BinOp, ast.Call, ast.Name)


def _discover_guard_files() -> List[Path]:
    """Every `*.py` file directly under `coordinator_core/bash_guards/`
    (never `tests/`, never `__pycache__`) -- discovered by directory
    listing, not a hardcoded name list, so a file added tomorrow is
    included the next time this runs."""
    return sorted(
        p for p in _BASH_GUARDS_DIR.glob("*.py")
        if p.name != "__init__.py"
    )


def find_handwritten_override_clauses(paths: List[Path]) -> List[Violation]:
    """Scan `paths` for a hand-written override clause matching
    `_VIOLATION_RE`, skipping anything in `_ALLOWLIST`. Never raises on a
    file that fails to parse -- a syntax-broken file is a different
    problem this test does not own; it is reported as a violation with a
    distinct marker so it is not silently skipped either.

    Two passes, deliberately kept separate:

    1. Every non-docstring plain `ast.Constant` string -- the original
       scan, unchanged.
    2. Every COMPOSITE string-producing expression (`ast.JoinedStr`,
       `ast.BinOp`, `ast.Call` -- see `_COMPOSITE_NODE_TYPES`) that
       statically folds to a concrete string via `_fold_string` (H3,
       2026-07-30 M13/M19 review finding): an f-string interpolating the
       env-var NAME, a `+`-concatenation split across the `NAME`/`=1`
       boundary, `%`-formatting (bare/tuple/dict RHS), `.format()`,
       `.join()`, `.replace()`, or a `bytes.decode()` can each carry the
       full instruction with no single literal containing it whole, which
       is exactly what pass 1 alone misses. A composite node can never
       itself be a docstring (Python only recognizes a bare
       `ast.Constant` string as one), so no docstring-exemption check
       applies to pass 2. If a composite instead resolves to `_AMBIGUOUS`
       (a shadowed, COORDINATOR-shaped `Name` binding -- module docstring
       "SHADOWED-NAME AMBIGUITY"), it is reported as its own violation
       needing manual review rather than silently folded either way.

    Deduplicated by `(path, lineno, matched)` -- a composite fold and the
    plain-Constant scan can occasionally flag the same instruction twice
    (e.g. a fully-literal f-string with no interpolation at all also
    matches as its own JoinedStr fold), which is a reporting nicety, not a
    correctness concern either way."""
    violations: List[Violation] = []
    seen: Set[tuple] = set()
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(
                Violation(path=str(path), lineno=0, matched="<unparseable>", snippet=str(exc))
            )
            continue

        exempt_ids = _docstring_node_ids(tree)
        bindings = _module_string_bindings(tree)
        rel = str(path.relative_to(_BASH_GUARDS_DIR)) if path.is_relative_to(_BASH_GUARDS_DIR) else str(path)

        def _record(value: str, lineno: int) -> None:
            for m in _VIOLATION_RE.finditer(value):
                matched = m.group(0)
                if (rel, matched) in _ALLOWLIST:
                    continue
                key = (rel, lineno, matched)
                if key in seen:
                    continue
                seen.add(key)
                start = max(0, m.start() - 40)
                end = min(len(value), m.end() + 20)
                snippet = value[start:end]
                violations.append(Violation(path=rel, lineno=lineno, matched=matched, snippet=snippet))

        def _record_ambiguous(lineno: int) -> None:
            key = (rel, lineno, "<ambiguous-shadowed-binding>")
            if key in seen:
                return
            seen.add(key)
            violations.append(Violation(
                path=rel,
                lineno=lineno,
                matched="<ambiguous-shadowed-binding>",
                snippet=(
                    "a Name referenced here resolves through more than one "
                    "differing, COORDINATOR-shaped binding in this file -- "
                    "cannot safely determine whether this composite string is "
                    "a hand-written override clause; rename one binding or "
                    "resolve the shadowing"
                ),
            ))

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in exempt_ids:
                    continue
                _record(node.value, getattr(node, "lineno", 0))
                continue
            if isinstance(node, _COMPOSITE_NODE_TYPES):
                if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                    continue
                folded = _fold_string(node, bindings)
                if folded is _AMBIGUOUS:
                    _record_ambiguous(getattr(node, "lineno", 0))
                elif folded is not None:
                    _record(folded, getattr(node, "lineno", 0))
    return violations


def test_bash_guards_package_carries_no_handwritten_override_clause():
    """THE gate. Every hand-written 'export COORDINATOR_OVERRIDE_X=1'-shaped
    clause found across the package must be routed through
    `operator_override_note` instead. See module docstring for the full
    detection strategy and why this is not a hardcoded-file-list check."""
    files = _discover_guard_files()
    assert len(files) >= 20, (
        "sanity check: discovery found suspiciously few files (%d) -- "
        "did _BASH_GUARDS_DIR resolve correctly?" % len(files)
    )
    violations = find_handwritten_override_clauses(files)
    assert not violations, (
        "hand-written override-env-var clause(s) found outside "
        "operator_override_note -- route each through that builder "
        "instead (see coordinator_core/bash_guards/_helpers.py):\n"
        + "\n".join(
            "  %s:%d -- %r (context: ...%s...)" % (v.path, v.lineno, v.matched, v.snippet)
            for v in violations
        )
    )


class TestDetectorSelfTest:
    """Positive AND negative controls against the detector itself, using
    synthetic source written to a temp file -- proves the detector fires
    on the exact defect shape and does NOT fire on prose merely mentioning
    the variable name, a docstring quoting the old pattern historically,
    or an allowlisted exception. An all-negative suite here would pass
    against a detector that always returns an empty list, which is exactly
    the tautological-pin failure mode this package has been bitten by
    before -- the positive controls close that gap.
    """

    def test_positive_hand_written_export_clause_is_caught(self, tmp_path):
        src = textwrap.dedent(
            '''
            """Module docstring, unrelated."""

            def check(payload):
                return {
                    "hookSpecificOutput": {
                        "permissionDecisionReason": (
                            "BLOCKED: some new guard. To bypass: export "
                            "COORDINATOR_OVERRIDE_SOME_NEW_GUARD=1."
                        )
                    }
                }
            '''
        )
        f = tmp_path / "fake_new_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_SOME_NEW_GUARD=1"

    def test_positive_bare_var_equals_one_without_export_word_is_caught(self, tmp_path):
        """Mirrors the real CLAUDEMD_BUDGET/REGISTRATION_QUAD shape this
        dispatch fixed -- no literal word 'export', still an instruction.
        Two sites now legitimately report the same instruction: the literal
        assignment (pass 1) AND the bare `reason` reference passed to
        `_deny()` (the `ast.Name` composite added for the `+=`-accumulation
        fix) -- both true positives at different line numbers, not a
        duplicate-counting bug, so this asserts on content rather than an
        exact count."""
        src = textwrap.dedent(
            '''
            def check(payload):
                reason = "Emergency override (logged): COORDINATOR_OVERRIDE_FOO=1"
                return _deny(reason)
            '''
        )
        f = tmp_path / "fake_bare_instruction.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert violations
        assert all(v.matched == "COORDINATOR_OVERRIDE_FOO=1" for v in violations)

    def test_negative_routed_through_builder_is_not_caught(self, tmp_path):
        src = textwrap.dedent(
            '''
            """Module docstring, unrelated."""

            from coordinator_core.bash_guards._helpers import operator_override_note

            _OVERRIDE_ENV = "COORDINATOR_OVERRIDE_SOME_NEW_GUARD"

            def check(payload):
                return {
                    "hookSpecificOutput": {
                        "permissionDecisionReason": (
                            "BLOCKED: some new guard.\\n\\n"
                            + operator_override_note(_OVERRIDE_ENV)
                        )
                    }
                }
            '''
        )
        f = tmp_path / "fake_routed_guard.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_negative_bare_var_name_with_no_equals_one_is_not_caught(self, tmp_path):
        """A module-level constant naming the env var (no `=1`) is not
        itself an instruction -- e.g. `_OVERRIDE_ENV = "COORDINATOR_
        OVERRIDE_X"`, or prose merely naming it. Every real guard in this
        package has constants shaped exactly like this; a detector that
        flagged them would false-positive on every guard, including ones
        already correctly routed."""
        src = textwrap.dedent(
            '''
            """Module docstring, unrelated."""

            _OVERRIDE_ENV = "COORDINATOR_OVERRIDE_SOME_NEW_GUARD"

            def check(payload):
                if payload.get(_OVERRIDE_ENV):
                    return None
            '''
        )
        f = tmp_path / "fake_bare_name_only.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_negative_docstring_quoting_the_old_pattern_historically_is_not_caught(self, tmp_path):
        """Mirrors `_blanket_disarm.py`'s own module docstring, which
        legitimately quotes the OLD wrong phrasing while explaining why it
        was replaced -- this must not force that historical explanation to
        be rewritten or allowlisted."""
        src = textwrap.dedent(
            '''
            """Fixes the old pattern of "To bypass: export
            COORDINATOR_ALLOW_X=1" -- unreachable from inside a session.
            """

            def check(payload):
                return None
            '''
        )
        f = tmp_path / "fake_history_docstring.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_negative_function_docstring_is_also_exempt(self, tmp_path):
        src = textwrap.dedent(
            '''
            def check(payload):
                """Explains that COORDINATOR_OVERRIDE_X=1 used to be
                hand-written here; now routed through the builder."""
                return None
            '''
        )
        f = tmp_path / "fake_function_docstring.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_positive_fstring_constant_part_is_caught(self, tmp_path):
        """The violation text can appear in the literal (non-interpolated)
        portion of an f-string, not only a plain string literal."""
        src = textwrap.dedent(
            '''
            def check(payload):
                shape = "whatever"
                return f"Denied ({shape}). To bypass: export COORDINATOR_ALLOW_FSTRING_CASE=1."
            '''
        )
        f = tmp_path / "fake_fstring_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_ALLOW_FSTRING_CASE=1"

    def test_positive_fstring_interpolating_the_var_name_is_caught(self, tmp_path):
        """H3 evasion form 1: the env-var NAME is interpolated, not the
        `=1` instruction -- no single literal in the f-string contains the
        full pattern, only the folded whole."""
        src = textwrap.dedent(
            '''
            def check(payload):
                var_name = "COORDINATOR_OVERRIDE_FSTRING_INTERP"
                return f"To bypass: export {var_name}=1."
            '''
        )
        f = tmp_path / "fake_fstring_interp_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_FSTRING_INTERP=1"

    def test_positive_plus_binop_split_across_the_boundary_is_caught(self, tmp_path):
        """H3 evasion form 2: `+`-concatenation split so neither operand
        alone contains the full `NAME=1` pattern."""
        src = textwrap.dedent(
            '''
            def check(payload):
                return "To bypass: export COORDINATOR_OVERRIDE_BINOP_" + "SPLIT=1."
            '''
        )
        f = tmp_path / "fake_binop_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_BINOP_SPLIT=1"

    def test_positive_percent_formatting_is_caught(self, tmp_path):
        """H3 evasion form 3: old-style `%` formatting."""
        src = textwrap.dedent(
            '''
            def check(payload):
                name = "COORDINATOR_OVERRIDE_PERCENT_CASE"
                return "To bypass: export %s=1." % name
            '''
        )
        f = tmp_path / "fake_percent_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_PERCENT_CASE=1"

    def test_positive_percent_formatting_tuple_rhs_is_caught(self, tmp_path):
        """`%` with an explicit tuple RHS (`% (name,)`), distinct code path
        from the bare-value RHS above."""
        src = textwrap.dedent(
            '''
            def check(payload):
                name = "COORDINATOR_OVERRIDE_PERCENT_TUPLE"
                return "To bypass: export %s=1." % (name,)
            '''
        )
        f = tmp_path / "fake_percent_tuple_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_PERCENT_TUPLE=1"

    def test_positive_dot_format_is_caught(self, tmp_path):
        """H3 evasion form 4: `.format()`."""
        src = textwrap.dedent(
            '''
            def check(payload):
                name = "COORDINATOR_OVERRIDE_FORMAT_CASE"
                return "To bypass: export {}=1.".format(name)
            '''
        )
        f = tmp_path / "fake_format_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_FORMAT_CASE=1"

    def test_positive_str_join_is_caught(self, tmp_path):
        """H3 evasion form 5: `str.join()` assembling the pieces."""
        src = textwrap.dedent(
            '''
            def check(payload):
                return "".join(["To bypass: export ", "COORDINATOR_OVERRIDE_JOIN_CASE", "=1."])
            '''
        )
        f = tmp_path / "fake_join_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_JOIN_CASE=1"

    def test_positive_module_level_binding_resolved_across_the_file_is_caught(self, tmp_path):
        """The evasion doesn't even need the binding local to the same
        expression -- a module-level constant declared once and referenced
        by name later must still resolve (`_module_string_bindings`)."""
        src = textwrap.dedent(
            '''
            _NAME = "COORDINATOR_OVERRIDE_MODULE_BINDING"

            def check(payload):
                return f"To bypass: export {_NAME}=1."
            '''
        )
        f = tmp_path / "fake_module_binding_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_MODULE_BINDING=1"

    def test_positive_percent_dict_rhs_is_caught(self, tmp_path):
        """H3 finding: `%`-formatting with a mapping RHS (`%(name)s`), the
        form the tuple-RHS handling alone used to miss."""
        src = textwrap.dedent(
            '''
            def check(payload):
                name = "COORDINATOR_OVERRIDE_PERCENT_DICT"
                return "To bypass: export %(var)s=1." % {"var": name}
            '''
        )
        f = tmp_path / "fake_percent_dict_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_PERCENT_DICT=1"

    def test_positive_str_replace_is_caught(self, tmp_path):
        """H3 finding: `str.replace()` -- 'ordinary Python a developer
        reaches for without thinking of it as evasion'. The base literal
        deliberately does NOT itself match `_VIOLATION_RE` (no `=1` until
        after the replace) so this proves the composite fold specifically,
        not a coincidental pass-1 hit on the unreplaced literal."""
        src = textwrap.dedent(
            '''
            def check(payload):
                return "To bypass: export COORDINATOR_OVERRIDE_REPLACE_CASE_TOKEN.".replace(
                    "_TOKEN.", "=1."
                )
            '''
        )
        f = tmp_path / "fake_replace_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_REPLACE_CASE=1"

    def test_positive_augassign_accumulation_is_caught(self, tmp_path):
        """H3 finding: `+=` accumulation splitting the instruction across
        two statements -- no single expression contains it whole."""
        src = textwrap.dedent(
            '''
            def check(payload):
                msg = "To bypass: export COORDINATOR_OVERRIDE_AUGASSIGN_CASE"
                msg += "=1."
                return msg
            '''
        )
        f = tmp_path / "fake_augassign_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_AUGASSIGN_CASE=1"

    def test_positive_bytes_decode_is_caught(self, tmp_path):
        """H3 finding: a `bytes` literal + `.decode()`, which fails both the
        plain scan's `isinstance(node.value, str)` check and the original
        `Call` handling (only `.format`/`.join`)."""
        src = textwrap.dedent(
            '''
            def check(payload):
                return b"To bypass: export COORDINATOR_OVERRIDE_BYTES_CASE=1.".decode()
            '''
        )
        f = tmp_path / "fake_bytes_decode_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_BYTES_CASE=1"

    def test_positive_shadowed_coordinator_name_is_flagged_ambiguous_not_silently_resolved(self, tmp_path):
        """The false-negative direction of shadowing (H3 follow-up, 2026-07-
        30): a REAL override-env-var name, shadowed by an unrelated later
        binding of the same name, must not silently resolve to either value
        -- it must be reported for manual review instead of passing clean.
        Both the f-string reference in `check_one` and the bare `return
        _ENV` in `check_two` independently resolve through the same
        ambiguous binding, so more than one flagged site is correct here,
        not over-counting -- assert on content, not an exact count."""
        src = textwrap.dedent(
            '''
            def check_one(payload):
                _ENV = "COORDINATOR_OVERRIDE_SHADOW_REAL"
                return f"To bypass: export {_ENV}=1."

            def check_two(payload):
                _ENV = "something_unrelated"
                return _ENV
            '''
        )
        f = tmp_path / "fake_shadowed_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert violations
        assert all(v.matched == "<ambiguous-shadowed-binding>" for v in violations)

    def test_negative_shadowed_non_coordinator_name_does_not_flood_the_gate(self, tmp_path):
        """The mirror check: an ordinary local variable name (`reason`)
        reused with different prose values across two functions is NOT
        COORDINATOR-shaped and must not be flagged -- otherwise every guard
        module sharing a `check(payload)` shape would light up on unrelated
        variable reuse, burying the real findings."""
        src = textwrap.dedent(
            '''
            def check_one(payload):
                reason = "first unrelated denial reason"
                return f"Denied: {reason}"

            def check_two(payload):
                reason = "second, different, unrelated denial reason"
                return f"Denied: {reason}"
            '''
        )
        f = tmp_path / "fake_reused_local_guard.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_negative_repeated_identical_coordinator_binding_is_not_ambiguous(self, tmp_path):
        """A name bound to the SAME value more than once is not really
        ambiguous -- only a DIFFERING value triggers the flag."""
        src = textwrap.dedent(
            '''
            def check_one(payload):
                _ENV = "COORDINATOR_OVERRIDE_REPEATED_CASE"
                return _ENV

            def check_two(payload):
                _ENV = "COORDINATOR_OVERRIDE_REPEATED_CASE"
                return f"To bypass: export {_ENV}=1."
            '''
        )
        f = tmp_path / "fake_repeated_binding_guard.py"
        f.write_text(src, encoding="utf-8")
        violations = find_handwritten_override_clauses([f])
        assert len(violations) == 1
        assert violations[0].matched == "COORDINATOR_OVERRIDE_REPEATED_CASE=1"

    def test_negative_format_with_unresolvable_arg_does_not_false_positive(self, tmp_path):
        """`_fold_string` declines (returns `None`) rather than guess when an
        argument isn't statically foldable -- a `.format()` call over a
        genuinely dynamic value must not be flagged."""
        src = textwrap.dedent(
            '''
            def check(payload):
                return "Denied: {}".format(payload.get("reason"))
            '''
        )
        f = tmp_path / "fake_dynamic_format_guard.py"
        f.write_text(src, encoding="utf-8")
        assert find_handwritten_override_clauses([f]) == []

    def test_allowlist_entry_suppresses_a_named_exception(self, tmp_path, monkeypatch):
        src = textwrap.dedent(
            '''
            def check(payload):
                return "To bypass: export COORDINATOR_OVERRIDE_ALLOWLISTED_CASE=1."
            '''
        )
        f = tmp_path / "fake_allowlisted_guard.py"
        f.write_text(src, encoding="utf-8")
        rel = str(f.relative_to(_BASH_GUARDS_DIR)) if f.is_relative_to(_BASH_GUARDS_DIR) else str(f)
        import coordinator_core.bash_guards.tests.test_no_handwritten_override_clauses as this_module

        monkeypatch.setattr(
            this_module,
            "_ALLOWLIST",
            {(rel, "COORDINATOR_OVERRIDE_ALLOWLISTED_CASE=1")},
        )
        assert find_handwritten_override_clauses([f]) == []


class TestDiscoveryCatchesANewFile:
    """Proves the discovery mechanism itself (not just the regex) picks up
    a file that did not exist when this test module was written -- the
    property the team lead's brief specifically asked to be demonstrated:
    'it must catch a newly added hand-written clause in a new guard
    module, not just re-assert today's known set.'"""

    def test_a_brand_new_file_dropped_into_the_real_package_is_scanned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.bash_guards.tests.test_no_handwritten_override_clauses._BASH_GUARDS_DIR",
            tmp_path,
        )
        (tmp_path / "existing_guard.py").write_text('"""doc."""\ndef check(p):\n    return None\n', encoding="utf-8")
        files_before = _discover_guard_files()
        assert len(files_before) == 1

        (tmp_path / "brand_new_guard_added_later.py").write_text(
            textwrap.dedent(
                '''
                """doc."""
                def check(payload):
                    return "To bypass: export COORDINATOR_ALLOW_BRAND_NEW=1."
                '''
            ),
            encoding="utf-8",
        )
        files_after = _discover_guard_files()
        assert len(files_after) == 2

        violations = find_handwritten_override_clauses(files_after)
        assert len(violations) == 1
        assert "brand_new_guard_added_later.py" in violations[0].path


class TestEvasionFormsCaughtThroughTheRealGate:
    """H3's "prove it" requirement (2026-07-30 M13/M19 review): a gate
    asserted to catch something is not a gate SEEN to catch it. Every
    class above proves the detector against a synthetic `tmp_path` file --
    this class instead plants each evasion form as a real, throwaway `.py`
    file directly inside the LIVE `coordinator_core/bash_guards/` directory
    (the one `--plugin-dir` resolves for every session on this machine),
    runs the exact same discovery + scan `test_bash_guards_package_carries_
    no_handwritten_override_clause` runs, confirms it is caught, and
    deletes the file -- proving reachability through the real directory
    listing, not only through a hand-picked file list handed to the scan
    function directly.

    The throwaway file is removed in a `finally` block so a failing
    assertion still cleans up rather than leaving evasion-shaped source
    sitting in the live guard package."""

    _EVASION_FORMS = {
        "fstring_interp": (
            'def check(payload):\n'
            '    var_name = "COORDINATOR_OVERRIDE_REALGATE_FSTRING"\n'
            '    return f"To bypass: export {var_name}=1."\n',
            "COORDINATOR_OVERRIDE_REALGATE_FSTRING=1",
        ),
        "binop_split": (
            'def check(payload):\n'
            '    return "To bypass: export COORDINATOR_OVERRIDE_REALGATE_BINOP_" + "SPLIT=1."\n',
            "COORDINATOR_OVERRIDE_REALGATE_BINOP_SPLIT=1",
        ),
        "percent": (
            'def check(payload):\n'
            '    name = "COORDINATOR_OVERRIDE_REALGATE_PERCENT"\n'
            '    return "To bypass: export %s=1." % name\n',
            "COORDINATOR_OVERRIDE_REALGATE_PERCENT=1",
        ),
        "dot_format": (
            'def check(payload):\n'
            '    name = "COORDINATOR_OVERRIDE_REALGATE_FORMAT"\n'
            '    return "To bypass: export {}=1.".format(name)\n',
            "COORDINATOR_OVERRIDE_REALGATE_FORMAT=1",
        ),
        "str_join": (
            'def check(payload):\n'
            '    return "".join(["To bypass: export ", "COORDINATOR_OVERRIDE_REALGATE_JOIN", "=1."])\n',
            "COORDINATOR_OVERRIDE_REALGATE_JOIN=1",
        ),
        "percent_dict": (
            'def check(payload):\n'
            '    name = "COORDINATOR_OVERRIDE_REALGATE_PERCENT_DICT"\n'
            '    return "To bypass: export %(var)s=1." % {"var": name}\n',
            "COORDINATOR_OVERRIDE_REALGATE_PERCENT_DICT=1",
        ),
        "str_replace": (
            'def check(payload):\n'
            '    return "To bypass: export COORDINATOR_OVERRIDE_REALGATE_REPLACE_TOKEN.".replace(\n'
            '        "_TOKEN.", "=1."\n'
            '    )\n',
            "COORDINATOR_OVERRIDE_REALGATE_REPLACE=1",
        ),
        "augassign": (
            'def check(payload):\n'
            '    msg = "To bypass: export COORDINATOR_OVERRIDE_REALGATE_AUGASSIGN"\n'
            '    msg += "=1."\n'
            '    return msg\n',
            "COORDINATOR_OVERRIDE_REALGATE_AUGASSIGN=1",
        ),
        "bytes_decode": (
            'def check(payload):\n'
            '    return b"To bypass: export COORDINATOR_OVERRIDE_REALGATE_BYTES=1.".decode()\n',
            "COORDINATOR_OVERRIDE_REALGATE_BYTES=1",
        ),
        "shadowed_ambiguous": (
            'def check_one(payload):\n'
            '    _ENV = "COORDINATOR_OVERRIDE_REALGATE_SHADOW"\n'
            '    return f"To bypass: export {_ENV}=1."\n\n\n'
            'def check_two(payload):\n'
            '    _ENV = "something_unrelated"\n'
            '    return _ENV\n',
            "<ambiguous-shadowed-binding>",
        ),
    }

    @pytest.mark.parametrize("form_name", sorted(_EVASION_FORMS))
    def test_evasion_form_planted_in_the_real_package_is_caught_then_removed(self, form_name):
        body, expected_matched = self._EVASION_FORMS[form_name]
        throwaway = _BASH_GUARDS_DIR / ("_throwaway_h3_evasion_probe_%s.py" % form_name)
        assert not throwaway.exists(), (
            "a stale throwaway probe from a prior failed run is still on disk -- "
            "remove %s before re-running" % throwaway
        )
        try:
            throwaway.write_text('"""Throwaway H3 evasion probe, deleted by the test."""\n' + body, encoding="utf-8")
            files = _discover_guard_files()
            assert any(p.name == throwaway.name for p in files), (
                "real directory-listing discovery did not pick up the planted file"
            )
            violations = find_handwritten_override_clauses(files)
            matches = [v for v in violations if throwaway.name in v.path]
            # >=1, not ==1: the shadowed_ambiguous and bare-Name-reference
            # forms legitimately surface the same finding at more than one
            # site (e.g. both the f-string use and a separate bare `return
            # _ENV` resolve through the same ambiguous binding) -- that is
            # thoroughness, not double-counting, so every matched site must
            # carry the expected marker rather than exactly one existing.
            assert matches, (
                "the real gate did not catch the %s evasion form planted at %s" % (form_name, throwaway)
            )
            assert all(v.matched == expected_matched for v in matches)
        finally:
            throwaway.unlink(missing_ok=True)
        assert not throwaway.exists()
