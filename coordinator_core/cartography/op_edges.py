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
  - Does NOT resolve a non-literal op-name argument (a variable, an
    f-string, a concatenation) at either end — every site this module
    records comes from an ``ast.Constant`` string literal argument/dict
    value; a call like ``get_op_handler(op_name)`` or
    ``dispatch_message(msg)`` (msg built elsewhere) contributes NO site.
    This is the same static-AST-only posture ``cartography.edges`` already
    documents for its own import/call graph, not a defect unique to this
    module — see that module's docstring negative-spec.
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


def _register_op_names(tree: ast.Module) -> List[str]:
    """Every op-name string literal passed as the first arg to a
    ``register_op(...)`` call — decorator form (``@register_op("x")``) and
    direct-call form (``register_op("x", handler)``) alike, since both are
    represented as ``ast.Call`` nodes reachable from ``ast.walk``."""
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_target_name(node) == "register_op" and node.args:
            name = _literal_str(node.args[0])
            if name is not None:
                names.append(name)
    return names


def _get_op_handler_names(tree: ast.Module) -> List[str]:
    """Every op-name string literal passed as the first arg to a
    ``get_op_handler(...)`` call."""
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_target_name(node) == "get_op_handler" and node.args:
            name = _literal_str(node.args[0])
            if name is not None:
                names.append(name)
    return names


def _dispatch_message_literal_names(tree: ast.Module) -> List[str]:
    """Every op-name string literal recovered from a
    ``dispatch_message({"method": "…", ...})`` call whose sole/first
    argument is an inline dict literal carrying a literal ``"method"`` key.
    A ``dispatch_message(msg)`` call (msg built elsewhere) contributes
    nothing — see module docstring negative-spec."""
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_target_name(node) == "dispatch_message" and node.args:
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Dict):
                continue
            for key, value in zip(first_arg.keys, first_arg.values):
                if _literal_str(key) == "method":
                    name = _literal_str(value)
                    if name is not None:
                        names.append(name)
    return names


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

    return {
        "path": rel_path,
        "registrations": _register_op_names(tree),
        "lookups": _get_op_handler_names(tree),
        "dispatches": _dispatch_message_literal_names(tree),
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
