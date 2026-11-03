"""Static call-signature binding over parsed ASTs: scope-aware import resolution,
call-target resolution and `inspect.Signature.bind` semantics over `ast.arguments`.

Shared by the payload parity gate (`percolate.payload_parity`) and the commit-time
call-signature leak detector. Imports only stdlib and `coordinator_core.pyimports`.
"""

from __future__ import annotations

import ast
from typing import Iterable, Optional

from coordinator_core.pyimports import resolve_relative_module

__all__ = [
    "top_level_functions",
    "FileImportMap",
    "resolve_call_target",
    "bind_check",
    "calls_asserting_typeerror",
]


def top_level_functions(tree: "ast.Module") -> "dict[str, ast.FunctionDef]":
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class _ImportScope:
    """One lexical scope's own `bound name -> resolution` bindings, chained
    to its enclosing scope (§ F1 fix). A name not found locally falls
    through to `parent` -- exactly Python's own name-resolution order, and
    the reason a function-local import can no longer clobber an unrelated
    call site elsewhere in the file: it only ever populates ITS OWN scope,
    never the module scope other call sites resolve against."""

    __slots__ = ("parent", "bindings")

    def __init__(self, parent: "Optional[_ImportScope]") -> None:
        self.parent = parent
        self.bindings: "dict[str, tuple]" = {}

    def resolve(self, name: str) -> "Optional[tuple]":
        scope: "Optional[_ImportScope]" = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None


class FileImportMap:
    """`bound name -> resolution`, SCOPE-AWARE (§ F1): a module-level
    `import`/`from ... import` binds in the module scope, visible to every
    call site in the file; a local import nested inside a function body
    binds ONLY in that function's own scope (and its nested functions',
    per normal lexical chaining) -- it can no longer silently overwrite an
    unrelated call site's resolution elsewhere in the file merely because
    both happen to reuse the same bound name (the F1 defect: a flat,
    single-dict, last-write-wins map conflated these).

    Resolution shapes, keyed by the FIRST element of the stored tuple:
      ("module", dotted) -- `import X[.Y] [as N]`: `N` (or `X`, unaliased)
          names the module `dotted`; `N.attr(...)` resolves to `dotted.attr`.
      ("maybe", full, base_module, attr_name) -- `from X import Y [as N]`:
          `N` (or `Y`) MAY be a function (`X.Y`, `full`) or a re-exported
          submodule (`X.Y` as a package) -- both a bare call `N(...)` and a
          further-qualified `N.attr(...)` are handled by
          `resolve_call_target`, which tries the function reading first.
    """

    def __init__(self, tree: "ast.Module", current_module: "Optional[str]", current_is_package: bool) -> None:
        self._current_module = current_module
        self._current_is_package = current_is_package
        self._module_scope = _ImportScope(parent=None)
        self._call_scope: "dict[int, _ImportScope]" = {}
        self._calls: "list[tuple[int, int, ast.Call]]" = []
        self.withs: "list[ast.AST]" = []
        self._walk(tree, self._module_scope, 0)

    @property
    def calls(self) -> "list[ast.Call]":
        """Every ``ast.Call`` in ``ast.walk`` (breadth-first) order: depth
        first, then pre-order position, which is the order a level is visited."""
        return [call for _depth, _seq, call in sorted(self._calls, key=lambda t: (t[0], t[1]))]

    def _bind_import_node(self, node: "ast.stmt", scope: "_ImportScope") -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                if alias.asname:
                    scope.bindings[alias.asname] = ("module", dotted)
                else:
                    top = dotted.split(".")[0]
                    scope.bindings[top] = ("module", top)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module
            if level:
                base = resolve_relative_module(
                    module or "", level, self._current_module, self._current_is_package
                )
            else:
                base = module
            if base is None:
                return
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                scope.bindings[bound] = ("maybe", f"{base}.{alias.name}", base, alias.name)

    def _walk(self, node: "ast.AST", scope: "_ImportScope", depth: int) -> None:
        ast_node = ast.AST
        for field_name in node._fields:
            value = getattr(node, field_name, None)
            if isinstance(value, list):
                children = [c for c in value if isinstance(c, ast_node)]
            elif isinstance(value, ast_node):
                children = (value,)
            else:
                continue
            for child in children:
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    self._bind_import_node(child, scope)
                elif isinstance(child, ast.Call):
                    self._call_scope[id(child)] = scope
                    self._calls.append((depth + 1, len(self._calls), child))
                    self._walk(child, scope, depth + 1)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # A new function body is its OWN scope, chained to the
                    # scope it's lexically nested in -- a local import inside
                    # this function is invisible outside it, and inside a
                    # further-nested function it's visible via the chain, same
                    # as real Python closures.
                    self._walk(child, _ImportScope(parent=scope), depth + 1)
                else:
                    if isinstance(child, (ast.With, ast.AsyncWith)):
                        self.withs.append(child)
                    self._walk(child, scope, depth + 1)

    def resolve_for_call(self, call: "ast.Call", name: str) -> "Optional[tuple]":
        scope = self._call_scope.get(id(call), self._module_scope)
        return scope.resolve(name)


