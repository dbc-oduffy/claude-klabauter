"""Structural pin: `GuardEntry` (H1's frozen dataclass, `dispatch.py`) must be
constructed ONLY inside `dispatch._build_guard_chain` (H9, 2026-07-30 --
os-aware-guard-advisory-defaults row H9).

A hand-maintained consumer list (H2 in the same plan) is the wrong artifact
for discharging "this mutates a shared symbol, so enumerate every consumer" --
a scan can always under-count, as it already did once for the confinement set
(`test_confinement_attack_corpus.py:270-273`) and as H2 itself did before its
own review. What actually discharges the rule is a test that FAILS the moment
a `GuardEntry`/chain-entry gets built anywhere else: H1's frozen dataclass
carries a 5th field (`advisory_value`) with a default, so a stray positional
construction elsewhere silently regresses to `AdvisoryValue.UNCLASSIFIED`
instead of an author-chosen classification -- exactly the failure mode H5
(the registry-validation test) exists to catch on the REGISTERED chain, but
only if the construction happened where the registry can see it. A
`GuardEntry` built outside `_build_guard_chain` and never appended to
`guard_chain` would never reach H5's own registry scan at all.

This is an AST-based scan, not a regex -- several real registrations in
`_build_guard_chain` (e.g. `block-approval-sentinel-creation`) span multiple
lines, which a line-oriented regex mis-handles (partial match, or a match
that silently stops at the first line break). Walking the parsed module tree
finds every `GuardEntry(...)` call site regardless of formatting and reports
its true enclosing function by construction, not by textual proximity.

Scope: every `.py` file anywhere under `coordinator_core/bash_guards/`
(excluding this package's own `tests/` subtree and any `__pycache__`
subtree) -- not merely `dispatch.py`, and not merely its top-level files. A
hand-scoped "just check dispatch.py" version would be exactly the kind of
hand-maintained, silently-under-counting artifact this row exists to
replace: a future guard module that imports `GuardEntry` and builds its own
entry (bypassing `_build_guard_chain` entirely) -- whether that module sits
at the package root or in a subdirectory -- would be invisible to a
narrower scan.

Import-alias resolution: a call site counts as a `GuardEntry` construction
if its bare name resolves -- via this module's own `Import`/`ImportFrom`
bindings -- to `dispatch.GuardEntry`, not merely if it is spelled
"GuardEntry" at the call site. `from ...dispatch import GuardEntry as GE`
followed by `GE(...)` is exactly as much a construction as the unaliased
form, and matching on literal spelling alone would miss it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, NamedTuple

from coordinator_core.bash_guards import dispatch

_PACKAGE_DIR = Path(dispatch.__file__).resolve().parent
_SANCTIONED_FILE = Path(dispatch.__file__).resolve()
_SANCTIONED_FUNCTION = "_build_guard_chain"


class _CallSite(NamedTuple):
    path: Path
    lineno: int
    enclosing_function: str


def _iter_package_py_files() -> List[Path]:
    """Every `.py` file anywhere under `bash_guards/`, excluding the
    `tests/` and `__pycache__` subtrees -- mirrors the scope a new guard
    module would land in, including one organized under a subdirectory
    rather than sitting directly in the package root."""
    files = []
    for path in _PACKAGE_DIR.rglob("*.py"):
        rel_parts = path.relative_to(_PACKAGE_DIR).parts
        if rel_parts[0] == "tests" or "__pycache__" in rel_parts:
            continue
        files.append(path)
    return files


def _guard_entry_local_names(tree: ast.Module) -> set:
    """Every local name that resolves to `dispatch.GuardEntry` in this
    module: the bare `GuardEntry` spelling (covers `dispatch.py`'s own
    definition, and any `from ...dispatch import GuardEntry` with no
    rename), plus any `asname` bound by `from ...dispatch import
    GuardEntry as <alias>`. Resolving the actual import binding -- rather
    than matching call-site text against the literal string "GuardEntry"
    -- is what stops `from ...dispatch import GuardEntry as GE` followed
    by `GE(...)` from constructing a real entry invisibly."""
    names = {"GuardEntry"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "dispatch":
            for alias in node.names:
                if alias.name == "GuardEntry":
                    names.add(alias.asname or alias.name)
    return names


def _guard_entry_call_sites_in_file(path: Path) -> List[_CallSite]:
    """Walk `path`'s AST and return one `_CallSite` per `GuardEntry(...)`
    call, tagged with the nearest enclosing named function (or `<module>`
    if the call sits at module top level). Handles the bare name (resolved
    through any import alias via `_guard_entry_local_names`, so a renamed
    import doesn't evade the scan), and `something.GuardEntry(...)` (a
    hypothetical qualified import from another module) -- either shape is
    "constructing a GuardEntry", and this test does not care which import
    style a future consumer picks."""
    tree = ast.parse(path.read_text(), filename=str(path))
    local_names = _guard_entry_local_names(tree)
    sites: List[_CallSite] = []
    function_stack: List[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            function_stack.append(node.name)
            self.generic_visit(node)
            function_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            function_stack.append(node.name)
            self.generic_visit(node)
            function_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            is_guard_entry_call = (isinstance(func, ast.Name) and func.id in local_names) or (
                isinstance(func, ast.Attribute) and func.attr == "GuardEntry"
            )
            if is_guard_entry_call:
                enclosing = function_stack[-1] if function_stack else "<module>"
                sites.append(_CallSite(path=path, lineno=node.lineno, enclosing_function=enclosing))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def _all_guard_entry_call_sites() -> List[_CallSite]:
    sites: List[_CallSite] = []
    for path in _iter_package_py_files():
        sites.extend(_guard_entry_call_sites_in_file(path))
    return sites


def test_guard_entry_call_sites_are_discoverable():
    """Guard the guard: if the AST scan stops finding the real, known-good
    `_build_guard_chain` registrations, every assertion below would pass
    vacuously against an empty result."""
    sites = _all_guard_entry_call_sites()
    assert len(sites) > 10, (
        "expected >10 GuardEntry(...) call sites (one per registered guard) "
        "inside _build_guard_chain, found %d -- the AST scan is broken, not "
        "the chain" % len(sites)
    )
    assert all(
        site.path == _SANCTIONED_FILE and site.enclosing_function == _SANCTIONED_FUNCTION for site in sites
    ), "sanity check failed against the CURRENT tree -- see test_guard_entry_is_constructed_only_inside_build_guard_chain for the real assertion"


def test_guard_entry_is_constructed_only_inside_build_guard_chain():
    """The load-bearing assertion: every `GuardEntry(...)` call site, in
    every non-test `.py` file under `bash_guards/`, must be textually inside
    `dispatch._build_guard_chain`. A construction anywhere else -- a helper
    function, module top level, or a different guard module entirely --
    bypasses H3's "every entry classified, INCLUDING every CONFINEMENT_DENY
    entry" discipline and can silently default `advisory_value` to
    `AdvisoryValue.UNCLASSIFIED` (H1's dataclass default) without ever
    being caught by H5's registry-validation test, which only inspects
    entries that made it into the registered `guard_chain` list.
    """
    offenders = [
        site
        for site in _all_guard_entry_call_sites()
        if not (site.path == _SANCTIONED_FILE and site.enclosing_function == _SANCTIONED_FUNCTION)
    ]
    assert not offenders, (
        "GuardEntry constructed outside dispatch._build_guard_chain at: %s -- "
        "construct every guard registration inside _build_guard_chain itself, "
        "not in a helper, module top level, or another guard module, and set "
        "advisory_value explicitly on the entry rather than relying on "
        "GuardEntry's own AdvisoryValue.UNCLASSIFIED default."
        % ", ".join("%s:%d (in %s)" % (site.path.name, site.lineno, site.enclosing_function) for site in offenders)
    )
