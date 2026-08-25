"""
coordinator_core.cartography.edges — static import + intra-module call graph.

Purpose: pure-function producer of a static edge graph over a set of Python
files: (a) import edges (module A imports module B), (b) intra-module call
edges (function/method A calls name B, resolved against symbols defined in
the SAME file), (c) Rule-1 cross-file call edges (a direct call resolved
through a retained import-binding table, ONLY when the target module is
confirmed to define the symbol), and (d) Rule-2 cross-file call edges (an
attribute call resolved ONLY when the receiver is a name bound to a
first-party MODULE import and the attribute is confirmed defined there) —
see "CROSS-FILE CALL RESOLUTION" below for both rules. Built via the stdlib
``ast`` module; never executes target code.

Emission shape mirrors ``coordinator_core/ops/roadmap_dag.py``'s
``{nodes, edges}`` convention: this module emits
``{"edges": [...], "excludes": [...], "static_only": true}``.

THE HYBRID BLIND SPOT (AC7 — load-bearing, do not remove):
  ``coordinator_core.ipc.register_op`` is a DYNAMIC dispatch mechanism — an
  op module calls ``register_op("some.op", handler)`` as an IMPORT-TIME
  SIDE EFFECT, and the actual caller (the JSON-RPC dispatcher) looks the
  handler up by STRING KEY at request time, not via any static `call` AST
  node naming the handler. A static import+call walk over
  ``coordinator_core/ops/*.py`` and ``coordinator_core/ipc.py`` therefore
  NEVER produces an edge from "the dispatcher" to "the op handler" — that
  edge only exists at runtime, keyed through the registry dict populated by
  ``register_op``. This is NOT a bug to fix in this module: it is an
  irreducible property of static analysis over dynamic-dispatch code, and
  is exactly why every edges payload below self-describes its own
  incompleteness via the in-band ``excludes``/``static_only`` markers rather
  than silently presenting as complete. See
  ``coordinator_core/cartography/tests/test_edges.py::test_register_op_dynamic_dispatch_edge_is_absent``
  for the executable proof of this absence. Agents remain responsible for
  filling this dynamic layer (the "hybrid seam") until/unless a future
  runtime-trace producer is built — out of scope for this static module.

IN-BAND COMPLETENESS MARKER (patrik Finding 4, 2026-07-12): every payload
this module emits carries ``"static_only": true`` and an ``"excludes"``
list (currently ``["register_op_dynamic_dispatch"]``) alongside ``"edges"``,
so no consumer (Phase-0.5 gate, any future Tier-3 caller) can mistake the
static graph for a complete edge graph. One field, cheap, self-describing.

CROSS-FILE CALL RESOLUTION — RULE 1 ONLY (decline-by-default):
  A direct call (``ast.Name`` callee, e.g. ``foo()``) is resolved across
  files ONLY when a per-file ``bound_name -> source_module`` table, built
  from absolute (``level == 0``) ``ImportFrom`` statements and their
  aliases, names ``foo`` AND the target module is found under
  ``target_root`` AND that module is CONFIRMED to DEFINE the imported
  symbol at module level (parsed and checked, never assumed from the import
  binding alone). Every other shape declines — no edge, not a guessed one:
  the target module absent from ``target_root`` (external/stdlib/
  third-party), the target module present but NOT defining the symbol (the
  ``__init__`` re-export case), relative imports (``level > 0`` —
  normalization is a separate row's job, not resolved here), and any name
  that isn't import-bound at all (builtins, locals, parameters). Attribute
  calls (``x.foo()``) are UNCHANGED by this rule — still same-file-only.
  See ``docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md``
  for the measured yield (18.3% of direct calls resolved cross-file over
  the corpus this was proven against) and the precision-over-recall
  rationale: recall is recoverable later by adding rules; precision lost to
  a heuristic is not, because no consumer can tell which edges were
  guessed. This module exists because an agent doing this resolution by
  hand is confidently wrong in ways an op is not — a producer that emits
  plausible-but-false edges reintroduces that exact failure at the layer
  meant to eliminate it.

CROSS-FILE CALL RESOLUTION — RULE 2 ONLY (decline-by-default):
  An attribute call (``ast.Attribute`` callee, e.g. ``mod.foo()``) is
  resolved across files ONLY when the receiver is an ``ast.Name`` bound, via
  a per-file ``bound_name -> source_module`` table built from ``ast.Import``
  statements (``import x``, ``import x as y``), to a first-party MODULE —
  never an arbitrary object, never ``self.…`` — AND that module is CONFIRMED
  to DEFINE the attribute at module level (parsed and checked, same
  ``_defines_symbol_at_module`` machinery Rule 1 uses). Every other receiver
  shape declines — no edge, not a guessed one: an object instance, a
  ``self.…`` attribute access, an external (stdlib/third-party) module
  alias, a module alias whose target does not define the attribute, or any
  receiver that is not statically determinable at all (the result of a call,
  a subscript, an attribute chain, a parameter of unknown type, etc.). This
  mechanism deliberately does NOT collapse ``ast.Attribute`` to a bare
  ``func.attr`` and match it by name across the corpus — that field-based
  approach is the disproven mechanism this module exists to avoid (see
  spike verdict below). Measured over the spike verdict's corpus (85,548
  attribute calls): 13.3% resolved (module alias, attribute confirmed
  defined), 0.1% module alias but attribute not defined there (declines),
  13.4% external module alias (declines), 1.2% ``self.…`` (declines), and
  **72.1% receiver not statically determinable (declines)**. 72.1% is
  declined, and that is the point — the mechanism's value is that it never
  guesses. See
  ``docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md``
  for the full measured breakdown of both rules.

Negative-spec:
  - Does NOT resolve star-imports (``from x import *``) to individual names
    — recorded as a single import edge to the star-imported module only,
    and contributes no binding-table entry (nothing to bind ``*`` to).
  - Does NOT resolve an attribute call whose receiver is anything other than
    a name bound to a first-party MODULE import — an object instance,
    ``self.…``, an external module alias, or any receiver not statically
    determinable — those stay unresolved; call edges for such an attribute
    callee only connect a caller to a callee DEFINED IN THE SAME FILE (the
    unchanged same-file heuristic).
  - Does NOT resolve relative imports (``from . import x``, ``from .pkg
    import y``) to a bound name at all — normalizing a relative target to
    an absolute module name is a separate row's job; a call bound only
    through a relative import is left unresolved here.
  - Does NOT resolve a direct call whose bound name's source module is not
    found under ``target_root`` (external module) or is found but does not
    define the symbol at module level (re-export) — both decline to no
    edge, never a guessed one.
  - Does NOT resolve an attribute call whose module-alias receiver's target
    module does not define the attribute at module level — declines, never
    a guess.
  - Does NOT include any edge derived from ``register_op``, ``getattr``,
    ``importlib.import_module``, or any other dynamic-dispatch mechanism —
    see "THE HYBRID BLIND SPOT" above. (A *static* call to ``register_op``
    itself, e.g. ``from coordinator_core.ipc import register_op`` followed
    by ``register_op(...)``, IS a normal Rule 1 candidate like any other
    imported callable — that is a different edge from the dispatcher's
    runtime string-keyed lookup of the handler, which remains categorically
    unresolvable by any static walk.)

RELATIVE-IMPORT NORMALIZATION — ADDITIVE ONLY (C15): a relative import edge
(``level > 0``) carries an extra ``to_normalized`` field alongside ``to`` —
the PEP-328-resolved absolute dotted module name. ``to`` itself stays
byte-identical to today's un-normalized output (leading dots intact, or the
bare imported name for ``from . import x``) in every case; this module never
rewrites ``to`` in place. That is load-bearing, not stylistic:
``cartography.count_references`` filters edges by exact string equality on
``to`` with ``kind == "import"``, and the spike verdict measured that 31 of
the corpus's 39 relative-import edges normalize to a target that COLLIDES
with an existing absolute edge's ``to`` — an in-place rewrite would silently
inflate those 31 modules' reference counts with no signal the contract
moved. See
docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md
§ "The count_references hazard" for the measured figures.

BOUNDARY-MARKER JOIN — OPTIONAL, ADDITIVE (C7): ``build_edges`` accepts an
optional ``path_system_map`` (the same ``{"<repo-relative path>": "<system>",
...}`` shape ``cartography.file_index`` emits under its ``"index"`` key).
When supplied, every ``kind == "import"`` edge gains a ``"boundary"`` string
field, computed via a module-name -> path table built in the SAME pass that
already derives module names for ``files`` (see ``_module_path_table``) — NOT
via any inversion of ``file_index``'s own path->system map, which is keyed by
path, not by the dotted module names ``build_edges`` emits, so no such
inversion exists to build on. The plan's original premise asserted that
inversion; it is wrong as stated, corrected here per
``docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md``
§ "C7's join premise — corrected, and viable".

The four marker labels are transcribed verbatim (predicate meaning, not
prose) from DoE-claude's
``coordinator/pipelines/deep-architecture-survey/agent-prompts.md`` §
"Marker Reference" — this repo does not own that vocabulary, and a future
reader must not have to re-derive it. That doc's markers are stated from a
FUNCTION's perspective (``[ENTRY]``, ``[BOUNDARY -> system-name]``,
``[INTERNAL -> sub-chunk-label]``); ``_label_boundary`` restates the same
four predicates for a static IMPORT EDGE (caller file -> target module),
evaluated using BOTH endpoints' system membership — never the target path's
own basename or shape (an earlier draft of this row used an
``__init__.py``-basename heuristic for ``"entry"``; that heuristic
disagreed with the actual DoE contract and was removed per EM ruling,
2026-08-06):

  - ``"external"``    — the target does NOT invert to any first-party path
                         under ``target_root`` at all (stdlib/third-party).
                         Always this literal string, NEVER ``None``/``null``
                         — a consumer reading ``null`` cannot distinguish
                         "unclassified" from "correctly outside the tree",
                         and that distinction is load-bearing for anyone
                         building on this join.
  - ``"internal"``    — the target inverts to a first-party path, AND the
                         CALLER file also inverts (via ``path_system_map``)
                         to a first-party path in the SAME system.
  - ``"cross-system"`` — the target inverts to a first-party path, AND the
                         caller inverts to a first-party path in a
                         DIFFERENT system — DoE's ``[BOUNDARY -> system-
                         name]``: this call reaches INTO a different system.
  - ``"entry"``       — the target inverts to a first-party path, but the
                         CALLER does not resolve into the mapped system set
                         at all — DoE's ``[ENTRY]``: this target is reached
                         from OUTSIDE its own system entirely.

These four are mutually exclusive and total over every ``kind == "import"``
edge (AC8): exactly one predicate matches, in the order listed above.

Measured over the spike verdict's corpus: 747 of 843 distinct import targets
(88.6%) invert to a first-party path; the remaining 96 (11.4%) are
stdlib/third-party and label ``"external"``.

``path_system_map`` is entirely optional — omitting it (the default) leaves
every edge byte-identical to pre-C7 output; no existing consumer
(``cartography.count_references`` included) observes any change.

ROOT-MISMATCH DETECTION (AC16): the spike verdict records a near-miss where
the first probe rooted module-name derivation at ``coordinator_core/`` while
the corpus imports itself as ``coordinator_core.*`` — a root disagreeing with
the corpus's own import root, which silently reported near-zero resolution
(0.1%/1.6%) rather than failing loudly. ``_check_root_consistency`` (invoked
only when ``path_system_map`` is supplied, i.e. only when the join is
actually exercised) raises ``ValueError`` the moment an unresolved import
target would resolve against a KNOWN first-party module name under a
different (shorter) prefix — the exact shape of a caller passing a
``target_root`` one level too deep for how the corpus refers to itself. This
is a precise structural signal (a real module-name suffix match), not a
"resolution rate looks low" heuristic that would false-positive on ordinary
external-import-heavy corpora.

Spec backlink: pln-makima-cartography-substrate-a-26eb2e
§ chunk C4 (cartography.edges), AC7. Rule 1 cross-file resolution:
docs/plans/2026-08-06-makima-ize-the-survey-census.md § chunk C6. Rule 2
cross-file resolution: docs/plans/2026-08-06-makima-ize-the-survey-census.md
§ chunk C14. Relative-import normalization:
docs/plans/2026-08-06-makima-ize-the-survey-census.md § chunk C15. Boundary-
marker join: docs/plans/2026-08-06-makima-ize-the-survey-census.md § chunk
C7 — all respecified against
docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Set

from coordinator_core.cartography._guard import PathEscapeError, path_guard

STATIC_ONLY_EXCLUDES: List[str] = ["register_op_dynamic_dispatch"]


def _module_name_for(target_root: Path, file_path: Path) -> str:
    """Derive a dotted module name for file_path relative to target_root."""
    rel = file_path.relative_to(target_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        # Review: code-reviewer (nit, Finding 3, 2026-07-12-codereview-
        # slicecartography-substrate-b-wave) — a root-level __init__.py
        # strips to an empty parts list ("".join([]) == ""), which would
        # otherwise silently emit edges with a blank "from" field. Fall back
        # to the containing directory's name (target_root's own basename)
        # rather than emitting an empty module name.
        return target_root.name
    return ".".join(parts)


def _normalize_relative_import(module_name: str, node_module: str | None, level: int) -> str:
    """PEP-328-resolve a relative import target to an absolute dotted module
    name, anchored at the IMPORTING file's own ``module_name``.

    ``level`` dots strip that many trailing components from the importing
    package (``level == 1`` is the immediate package, i.e. ``from . import
    x`` resolves relative to the package containing the importing module;
    each additional level strips one more ancestor). ``node_module`` is the
    text after the dots (``None`` for ``from . import x`` with no module
    name — the anchor package itself). This is purely additive metadata (see
    ``to_normalized`` at the call site in ``_import_edges``): it never
    changes ``to``, which stays byte-identical to today's un-normalized
    output — ``cartography.count_references`` keeps filtering on ``to``
    exactly as it does today (see spike verdict's measured 31-of-39
    collision hazard for why an in-place rewrite is unsafe).
    """
    anchor_parts = module_name.split(".")
    # The importing module ITSELF is one component past its containing
    # package; `level == 1` ("from . import x") resolves relative to that
    # containing package, so drop one extra component beyond `level`.
    strip = level
    package_parts = anchor_parts[: len(anchor_parts) - strip] if strip else anchor_parts
    if node_module:
        package_parts = package_parts + node_module.split(".")
    return ".".join(package_parts)


def _import_edges(
    tree: ast.Module,
    module_name: str,
    target_root: Path | None = None,
    definition_cache: Dict[str, Set[str] | None] | None = None,
) -> List[Dict[str, str]]:
    """``target_root``/``definition_cache`` are OPTIONAL — supplied by
    ``edges_for_file`` to disambiguate the module-less relative-import shape
    (``from . import x``) below; omitted (e.g. a direct unit-test call),
    ``to_normalized`` is simply declined for that shape rather than guessed.
    """
    if definition_cache is None:
        definition_cache = {}
    edges: List[Dict[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append({"from": module_name, "to": alias.name, "kind": "import"})
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # relative "from . import x" with no module name — record per-name
                for alias in node.names:
                    edge = {"from": module_name, "to": alias.name, "kind": "import"}
                    if node.level and node.level > 0:
                        normalized = _normalize_module_less_relative_import(
                            module_name,
                            alias.name,
                            node.level,
                            target_root,
                            definition_cache,
                        )
                        if normalized is not None:
                            edge["to_normalized"] = normalized
                    edges.append(edge)
                continue
            target = node.module
            edge = {"from": module_name, "to": target, "kind": "import"}
            if node.level and node.level > 0:
                edge["to"] = "." * node.level + target
                edge["to_normalized"] = _normalize_relative_import(
                    module_name, node.module, node.level
                )
            edges.append(edge)
    return edges


def _normalize_module_less_relative_import(
    module_name: str,
    imported_name: str,
    level: int,
    target_root: Path | None,
    definition_cache: Dict[str, Set[str] | None],
) -> str | None:
    """Disambiguate ``to_normalized`` for ``from . import x`` (``level`` dots,
    no ``node.module``) — decline-by-default, per module docstring "RELATIVE-
    IMPORT NORMALIZATION": ``imported_name`` (``x``) is EITHER a submodule of
    the anchor package (``anchor.x`` is a real file) OR a name re-exported
    from the anchor package's own ``__init__.py`` (the normalized target is
    the anchor itself, NOT ``anchor.x``) — and this function cannot tell
    which without ``target_root`` to check both candidates against.

    Returns the resolved absolute dotted name, or ``None`` (declined) when
    ``target_root`` is absent, or neither candidate is confirmed — never a
    guess between the two.
    """
    if target_root is None:
        return None
    anchor = _normalize_relative_import(module_name, None, level)
    submodule_candidate = f"{anchor}.{imported_name}"
    if _module_file_path(target_root, submodule_candidate) is not None:
        return submodule_candidate
    if _defines_symbol_at_module(target_root, anchor, imported_name, definition_cache):
        return anchor
    return None


def _import_bindings(tree: ast.Module) -> Dict[str, tuple[str, str]]:
    """Per-file ``bound_name -> (source_module, imported_name)`` table.

    Built ONLY from absolute (``node.level == 0``) ``ImportFrom`` statements
    with an explicit ``node.module`` — the substrate Rule 1 resolves direct
    calls through. Relative imports (``level > 0``) are deliberately
    excluded: normalizing a relative target to an absolute module name is a
    separate row's job (see module docstring negative-spec), not guessed
    here. Star imports (``alias.name == "*"``) contribute no binding — there
    is no single name to bind ``*`` to.
    """
    bindings: Dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level or node.module is None:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            bound_name = alias.asname or alias.name
            bindings[bound_name] = (node.module, alias.name)
    return bindings


def _module_import_bindings(tree: ast.Module) -> Dict[str, str]:
    """Per-file ``bound_name -> source_module`` table for MODULE aliases —
    the substrate Rule 2 resolves attribute-call receivers through.

    Built ONLY from ``ast.Import`` statements (``import x``, ``import x as
    y``, ``import x.y.z``) — this is a categorically different table from
    ``_import_bindings`` (which tracks ``from x import y`` NAME bindings).
    Rule 2 needs a name bound to a MODULE, because it resolves
    ``receiver.attr()`` by looking up ``attr`` as a top-level definition
    inside the module ``receiver`` is bound to.

    ``import x as y`` binds ``y`` to the full dotted target ``x``. A bare
    ``import x`` with no dot binds ``x`` to itself. A bare ``import x.y.z``
    with no ``asname`` binds only the top-level name ``x`` (Python's own
    binding rule) to the top-level module ``x`` — NOT to ``x.y.z`` — so a
    receiver bound this way can only resolve an attribute defined on ``x``
    itself; deeper submodule attributes are out of reach for this
    single-hop lookup and correctly decline via ``_defines_symbol_at_module``
    finding no match, never a guess.
    """
    bindings: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.asname:
                bindings[alias.asname] = alias.name
            else:
                bindings[alias.name.split(".")[0]] = alias.name.split(".")[0]
    return bindings


def _module_file_path(target_root: Path, module_name: str) -> Path | None:
    """Reverse of ``_module_name_for``: locate the file defining
    ``module_name`` under ``target_root``, or ``None`` if it does not
    resolve within ``target_root`` at all (external/stdlib/third-party
    module, or simply absent from this corpus) — the caller treats ``None``
    as a decline, never a guess.
    """
    parts = module_name.split(".")
    module_file = target_root.joinpath(*parts).with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_init = target_root.joinpath(*parts, "__init__.py")
    if package_init.is_file():
        return package_init
    return None


def _defines_symbol_at_module(
    target_root: Path,
    module_name: str,
    symbol: str,
    definition_cache: Dict[str, Set[str] | None],
) -> bool:
    """True iff ``module_name`` resolves to a file under ``target_root``
    that DEFINES ``symbol`` at module level (top-level function or class) —
    never true on the import binding alone. Declines (returns ``False``,
    same as any other unresolved shape) for a module not found under
    ``target_root``, a read error, or a ``SyntaxError`` — mirrors
    ``edges_for_file``'s own non-raising error handling rather than
    propagating a parse failure in a SIBLING file into this file's edges.
    ``definition_cache`` avoids re-parsing the same target module for every
    call site that binds to it within one ``edges_for_file`` invocation.
    """
    if module_name not in definition_cache:
        module_file = _module_file_path(target_root, module_name)
        if module_file is None:
            definition_cache[module_name] = None
        else:
            try:
                source = module_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(module_file))
            except (OSError, SyntaxError):
                definition_cache[module_name] = None
            else:
                definition_cache[module_name] = _collect_top_level_defs(tree)
    defs = definition_cache[module_name]
    return defs is not None and symbol in defs


def _collect_top_level_defs(tree: ast.Module) -> Set[str]:
    """Names defined at module level: top-level functions and classes.

    Class methods are intentionally excluded from this set — intra-module
    call-edge resolution here is name-based against top-level defs only, not
    a full attribute-resolution (``self.foo()`` is not distinguished from a
    bare ``foo()`` call to a same-named top-level function).
    """
    names: Set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
    return names


def _call_edges(
    tree: ast.Module,
    module_name: str,
    target_root: Path,
    bindings: Dict[str, tuple[str, str]],
    module_bindings: Dict[str, str],
    definition_cache: Dict[str, Set[str] | None],
) -> List[Dict[str, str]]:
    """Intra-module + Rule-1 + Rule-2 cross-file call edges: caller
    (top-level func/class) -> callee.

    Same-file resolution (unchanged): a callee name — ``ast.Name`` or
    ``ast.Attribute`` — matching a name defined at module level in the SAME
    file is recorded; this stays a name-based, not type-resolved, heuristic
    (see module docstring negative-spec).

    Cross-file resolution (Rule 1): ONLY for a direct call (``ast.Name``
    callee) that did NOT resolve same-file — if the callee name is bound via
    ``bindings`` (an absolute ``ImportFrom``) AND the bound source module is
    confirmed, by parsing, to DEFINE the imported symbol, a call edge to
    ``{source_module}.{imported_name}`` is recorded.

    Cross-file resolution (Rule 2, decline-by-default): ONLY for an
    attribute call (``ast.Attribute`` callee, e.g. ``mod.foo()``) that did
    NOT resolve same-file — if the receiver is an ``ast.Name`` bound via
    ``module_bindings`` (an ``ast.Import``, i.e. a MODULE alias, never an
    arbitrary object) AND the bound module is confirmed, by parsing, to
    DEFINE the attribute at module level, a call edge to
    ``{source_module}.{attr}`` is recorded. Every other receiver shape —
    an object instance, ``self.…``, an external module alias, anything not
    statically determinable — declines: no edge, not a guessed one. This
    mechanism does NOT collapse ``ast.Attribute`` to a bare ``func.attr``
    and match it by name across the corpus; measured over the spike
    verdict's corpus, 72.1% of attribute call sites decline for exactly
    this reason (receiver not statically determinable), and that decline
    rate is the mechanism working as intended, not a coverage gap.
    """
    top_level_defs = _collect_top_level_defs(tree)
    edges: List[Dict[str, str]] = []

    def _scan_caller(caller_name: str, body_node: ast.AST) -> None:
        for node in ast.walk(body_node):
            if isinstance(node, ast.Call):
                func = node.func
                callee: str | None = None
                if isinstance(func, ast.Name):
                    callee = func.id
                elif isinstance(func, ast.Attribute):
                    callee = func.attr
                if callee is not None and callee in top_level_defs and callee != caller_name:
                    edges.append(
                        {
                            "from": f"{module_name}.{caller_name}",
                            "to": f"{module_name}.{callee}",
                            "kind": "call",
                        }
                    )
                elif isinstance(func, ast.Name) and func.id in bindings:
                    source_module, imported_name = bindings[func.id]
                    if _defines_symbol_at_module(
                        target_root, source_module, imported_name, definition_cache
                    ):
                        edges.append(
                            {
                                "from": f"{module_name}.{caller_name}",
                                "to": f"{source_module}.{imported_name}",
                                "kind": "call",
                            }
                        )
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in module_bindings
                ):
                    source_module = module_bindings[func.value.id]
                    attr = func.attr
                    if _defines_symbol_at_module(
                        target_root, source_module, attr, definition_cache
                    ):
                        edges.append(
                            {
                                "from": f"{module_name}.{caller_name}",
                                "to": f"{source_module}.{attr}",
                                "kind": "call",
                            }
                        )

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _scan_caller(stmt.name, stmt)
        elif isinstance(stmt, ast.ClassDef):
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _scan_caller(item.name, item)

    return edges


def _module_path_table(target_root: Path, files: List[str | Path]) -> Dict[str, str]:
    """Module-name -> repo-relative-path table, built over ``files`` via the
    SAME ``_module_name_for`` derivation ``edges_for_file`` already uses for
    each file's own ``"from"`` module name — the join C7 needs (see module
    docstring "BOUNDARY-MARKER JOIN"). A ``file_path`` that fails
    ``path_guard`` containment is skipped, not raised — that containment
    violation is already raised loudly by ``edges_for_file``'s own pass over
    the same ``files`` list; this table-building pass does not duplicate
    that error, it only skips what the other pass will already reject.
    """
    table: Dict[str, str] = {}
    for file_path in files:
        try:
            resolved = path_guard(target_root, file_path)
        except PathEscapeError:
            continue
        if not resolved.is_file():
            continue
        rel_path = resolved.relative_to(target_root).as_posix()
        table[_module_name_for(target_root, resolved)] = rel_path
    return table


def _check_root_consistency(
    module_path_table: Dict[str, str], import_targets: Set[str]
) -> None:
    """Raise ``ValueError`` iff an unresolved import target would resolve
    against a KNOWN first-party module name under a shorter prefix — the
    exact root-mismatch shape the spike verdict documents (derivation root
    rooted one level deeper than the corpus's own self-import root),
    surfaced loudly instead of silently reporting near-zero resolution.
    See module docstring "ROOT-MISMATCH DETECTION (AC16)".
    """
    if not module_path_table:
        return
    for target in import_targets:
        if target in module_path_table:
            continue
        parts = target.split(".")
        for i in range(1, len(parts)):
            suffix = ".".join(parts[i:])
            # Known narrow false-positive shape (Review: code-reviewer, P3):
            # an external target (e.g. "os.path") whose trailing dotted
            # component coincidentally equals an unrelated first-party
            # top-level module name (e.g. a first-party module literally
            # named "path.py") raises here even though target_root is
            # correct. Accepted precision/recall tradeoff — narrowing this
            # further (minimum suffix length, top-level-entry-only match)
            # risks masking a genuine root mismatch; left as documented in
            # the module docstring "ROOT-MISMATCH DETECTION" rather than
            # narrowed.
            if suffix in module_path_table:
                raise ValueError(
                    "cartography.edges: module-name derivation root "
                    f"disagrees with this corpus's own import root — "
                    f"{target!r} would resolve to first-party module "
                    f"{suffix!r} under a different (shorter) target_root. "
                    "Re-check target_root against how this corpus refers to "
                    "itself (see spike verdict § root-mismatch note)."
                )


def _label_boundary(
    target_module: str,
    module_path_table: Dict[str, str],
    path_system_map: Dict[str, str],
    caller_system: str | None,
) -> str:
    """Label a single import edge's target per the boundary-marker join —
    predicates transcribed from DoE-claude's ``deep-architecture-survey/
    agent-prompts.md`` § "Marker Reference"; see module docstring
    "BOUNDARY-MARKER JOIN" for the full mapping and citation. Always
    returns a real string; never ``None``.

    ``caller_system`` is ``path_system_map.get(<caller file's own path>)``,
    resolved by the caller of this function (``build_edges``) — ``None``
    iff the caller file itself does not resolve into the mapped system set
    at all, which is exactly DoE's ``[ENTRY]`` predicate for the target.
    """
    target_path = module_path_table.get(target_module)
    if target_path is None:
        return "external"
    if caller_system is None:
        return "entry"
    target_system = path_system_map.get(target_path)
    if target_system == caller_system:
        return "internal"
    return "cross-system"


def edges_for_file(
    target_root: str | Path,
    file_path: str | Path,
    definition_cache: Dict[str, Set[str] | None] | None = None,
) -> Dict[str, Any]:
    """Return import + intra-module call edges for a single Python file.

    Args:
        target_root: containment root (see ``cartography._guard.path_guard``).
        file_path: absolute or ``target_root``-relative path to a ``*.py`` file.
        definition_cache: OPTIONAL — a ``module_name -> {defined symbols}``
            cache (see ``_defines_symbol_at_module``). Omitted (the default,
            and every existing single-file caller), a fresh cache is created
            for this call only. ``build_edges`` passes ONE cache shared
            across every file in its ``files`` list, so a hot cross-file
            target (e.g. ``coordinator_core/ipc.py``, imported+called from
            many op modules) is parsed once per ``build_edges`` invocation
            rather than once per calling file. Purely a perf optimization —
            does not change any emitted edge.

    Returns:
        {"path": <str>, "edges": [{"from", "to", "kind"}, ...]}
        or, on a parse failure: {"path": <str>, "edges": [], "error": <str>}

    Never raises for a malformed target file. Raises ``PathEscapeError``
    (propagated) if ``file_path`` escapes ``target_root``.
    """
    root = Path(target_root).resolve()
    resolved = path_guard(target_root, file_path)
    rel_path = resolved.relative_to(root).as_posix()

    try:
        source = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": rel_path, "edges": [], "error": f"read error: {exc}"}

    try:
        tree = ast.parse(source, filename=str(resolved))
    except SyntaxError as exc:
        return {"path": rel_path, "edges": [], "error": f"SyntaxError: {exc}"}

    module_name = _module_name_for(root, resolved)
    bindings = _import_bindings(tree)
    module_bindings = _module_import_bindings(tree)
    if definition_cache is None:
        definition_cache = {}
    edges = _import_edges(tree, module_name, root, definition_cache) + _call_edges(
        tree, module_name, root, bindings, module_bindings, definition_cache
    )
    return {"path": rel_path, "edges": edges}


def build_edges(
    target_root: str | Path,
    files: List[str | Path],
    path_system_map: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Build the aggregate static edge-graph payload for a list of Python files.

    Args:
        target_root: containment root (see ``edges_for_file``).
        files: list of absolute or ``target_root``-relative ``*.py`` paths.
        path_system_map: OPTIONAL, additive — a caller-supplied
            ``{"<repo-relative path>": "<system>", ...}`` map, the same
            shape ``cartography.file_index`` emits under its ``"index"``
            key. When supplied, every ``kind == "import"`` edge gains a
            ``"boundary"`` field (see module docstring "BOUNDARY-MARKER
            JOIN"). Omitted (the default), every edge is byte-identical to
            pre-C7 output — this param changes nothing for an existing
            caller, ``cartography.count_references`` included.

    Returns:
        {
            "edges": [<per-file edges_for_file(...) entry>, ...],
            "excludes": ["register_op_dynamic_dispatch"],
            "static_only": true,
        }

    The ``excludes``/``static_only`` pair is the IN-BAND completeness marker
    (AC7, patrik Finding 4) — always present, regardless of input, so a
    consumer can never mistake this payload for a complete edge graph. See
    module docstring "THE HYBRID BLIND SPOT" for why register_op edges are
    categorically absent from any output of this function.

    Raises:
        ValueError — via ``_check_root_consistency``, ONLY when
        ``path_system_map`` is supplied AND ``target_root`` disagrees with
        the corpus's own import root (see module docstring "ROOT-MISMATCH
        DETECTION (AC16)").
    """
    definition_cache: Dict[str, Set[str] | None] = {}
    per_file = [edges_for_file(target_root, f, definition_cache) for f in files]
    if path_system_map is not None:
        root = Path(target_root).resolve()
        module_path_table = _module_path_table(root, files)
        import_targets: Set[str] = {
            edge.get("to_normalized", edge["to"])
            for entry in per_file
            for edge in entry["edges"]
            if edge.get("kind") == "import"
        }
        _check_root_consistency(module_path_table, import_targets)
        for entry in per_file:
            caller_system = path_system_map.get(entry["path"])
            for edge in entry["edges"]:
                if edge.get("kind") != "import":
                    continue
                target_module = edge.get("to_normalized", edge["to"])
                edge["boundary"] = _label_boundary(
                    target_module, module_path_table, path_system_map, caller_system
                )
    return {
        "edges": per_file,
        "excludes": list(STATIC_ONLY_EXCLUDES),
        "static_only": True,
    }
