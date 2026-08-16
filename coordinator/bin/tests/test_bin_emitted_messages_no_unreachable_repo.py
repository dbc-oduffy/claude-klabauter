"""coordinator/bin/tests/test_bin_emitted_messages_no_unreachable_repo.py --
the standalone ratchet for C3 of `docs/plans/2026-08-14-shipped-bin-messages-
stop-pointing-at-an-unreachable-repo.md`. A `coordinator/bin` script that
still prints a `DoE-claude` prose pointer to an OSS operator (who cannot
reach that private repo) is exactly the defect this plan's C1/C2 fixed 49
sites of -- this file is what would have caught all 49, and is what keeps
them fixed.

Spec backlink: docs/plans/2026-08-14-shipped-bin-messages-stop-pointing-at-
an-unreachable-repo.md, chunk C3.

Deliberately NOT an extension of `PackageCoverage` / `guard_message_corpus`
/ `guard_message_register_lint` (see the plan's Anti-scope: those pin the
covered package set to exactly three, and `guard_message_register_lint.py`'s
own docstring declares `coordinator/bin` out of scope). This module is a
standalone static lint over a fourth tree that mechanism never reaches.

Negative spec: this module never copies `PINNED_UNREACHABLE_TOKENS` values
into a local list -- it imports the live set (and `exemption_spans`) from
`coordinator_core.message_register._codename_classes` so the lint tracks the
pin automatically as it changes upstream. A hardcoded token list here would
silently stop tracking a token added/removed there.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from coordinator_core.message_register._codename_classes import (
    PINNED_UNREACHABLE_TOKENS,
    exemption_spans,
)

BIN_ROOT = Path(__file__).resolve().parents[1]

#: Files deliberately excluded from this lint's sweep, each annotated with
#: why -- mirrors the plan's own § Out of scope list. Only *.py files are
#: ever walked (see `_iter_bin_python_files`), so the one *.cmd entry never
#: needs to match a walked path; it is listed anyway for parity with the
#: plan's own out-of-scope enumeration.
ALLOWLIST: dict[str, str] = {
    "verify-dist-publish-repo-sync.py": "does not ship to the OSS mirror -- no OSS operator reaches it",
    "verify-publish-targets-portable-sync.py": "does not ship to the OSS mirror -- no OSS operator reaches it",
    "install-doe-claude-precommit-hook.cmd": (
        "does not ship to the OSS mirror -- listed for parity with the plan's "
        "§ Out of scope, never matched by the *.py walk"
    ),
    "install-doe-claude-precommit-hook.py": (
        "does not ship to the OSS mirror, AND its naming is separately sanctioned: the "
        "file's own docstring records that `doe_claude` is an explicitly KEPT slug in "
        "coordinator_core.ops.check_registry_codename_leak's KEEPSET, because the OSS "
        "clone resolver reads `repos.doe_claude`. Its filename, module path, and "
        "identifiers all carry the slug regardless, so a paraphrase in the prose would "
        "conceal nothing. Added 2026-08-14 after the C3 lint surfaced it -- it was in "
        "neither the plan's scope: nor its § Out of scope, a real gap in the census"
    ),
}

# ---------------------------------------------------------------------------
# Extraction -- resolves emitted-string content statically, never renaming or
# re-deriving the token domain. An expression whose emitted text this
# resolver cannot determine at all contributes no text to check (there is
# nothing static to check), but a __doc__-derived expression whose SHAPE is
# ambiguous defaults to treating the WHOLE docstring as emitted (the
# more-conservative, fail-loud reading) rather than assuming only a safe
# first line.
#
# Review: coordinator:code-reviewer -- what the resolver sees, precisely, as
# of this fix: string/f-string literals; `+` and `%` concatenation (for `%`,
# only the static left operand -- the substitution values on the right are
# a live interpolation, out of scope by design); ternary (`IfExp`) branches;
# `.strip()`/`.format()`/etc. passthrough calls; argparse
# description/epilog/help kwargs; `__doc__` (whole or first-line-split); and
# a Name reference to a var recorded via plain `Assign` or `+=` (`AugAssign`
# with `Add`) anywhere in the same function/module scope, including inside
# `if`/`elif`/`else`, `try`/`except`/`else`/`finally`, `for`, `while`, and
# `with` bodies (but never inside a nested `def`/`class`, tracked as its own
# separate scope).
#
# Residual (deliberate) blind spots: any RHS the resolver cannot resolve
# statically at all (e.g. a function call's return value, `%`'s dynamic
# substitution values, a subscript/attribute lookup, string `*` repetition)
# contributes no text and is silently skipped rather than guessed at; a
# var assigned via any binding shape besides single-target `Assign`/`Add`
# `AugAssign` (e.g. tuple-unpacking assignment, `:=`, `for`-loop targets) is
# never recorded; and a var reassigned across sibling branches (`if`/`else`,
# `try`/`except`, etc.) has EVERY branch's text unioned into the same name
# (mirroring `IfExp`), never picking only the branch that would actually
# execute at runtime -- this module does no control-flow analysis, so it
# cannot narrow to one branch, and narrowing incorrectly (by visit order)
# would silently DROP a real, statically-present violation rather than
# merely misattribute it. The union can therefore also flag a name whose
# violating text sits in a branch that never runs alongside the emission --
# a false positive this module accepts deliberately, in the same
# fail-loud direction as every other choice in this list.
#
# `_embedded_in_larger_identifier` (see its docstring) also has one
# deliberate, named residual: `_IDENT_CHARS` does not include `-`, so a
# hyphen-joined identifier compound (e.g. `install-doe-claude-precommit-
# hook`) is NOT recognized as "embedded in a larger identifier" and WILL
# fire as a violation. Today this is masked only because the one in-tree
# file that would trigger it, `install-doe-claude-precommit-hook.py`, is
# excluded wholesale via `ALLOWLIST` rather than covered by this mechanism.
# A future `coordinator/bin` script emitting a hyphen-joined identifier
# containing a pinned token will need its own new `ALLOWLIST` (or
# `_EXEMPTION_PATTERNS`-shaped) entry -- it will NOT be caught generally by
# `_embedded_in_larger_identifier`. Widening `_IDENT_CHARS` to include `-`
# is deliberately REJECTED as the fix: hyphen is also a common flanking
# character in ordinary prose (e.g. "the DoE-claude-side leg"), so treating
# `-` as an identifier character would suppress genuine prose hits too --
# trading a narrow, allowlist-covered false positive for a false-negative
# class in exactly the prose this lint exists to catch. The wrong direction
# for a ratchet.
# ---------------------------------------------------------------------------

_EMISSION_LOGGING_ATTRS = {"debug", "info", "warning", "warn", "error", "critical", "exception"}
_PASSTHROUGH_STR_METHODS = {"strip", "rstrip", "lstrip", "format", "upper", "lower", "title"}


def _module_docstring(tree: ast.Module) -> str | None:
    if not tree.body:
        return None
    first = tree.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value.value
    return None


def _contains_doc_ref(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__doc__" for n in ast.walk(node))


def _is_doc_first_line_shape(node: ast.AST) -> bool:
    """True iff `node` is exactly `__doc__.splitlines()[0]` -- the one shape
    the plan names as emitting ONLY the docstring's first line. Every other
    shape touching `__doc__` (`.strip()`, bare `__doc__`, `[:100]`, ...)
    falls through to the whole-docstring default in `_classify_doc_expr`."""
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "splitlines"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "__doc__"
    ):
        return False
    idx = node.slice
    return isinstance(idx, ast.Constant) and idx.value == 0


def _classify_doc_expr(node: ast.AST) -> str | None:
    """"whole" | "first_line" | None (node does not touch `__doc__` at all)."""
    if not _contains_doc_ref(node):
        return None
    if _is_doc_first_line_shape(node):
        return "first_line"
    return "whole"


@dataclass
class _Scope:
    doc_whole: str | None
    doc_first_line: str | None
    vars: dict[str, list[str]]


def _resolve_fragments(node: ast.AST | None, scope: _Scope) -> list[str]:
    """Every statically-known literal text fragment `node` can emit, or an
    empty list if `node`'s value is fully dynamic (a runtime value with no
    literal source contribution -- there is nothing static to check, which
    is not the same as "could not determine if this is emitted": we know
    it's a live interpolation, exactly the class the upstream
    `_codename_classes._live_interpolation_spans` mechanism already treats
    as out of the OSS-scrub domain for the same reason)."""
    if node is None:
        return []

    # Review: coordinator:code-reviewer -- the __doc__ classification below
    # must NOT run on a JoinedStr node itself: `_contains_doc_ref` walks the
    # whole node, so a bare f-string containing a `__doc__` reference (e.g.
    # `f"prefix {__doc__}"`) would short-circuit here and discard the
    # f-string's own static literal segments (`"prefix "`), which are
    # emitted regardless of what `__doc__` resolves to. Deferring to the
    # JoinedStr branch below lets each fragment (including a FormattedValue
    # that is exactly `__doc__`) get classified individually instead.
    if not isinstance(node, ast.JoinedStr):
        doc_class = _classify_doc_expr(node)
        if doc_class == "whole":
            return [scope.doc_whole] if scope.doc_whole is not None else []
        if doc_class == "first_line":
            return [scope.doc_first_line] if scope.doc_first_line is not None else []

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]

    if isinstance(node, ast.JoinedStr):
        frags: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                frags.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                frags.extend(_resolve_fragments(value.value, scope))
        return frags

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _resolve_fragments(node.left, scope) + _resolve_fragments(node.right, scope)

    # Review: coordinator:code-reviewer -- "...%s" % (x,) still has a fully
    # static left operand even though the substitution values are dynamic;
    # only the right side (the args tuple/dict) is the live interpolation.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _resolve_fragments(node.left, scope)

    if isinstance(node, ast.IfExp):
        return _resolve_fragments(node.body, scope) + _resolve_fragments(node.orelse, scope)

    if isinstance(node, ast.Name):
        return scope.vars.get(node.id, [])

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _PASSTHROUGH_STR_METHODS:
            return _resolve_fragments(node.func.value, scope)
        return []

    return []


def _record_simple_assign(stmt: ast.stmt, scope: _Scope) -> None:
    # Review: coordinator:code-reviewer -- `msg += "..."` is a fully static
    # concatenation when its RHS is static, but was previously invisible
    # since only ast.Assign was matched.
    if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
        target = stmt.target
        if isinstance(target, ast.Name):
            existing = scope.vars.get(target.id, [])
            scope.vars[target.id] = existing + _resolve_fragments(stmt.value, scope)
        return
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return
    target = stmt.targets[0]
    if not isinstance(target, ast.Name):
        return
    # Review: coordinator:code-reviewer -- union with any prior recording for
    # this name rather than overwrite. This resolver has no control-flow
    # analysis (see module docstring), so it cannot know which of two
    # sibling-branch assignments to the same name would actually execute --
    # overwriting silently drops whichever branch is visited first, which
    # can drop a real, statically-known violation entirely (not just
    # misattribute it), exactly like the IfExp branch below already avoids
    # by unioning body+orelse. "Any branch could execute, so any branch's
    # text counts" applies to this walk too.
    existing = scope.vars.get(target.id, [])
    scope.vars[target.id] = existing + _resolve_fragments(stmt.value, scope)


#: Statement types whose nested body/handler statement lists are walked for
#: simple-assignment tracking, mapped to the attribute names holding those
#: lists. `ast.Try`/`ast.TryStar` are handled separately below since their
#: `handlers` are `ast.excepthandler` nodes, not statement lists directly.
_CONTROL_FLOW_BODY_FIELDS: dict[type, tuple[str, ...]] = {
    ast.If: ("body", "orelse"),
    ast.For: ("body", "orelse"),
    ast.AsyncFor: ("body", "orelse"),
    ast.While: ("body", "orelse"),
    ast.With: ("body",),
    ast.AsyncWith: ("body",),
}


def _record_simple_assigns(stmts: list[ast.stmt], scope: _Scope) -> None:
    """Record simple assignments (`_record_simple_assign`) from `stmts`,
    recursing into `if`/`try`/`for`/`while`/`with` bodies -- a var assigned
    inside a branch is otherwise invisible to a later same-scope reference
    -- but never into a nested `def`/`class`, which is tracked as its own
    separate scope by `extract_emitted_strings`."""
    for stmt in stmts:
        _record_simple_assign(stmt, scope)
        if isinstance(stmt, ast.Try) or (hasattr(ast, "TryStar") and isinstance(stmt, ast.TryStar)):
            for field in ("body", "orelse", "finalbody"):
                _record_simple_assigns(getattr(stmt, field, []), scope)
            for handler in getattr(stmt, "handlers", []):
                _record_simple_assigns(handler.body, scope)
            continue
        fields = _CONTROL_FLOW_BODY_FIELDS.get(type(stmt))
        if fields:
            for field in fields:
                _record_simple_assigns(getattr(stmt, field), scope)


def _emission_kind(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "print":
        return "print"
    if isinstance(func, ast.Attribute):
        if (
            func.attr == "write"
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
            and func.value.attr in ("stdout", "stderr")
        ):
            return "write"
        if func.attr in _EMISSION_LOGGING_ATTRS:
            return "logging"
    return None


@dataclass
class Hit:
    lineno: int
    text: str
    origin: str


def _walk_emissions(stmts: list[ast.stmt], scope: _Scope) -> list[Hit]:
    """Depth-first over every node reachable from `stmts` (not just the
    top-level statement list) so a call nested inside an `if`/`for`/`try`
    body is still seen. Simple-assignment tracking (`_record_simple_assigns`)
    recurses into `if`/`try`/`for`/`while`/`with` bodies too (never into a
    nested `def`/`class`, a separate scope) -- assignments are recorded for
    the whole `stmts` tree up front, then emissions are resolved against the
    fully-populated `scope.vars`, so a var referenced before its (branch-
    nested) assignment in source order still resolves; genuine control-flow-
    aware data flow (e.g. tracking which branch actually ran) stays out of
    scope for a static lint of this size."""
    hits: list[Hit] = []
    _record_simple_assigns(stmts, scope)
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                kind = _emission_kind(node)
                if kind is not None:
                    for arg in node.args:
                        for text in _resolve_fragments(arg, scope):
                            hits.append(Hit(node.lineno, text, kind))
                for kw in node.keywords:
                    if kw.arg in {"description", "epilog", "help"}:
                        for text in _resolve_fragments(kw.value, scope):
                            hits.append(Hit(node.lineno, text, f"kwarg:{kw.arg}"))
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                for arg in node.exc.args:
                    for text in _resolve_fragments(arg, scope):
                        hits.append(Hit(node.lineno, text, "raise"))
                for kw in node.exc.keywords:
                    for text in _resolve_fragments(kw.value, scope):
                        hits.append(Hit(node.lineno, text, "raise-kwarg"))
    return hits


def extract_emitted_strings(source: str) -> list[Hit]:
    """Every emitted-text fragment `source` (a `coordinator/bin/*.py`
    module's text) statically resolves to -- see module docstring for what
    counts as "emitted" and the resolver's documented blind spots."""
    tree = ast.parse(source)
    doc_whole = _module_docstring(tree)
    doc_first_line = doc_whole.splitlines()[0] if doc_whole else None
    module_scope = _Scope(doc_whole=doc_whole, doc_first_line=doc_first_line, vars={})

    hits = _walk_emissions(tree.body, module_scope)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_scope = _Scope(
                doc_whole=doc_whole, doc_first_line=doc_first_line, vars=dict(module_scope.vars)
            )
            hits.extend(_walk_emissions(node.body, func_scope))

    return hits


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    path: Path
    lineno: int
    token: str
    text: str


def _span_covered(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)


_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _embedded_in_larger_identifier(text: str, start: int, end: int) -> bool:
    """True when the `[start, end)` hit is a fragment of a LONGER identifier
    run (`real_doe_claude_lessons_dir_untouched_guard`, `_doe_claude_probe`)
    rather than a standalone prose mention.

    Such a hit is a functional identifier -- the same class
    `_EXEMPTION_PATTERNS` already exempts by shape (`repos.<key>`,
    `REPO_DOE_CLAUDE`, `<name>-em`) -- and B7's own rules are word-anchored
    for exactly this reason. `find_token_violations` searches by bare
    substring, so without this guard the sweep fires on any snake_case
    identifier that happens to CONTAIN a pinned token, which is not a
    pointer an OSS reader could mistake for something navigable. Observed
    2026-08-14 as a false positive against
    `tests/test_harvest_doe_root_machine_local_leg.py`, whose emitted guard
    NAME embeds `doe_claude` while its only real repo reference on the same
    line (`repos.doe_claude`) was already correctly exempt."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before in _IDENT_CHARS or after in _IDENT_CHARS


def find_token_violations(text: str) -> list[str]:
    """Every `PINNED_UNREACHABLE_TOKENS` member present in `text` outside a
    span `exemption_spans(text)` covers, and not embedded in a longer
    identifier. Consumes the token set and `exemption_spans` live from
    `_codename_classes` -- see module docstring."""
    covered = exemption_spans(text)
    found: list[str] = []
    for token in PINNED_UNREACHABLE_TOKENS:
        start = text.find(token)
        while start != -1:
            end = start + len(token)
            if not _span_covered(covered, start, end) and not _embedded_in_larger_identifier(
                text, start, end
            ):
                found.append(token)
            start = text.find(token, start + 1)
    return found


def _iter_bin_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.name not in ALLOWLIST)


def collect_violations(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in _iter_bin_python_files(root):
        source = path.read_text(encoding="utf-8")
        for hit in extract_emitted_strings(source):
            for token in find_token_violations(hit.text):
                violations.append(Violation(path, hit.lineno, token, hit.text))
    return violations


# ---------------------------------------------------------------------------
# AC1 -- the live sweep over coordinator/bin
# ---------------------------------------------------------------------------


def test_no_bin_script_emits_an_unreachable_repo_pointer():
    violations = collect_violations(BIN_ROOT)
    assert violations == [], "\n".join(
        f"{v.path.relative_to(BIN_ROOT.parent.parent)}:{v.lineno} emits {v.token!r} in: {v.text!r}"
        for v in violations
    )


def test_allowlist_matches_plan_out_of_scope_list():
    assert set(ALLOWLIST) == {
        "install-doe-claude-precommit-hook.py",
        "verify-dist-publish-repo-sync.py",
        "verify-publish-targets-portable-sync.py",
        "install-doe-claude-precommit-hook.cmd",
    }


def test_sweep_actually_covers_scripts_beyond_the_allowlist():
    covered = _iter_bin_python_files(BIN_ROOT)
    assert len(covered) > 100, "the walk should reach the great majority of coordinator/bin/*.py"


# ---------------------------------------------------------------------------
# AC5 -- self-check: a deliberately-added probe fails, removing it passes.
# Both legs run against a throwaway tmp_path tree, never against a script
# two peer agents are concurrently editing.
# ---------------------------------------------------------------------------


def test_probe_string_fails_then_passes(tmp_path):
    dirty = tmp_path / "probe-script.py"
    dirty.write_text(
        textwrap.dedent(
            '''
            """A throwaway probe module, not a real coordinator/bin script."""
            import sys


            def main() -> None:
                sys.stderr.write("cannot resolve the DoE-claude repo root (boom)")
            '''
        ),
        encoding="utf-8",
    )
    dirty_violations = collect_violations(tmp_path)
    assert any(v.token == "DoE-claude" for v in dirty_violations), (
        "probe with an emitted DoE-claude prose string should fail the lint"
    )

    clean = tmp_path / "probe-script.py"
    clean.write_text(
        textwrap.dedent(
            '''
            """A throwaway probe module, not a real coordinator/bin script."""
            import sys


            def main() -> None:
                sys.stderr.write("cannot resolve the coordinator doctrine repo root (boom)")
            '''
        ),
        encoding="utf-8",
    )
    clean_violations = collect_violations(tmp_path)
    assert clean_violations == [], "removing the probe token should make the lint pass"


# ---------------------------------------------------------------------------
# Extraction-behavior unit tests -- exercise the resolver's documented rules
# directly, independent of what today's coordinator/bin corpus happens to
# contain.
# ---------------------------------------------------------------------------


def test_extractor_sees_print_of_whole_docstring():
    source = textwrap.dedent(
        '''
        """Line one names DoE-claude.
        Line two is harmless."""
        print(__doc__)
        '''
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_extractor_respects_first_line_only_split():
    source = textwrap.dedent(
        '''
        """Harmless first line.
        Second line names DoE-claude."""
        print(__doc__.splitlines()[0])
        '''
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" not in joined


def test_extractor_treats_ambiguous_doc_usage_as_whole_emission():
    source = textwrap.dedent(
        '''
        """Harmless first line.
        Second line names DoE-claude."""
        print(__doc__.strip())
        '''
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_extractor_sees_argparse_description_epilog_help():
    source = textwrap.dedent(
        """
        import argparse

        parser = argparse.ArgumentParser(
            description="see the DoE-claude repo",
            epilog="also DoE-claude",
        )
        parser.add_argument("--x", help="points at DoE-claude too")
        """
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert joined.count("DoE-claude") == 3


def test_extractor_sees_fstring_static_fragment():
    source = textwrap.dedent(
        """
        exc = "boom"
        print(f"cannot resolve the DoE-claude repo root ({exc})")
        """
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_extractor_sees_variable_indirection():
    source = textwrap.dedent(
        """
        _USAGE_TEXT = "usage: see the DoE-claude repo"


        def _print_help():
            print(_USAGE_TEXT)
        """
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_extractor_unions_branch_reassignment_of_same_name():
    # Review: coordinator:code-reviewer -- regression for the P1 overwrite
    # bug: a name assigned in both arms of an if/else must resolve to BOTH
    # branches' text, not just whichever branch is visited last.
    source = textwrap.dedent(
        """
        def f(cond):
            if cond:
                msg = "cannot resolve the DoE-claude repo root"
            else:
                msg = "clean text"
            print(msg)
        """
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_extractor_sees_fstring_literal_alongside_doc_reference():
    # Review: coordinator:code-reviewer -- regression for the P2a short-
    # circuit bug: an f-string combining a __doc__ reference with its own
    # static literal segment must resolve both, not just the docstring.
    source = textwrap.dedent(
        '''
        """Harmless docstring, no pinned token here."""
        print(f"see the DoE-claude repo: {__doc__}")
        '''
    )
    hits = extract_emitted_strings(source)
    joined = "\n".join(h.text for h in hits)
    assert "DoE-claude" in joined


def test_functional_identifier_exemptions_do_not_false_positive():
    source = textwrap.dedent(
        """
        print("set repos.doe_claude to fix this")
        print("REPO_DOE_CLAUDE is unset")
        print("route the memo to doe-claude-em")
        """
    )
    violations = []
    for hit in extract_emitted_strings(source):
        violations.extend(find_token_violations(hit.text))
    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