def _attribute_chain(node: "ast.expr") -> "Optional[list[str]]":
    """`a.b.c` -> `["a", "b", "c"]`; `None` for any base that is not a bare
    `Name` (a chained call, a subscript, ...) -- those are DYNAMIC shapes
    handled by `resolve_call_target`'s own caller before this is reached."""
    parts: "list[str]" = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def resolve_call_target(call: "ast.Call", import_map: "FileImportMap") -> "Optional[tuple[str, bool]]":
    """`(dotted_or_hint, is_dynamic)` for `call`'s callee expression, or
    `None` when it is not a shape this checker recognises as a possible
    first-party call at all (out of scope, never an abstention -- § module
    docstring negative-spec).

    `is_dynamic=True` (§ AC5) for a call whose func is itself a `Call`
    (`getattr(obj, name)(...)`) or a `Subscript` (`handlers[name](...)`) --
    plausibly first-party, statically unresolvable.
    """
    func = call.func
    if isinstance(func, (ast.Call, ast.Subscript)):
        return ("<dynamic call target>", True)

    chain = _attribute_chain(func)
    if chain is None:
        return None
    base, *attrs = chain
    entry = import_map.resolve_for_call(call, base)
    if entry is None:
        return None

    kind = entry[0]
    if kind == "module":
        dotted_base = entry[1]
        if not attrs:
            return None  # calling the module object itself -- not a function call
        return (".".join([dotted_base, *attrs]), False)

    if kind == "maybe":
        _, full, _base_mod, _name = entry
        if not attrs:
            return (full, False)
        return (".".join([full, *attrs]), False)

    return None


def bind_check(call: "ast.Call", sig: "ast.arguments") -> "list[str]":
    """Every way `call`'s (non-splat, § caller's own splat pre-check)
    positional/keyword arguments fail to bind against `sig` -- an unexpected
    keyword, a missing required parameter, or a positional arity overflow.
    Empty list means the call binds cleanly."""
    posonly = [p.arg for p in sig.posonlyargs]
    pos_or_kw = [p.arg for p in sig.args]
    all_positional = posonly + pos_or_kw
    kwonly = [p.arg for p in sig.kwonlyargs]
    has_vararg = sig.vararg is not None
    has_kwarg = sig.kwarg is not None

    num_defaults = len(sig.defaults)
    required_positional = set(all_positional[: len(all_positional) - num_defaults]) if num_defaults <= len(all_positional) else set()
    kwonly_required = {p.arg for p, d in zip(sig.kwonlyargs, sig.kw_defaults) if d is None}

    violations: "list[str]" = []

    n_pos = len(call.args)
    if n_pos > len(all_positional) and not has_vararg:
        violations.append(
            f"too many positional arguments ({n_pos} given, {len(all_positional)} accepted)"
        )

    filled = set(all_positional[: min(n_pos, len(all_positional))])

    for kw in call.keywords:
        name = kw.arg
        if name is None:
            continue  # **kwargs splat -- caller already abstained on this
        if name in filled:
            violations.append(f"parameter {name!r} passed both positionally and by keyword")
            continue
        if name in posonly:
            # F3: CPython rejects a keyword argument naming a positional-
            # only parameter even when that name happens to be otherwise
            # unfilled -- `pos_or_kw`/`kwonly` may legitimately accept it,
            # `posonly` never may.
            if not has_kwarg:
                violations.append(f"positional-only argument {name!r} passed as keyword")
        elif name in pos_or_kw or name in kwonly:
            filled.add(name)
        elif not has_kwarg:
            violations.append(f"unexpected keyword argument {name!r}")

    missing = (required_positional | kwonly_required) - filled
    if missing:
        violations.append(f"missing required argument(s): {sorted(missing)!r}")

    return violations


def calls_asserting_typeerror(withs: "Iterable[ast.AST]") -> "set[int]":
    """Every ``ast.Call`` lexically inside a ``with pytest.raises(TypeError):``
    body of one of ``withs`` (a tree's ``With``/``AsyncWith`` nodes), keyed
    by ``id()`` (valid for the lifetime of this one parsed tree).

    A call written deliberately WRONG under such a context is the assertion
    itself -- the test exists to prove the signature rejects that shape. See
    ``coordinator_core/install/test_forwarder_trust_guard.py ::
    test_write_agent_forwarder_target_is_required_keyword_only``, whose whole
    point is that ``_write_agent_forwarder`` has no defaultable ``target``.
    Binding it produces a "missing required argument(s)" violation that is
    exactly backwards: the call site and the signature AGREE, and the test is
    what proves it.

    OUT OF SCOPE, not an abstention (§ the unresolvable-base precedent in this
    module's negative spec): the gate can bind this call perfectly well, so
    counting it against ``abstain_share`` would misreport how much a run
    proved. Narrow to ``TypeError`` on purpose -- that is the arity/signature
    idiom. A ``pytest.raises(ValueError)`` block wraps a well-formed call
    whose RUNTIME behaviour is under test, and stays fully checked.
    """
    marked: "set[int]" = set()
    for node in withs:
        if not any(_is_typeerror_raises(item.context_expr) for item in node.items):
            continue
        for body_stmt in node.body:
            for inner in ast.walk(body_stmt):
                if isinstance(inner, ast.Call):
                    marked.add(id(inner))
    return marked


def _is_typeerror_raises(expr: ast.expr) -> bool:
    """``pytest.raises(TypeError)`` / ``raises(TypeError)``, however imported.

    Matches on the trailing attribute/name ``raises`` rather than a fully
    resolved dotted path: this predicate only ever widens what is skipped
    when a first argument names ``TypeError``, and the alternative (routing
    it through `FileImportMap`) buys nothing a stricter match would catch.
    """
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if name != "raises":
        return False
    for arg in expr.args:
        if isinstance(arg, ast.Name) and arg.id == "TypeError":
            return True
        if isinstance(arg, ast.Tuple):
            if any(isinstance(e, ast.Name) and e.id == "TypeError" for e in arg.elts):
                return True
    return False
