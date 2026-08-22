"""
coordinator_core.cartography.op_edges — registry-dispatch producer/consumer
edge graph.

Purpose: ``coordinator_core.cartography.edges.build_edges`` self-describes,
in-band, that it categorically excludes ``register_op`` dynamic-dispatch
edges (its own "THE HYBRID BLIND SPOT" docstring section,
``excludes: ["register_op_dynamic_dispatch"]``). The 2026-08-06 architecture
survey found six of thirteen analysts independently paid that cost from six
different entry points, one naming ``p05-edges.json`` "close to useless" for
its chunk as a result. This module builds the ONE edge class that gap names:
walk ``@register_op`` decorator/call sites (the producer — an op module
registering a handler under a string op-name key), ``get_op_handler("…")``
call sites, and ``dispatch_message({"method": …})`` literal-dict call sites
(the two consumer shapes — a caller resolving or invoking a handler by that
same string key) over a caller-supplied file list, and emits the
producer(registering file) -> consumer(calling file) edges that
``coordinator_core.cartography.edges`` cannot see because none of the three
sites above is a static `call` AST node naming the other side directly —
the join only exists through the shared string literal.

Static-AST only, same posture as ``coordinator_core.cartography.edges``:
never executes target code, never imports/exec's a file, only
``ast.parse``/``ast.walk`` over already-read source text.

Producer/consumer matching is done by STRING EQUALITY of the op-name literal
recovered from each site — not by any cross-file symbol resolution. A
``register_op`` site and a ``get_op_handler``/``dispatch_message`` site are
joined into an edge iff they carry the identical literal op-name string, one
of ``ast.Constant`` at each end (see "Negative-spec" for what defeats this).

Known-unmodelled edge classes (named, not built — same 2026-08-06 survey,
scope-disciplined to the ONE declared class above):
  - Detached ``Popen`` process-spawn edges (a script spawning another process
    with no shared static or registry-mediated symbol at all).
  - Guard -> sink policy edges (a ``write_guards``/``bash_guards`` module's
    policy decision routing to an enforcement sink it never statically
    imports or calls).
  - ``CONSUMES_MANIFEST`` -> bin-script edges resolved BY NAME at apply
    time (a manifest string naming a script the apply-time resolver looks
    up, mirroring this exact registry-dispatch shape one level up the
    stack, in ``coordinator/bin/`` rather than ``coordinator_core``).
  These are real, named here so a future reader does not re-discover them
  from scratch, and deliberately out of scope for this module.

Negative-spec:
  - Resolves an ``ast.Name`` argument ONE hop, module scope only: a name
    bound at module scope directly to a string ``ast.Constant``
    (``OP_X = "x.y"``, plain or annotated), or a ``for`` loop target
    iterating a module-scope constant tuple/list/set of string literals
    (one edge per member). Does NOT resolve a call-bound name, an
    f-string, a concatenation, an imported name, a function parameter, or
    a function-local assignment — none of those is a module-scope literal
    binding, so each contributes NO site. This is the same
    static-AST-only posture ``cartography.edges`` already documents for
    its own import/call graph, not a defect unique to this module — see
    that module's docstring negative-spec.
  - Does NOT special-case ``dispatch_message``'s ONLY real caller shape in
    this repo today (a ``msg`` dict built earlier and passed by name) —
    zero live ``dispatch_message({"method": …})`` inline-literal call sites
    exist in this repo as of 2026-08-06 (grep-verified). The declared edge
    class is implemented exactly as specified; it legitimately yields zero
    ``dispatch`` edges against today's tree. A caller wanting the
    ``msg``-variable shape needs a different (out-of-scope) traversal.
  - Does NOT attempt cross-op de-duplication beyond literal string equality
    — two op names differing only by case or whitespace are distinct.
  - Does NOT walk files outside the caller-supplied ``files`` list — same
    target-resolution/containment model as every ``cartography.*`` sibling
    (``coordinator_core.cartography._guard.path_guard``).

Spec backlink: cross-repo memo, 2026-08-06 architecture survey (six of
thirteen analysts independently paid the ``register_op`` dynamic-dispatch
blind-spot cost cartography.edges's own docstring names).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Set

from coordinator_core.cartography._guard import path_guard

#: Registry-dispatch edge classes this module deliberately does not model —
#: named in the module docstring "Known-unmodelled edge classes" section.
UNMODELLED_EDGE_CLASSES: List[str] = [
    "detached_popen_spawn",
    "guard_sink_policy_edge",
    "consumes_manifest_bin_resolution",
]


def _call_target_name(node: ast.Call) -> str | None:
    """Return the bare callee name for a Call node (`f(...)` or `mod.f(...)`),
    or None for any other callee shape (e.g. a subscript or a call result)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_str(node: ast.AST | None) -> str | None:
    """Return the literal string value of a Constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_level_string_constants(tree: ast.Module) -> Dict[str, str]:
    """Names bound at module scope directly to a string ``ast.Constant``
    (``OP_X = "x.y"``, plain ``Assign`` or annotated ``AnnAssign`` alike).
    ONE hop only — a name bound to another name is not resolved."""
    consts: Dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            value = _literal_str(node.value)
            if value is not None:
                consts[node.targets[0].id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = _literal_str(node.value)
            if value is not None:
                consts[node.target.id] = value
    return consts


def _module_level_literal_collections(tree: ast.Module) -> Dict[str, List[str]]:
    """Names bound at module scope directly to a List/Tuple/Set literal
    whose every element is a string ``ast.Constant`` — the source a `for`
    target can be resolved against. A collection with any non-string-literal
    member is skipped entirely (no partial resolution)."""
    collections: Dict[str, List[str]] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target
            value = node.value
        if target is None or not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            continue
        members: List[str] = []
        all_str = True
        for elt in value.elts:
            member = _literal_str(elt)
            if member is None:
                all_str = False
                break
            members.append(member)
        if all_str and members:
            collections[target.id] = members
    return collections


def _resolve_expr_candidates(
    node: ast.AST | None,
    module_str_consts: Dict[str, str],
    active_loop_vars: Dict[str, List[str]],
) -> List[str]:
    """Resolve a call-argument expression to zero, one, or many op-name
    strings: a literal string yields itself; a `Name` resolves ONE hop
    against an enclosing `for` target bound to a module-scope literal
    collection first, else a module-scope string constant; anything else
    (call-bound name, f-string, concatenation, imported name, function
    parameter, function-local assignment) yields nothing."""
    literal = _literal_str(node)
    if literal is not None:
        return [literal]
    if isinstance(node, ast.Name):
        if node.id in active_loop_vars:
            return list(active_loop_vars[node.id])
        if node.id in module_str_consts:
            return [module_str_consts[node.id]]
    return []


class _CallSiteWalker(ast.NodeVisitor):
    """Walks a module tracking `for` loops whose iterable is a module-scope
    constant collection, so a call inside the loop body naming the loop
    target resolves to one entry per member (ONE hop, module scope)."""

    def __init__(self, module_str_consts: Dict[str, str], module_collections: Dict[str, List[str]]) -> None:
        self._module_str_consts = module_str_consts
        self._module_collections = module_collections
        self._loop_scopes: List[Dict[str, List[str]]] = []
        self.register_op_names: List[str] = []
        self.get_op_handler_names: List[str] = []
        self.dispatch_message_names: List[str] = []

    def _active_loop_vars(self) -> Dict[str, List[str]]:
        merged: Dict[str, List[str]] = {}
        for scope in self._loop_scopes:
            merged.update(scope)
        return merged

    def visit_For(self, node: ast.For) -> None:  # noqa: N802 (ast.NodeVisitor naming contract)
        scope: Dict[str, List[str]] = {}
        if isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Name):
            members = self._module_collections.get(node.iter.id)
            if members is not None:
                scope[node.target.id] = members
        self._loop_scopes.append(scope)
        self.generic_visit(node)
        self._loop_scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast.NodeVisitor naming contract)
        target = _call_target_name(node)
        active = self._active_loop_vars()
        if target == "register_op" and node.args:
            self.register_op_names.extend(
                _resolve_expr_candidates(node.args[0], self._module_str_consts, active)
            )
        elif target == "get_op_handler" and node.args:
            self.get_op_handler_names.extend(
                _resolve_expr_candidates(node.args[0], self._module_str_consts, active)
            )
        elif target == "dispatch_message" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Dict):
                for key, value in zip(first_arg.keys, first_arg.values):
                    if _literal_str(key) == "method":
                        self.dispatch_message_names.extend(
                            _resolve_expr_candidates(value, self._module_str_consts, active)
                        )
        self.generic_visit(node)


def _collect_call_site_names(tree: ast.Module) -> "_CallSiteWalker":
    """Run `_CallSiteWalker` over `tree` and return it, populated with
    every resolved `register_op`/`get_op_handler`/`dispatch_message`
    op-name site (literal-only plus the ONE-hop module-scope Name/`for`
    resolutions above)."""
    module_str_consts = _module_level_string_constants(tree)
    module_collections = _module_level_literal_collections(tree)
    walker = _CallSiteWalker(module_str_consts, module_collections)
    walker.visit(tree)
    return walker


def op_edges_for_file(target_root: str | Path, file_path: str | Path) -> Dict[str, Any]:
    """Return the registry-dispatch sites found in a single Python file.

    Args:
        target_root: containment root (see ``cartography._guard.path_guard``).
        file_path: absolute or ``target_root``-relative path to a ``*.py`` file.

    Returns:
        {"path": <str>, "registrations": [...], "lookups": [...], "dispatches": [...]}
        or, on a parse failure: {"path": <str>, "registrations": [], "lookups": [],
        "dispatches": [], "error": <str>}

    Never raises for a malformed target file. Raises ``PathEscapeError``
    (propagated) if ``file_path`` escapes ``target_root``.
    """
    root = Path(target_root).resolve()
    resolved = path_guard(target_root, file_path)
    rel_path = resolved.relative_to(root).as_posix()

    empty = {"registrations": [], "lookups": [], "dispatches": []}

    try:
        source = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": rel_path, **empty, "error": f"read error: {exc}"}

    try:
        tree = ast.parse(source, filename=str(resolved))
    except SyntaxError as exc:
        return {"path": rel_path, **empty, "error": f"SyntaxError: {exc}"}

    walker = _collect_call_site_names(tree)

    return {
        "path": rel_path,
        "registrations": walker.register_op_names,
        "lookups": walker.get_op_handler_names,
        "dispatches": walker.dispatch_message_names,
    }


def build_op_edges(target_root: str | Path, files: List[str | Path]) -> Dict[str, Any]:
    """Build the aggregate producer -> consumer registry-dispatch edge graph.

    Args:
        target_root: containment root (see ``op_edges_for_file``).
        files: list of absolute or ``target_root``-relative ``*.py`` paths.

    Returns:
        {
            "edges": [{"op": str, "from": str, "to": str, "kind":
                "get_op_handler" | "dispatch_message_literal"}, ...],
            "op_names": [<unique op-name strings from register_op sites>, ...],
            "registration_site_count": <int, one per register_op site, NOT
                de-duplicated by name>,
            "unmodelled": list(UNMODELLED_EDGE_CLASSES),
            "static_only": True,
        }

    ``op_names`` is de-duplicated (a `set`, sorted); ``registration_site_count``
    is not — the 866-decorator-sites/211-unique-names split this module is
    built against (2026-08-06 survey) is exactly the distinction those two
    fields exist to keep visible, rather than collapsing into one count a
    consumer would have to re-derive by re-walking the files themselves.

    ``edges`` joins each ``register_op`` producer site to each
    ``get_op_handler``/``dispatch_message`` consumer site sharing the
    identical literal op-name string, across ALL supplied files (including a
    producer and consumer edge within the SAME file — that is still a real
    registry-mediated join, not a static call, and is not suppressed).
    An op name with no registration site anywhere in `files` (the registering
    module lies outside the supplied file set) produces no edge for that
    name — this op is confined to `files`, same as every `cartography.*`
    sibling; it is not a repo-wide index.
    """
    per_file = [op_edges_for_file(target_root, f) for f in files]

    registrations_by_name: Dict[str, List[str]] = {}
    registration_site_count = 0
    for entry in per_file:
        for name in entry["registrations"]:
            registrations_by_name.setdefault(name, []).append(entry["path"])
            registration_site_count += 1

    edges: List[Dict[str, str]] = []
    for entry in per_file:
        consumer_path = entry["path"]
        for name in entry["lookups"]:
            for producer_path in registrations_by_name.get(name, []):
                edges.append(
                    {
                        "op": name,
                        "from": producer_path,
                        "to": consumer_path,
                        "kind": "get_op_handler",
                    }
                )
        for name in entry["dispatches"]:
            for producer_path in registrations_by_name.get(name, []):
                edges.append(
                    {
                        "op": name,
                        "from": producer_path,
                        "to": consumer_path,
                        "kind": "dispatch_message_literal",
                    }
                )

    op_names: Set[str] = set(registrations_by_name.keys())

    return {
        "edges": edges,
        "op_names": sorted(op_names),
        "registration_site_count": registration_site_count,
        "unmodelled": list(UNMODELLED_EDGE_CLASSES),
        "static_only": True,
    }
