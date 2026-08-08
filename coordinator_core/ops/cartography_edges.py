"""
coordinator_core.ops.cartography_edges — JSON-RPC "cartography.edges" and
"cartography.count_references" operations.

Purpose: Thin RPC wrapper over coordinator_core.cartography.edges.build_edges
— static import + intra-module call graph over a list of Python files, plus
decline-by-default cross-file call resolution (Rule 1: direct calls through
a confirmed import binding; Rule 2: import-bound attribute calls through a
confirmed module alias) — see coordinator_core.cartography.edges module
docstring for the full resolution spec. Scope "none" / COMPUTE_ONLY, mirrors
ops/ping.py's registration pattern.

"cartography.count_references" (C1f, docs/plans/2026-07-22-coordinator-ops-
buildout-from-fence-inventory.md § Mandated resolvers) is a second, thinner
RPC wrapper over the SAME build_edges graph: given a `module_name`, it counts
how many of the supplied files carry a static import edge naming that module
and lists which files those are. This WIRES UP the graph this module already
builds rather than porting the fence's `grep -rl <module> --include=*.py |
wc -l` shape — the fence's grep-count is a strict subset of the AST-derived
import-edge graph build_edges already produces (it also misses intra-module
call edges and self-describes its own static-only blind spot in-band), so
reusing build_edges retires the fence's Windows/GNU-grep hazard rather than
translating it (op-classification.tsv platform-hazard column, count-module-
cross-references row).

Self-registration: importing this module calls register_op("cartography.edges",
_cartography_edges) as a side-effect. Wired into coordinator_core.ops.__init__,
which imports this module — both ops here are LIVE on the dispatch path.

Consumption status: UNCONSUMED — neither "cartography.edges" nor
"cartography.count_references" has a call site today. Example-doctrine-repo's frozen
contract (`docs/contracts/arch-engine-scripts.md`) names "cartography.edges"
under its `arch-callgraph` lane, but the survey's Workflow script does not
call either op; only `cartography.chunk_table` and `cartography.churn` have
call sites (docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The
survey calls two of nine cartography op names").

Wire params:
    target_root (str, required) — containment root; every path in `files` is
                                   validated (post-resolve()) to be contained
                                   under this root before being read.
    files       (list[str], required) — absolute or target_root-relative
                                   paths to *.py files to build the edge
                                   graph over.
    path_system_map (dict[str, str], optional, C7) — caller-supplied
                                   {"<repo-relative path>": "<system>", ...}
                                   map, the same shape cartography.file_index
                                   emits under its "index" key. Purely
                                   additive: omitted, this op's output is
                                   byte-identical to pre-C7 behavior. See
                                   coordinator_core.cartography.edges module
                                   docstring, "BOUNDARY-MARKER JOIN", for the
                                   "boundary" field this adds to import edges
                                   when supplied, and "ROOT-MISMATCH
                                   DETECTION (AC16)" for the ValueError this
                                   param can trigger.

Reply fields:
    {"edges": [...], "excludes": ["register_op_dynamic_dispatch"], "static_only": true}
    — see coordinator_core.cartography.edges.build_edges for the per-entry
    shape. The "excludes"/"static_only" pair is the IN-BAND COMPLETENESS
    MARKER (AC7, the Staff Engineer Finding 4, 2026-07-12): this graph is a STATIC-ONLY
    graph — register_op dynamic-dispatch edges (op-name string key ->
    handler, resolved at runtime through the registry, never through a
    static `call` AST node) are categorically absent. See
    coordinator_core.cartography.edges module docstring, "THE HYBRID BLIND
    SPOT", and coordinator_core/cartography/tests/test_edges.py for the
    executable proof that a known register_op registration edge is absent
    from this op's output.

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler only reads *.py source text (open mode 'r') and returns a
     computed dict; no file is opened for write, no git object is touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     Every file touched is opened read-only via Path.read_text(); no tempfile,
     no sentinel, no os.replace.
  4. Mutates shared mutable state outside its own module?                No.
     ast.parse/ast.walk operate on an in-memory string/tree; no shared/global
     state is written by this handler or by coordinator_core.cartography.edges.
  5. Persistent state changes observable across process boundaries?     No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller.
  Git-shelling-is-read-only precedent: this handler shells out to nothing —
  it is pure-Python ast.parse/ast.walk over already-read source text, a
  strictly narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git
  subprocess reads (DR-208's own table: "all subprocess calls are read-only
  git queries"). No subprocess call is made here at all.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C4 (cartography.edges), AC7.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.edges import build_edges


@register_op("cartography.edges")
def _cartography_edges(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.edges" handler.

    Args (via params):
        target_root (str): containment root for every path in `files`.
        files (list[str]): *.py file paths to build the static edge graph over.

    Returns:
        {"edges": [...], "excludes": [...], "static_only": true}
        (see coordinator_core.cartography.edges.build_edges)

    Raises:
        ValueError — if `target_root` or `files` is missing (descriptive
        message naming the required param), matching the cartography.tree /
        cartography.file_index error contract. Also raised (C7) when
        `path_system_map` is supplied AND `target_root` disagrees with this
        corpus's own import root — see
        coordinator_core.cartography.edges._check_root_consistency.
        coordinator_core.cartography._guard.PathEscapeError — propagated,
        uncaught, if any entry in `files` resolves outside `target_root`.
        This is a containment violation, not a per-file data condition.
    """
    # Review: code-reviewer (P2, 2026-07-12-workflow-review-cartography.md) —
    # bare params[...] raised an uncaught, un-annotated KeyError on a missing
    # param, inconsistent with the descriptive-ValueError contract tree/
    # file_index already use in this same op family.
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.edges requires param: target_root")
    files = params.get("files")
    if not files:
        raise ValueError("cartography.edges requires param: files")
    path_system_map = params.get("path_system_map")
    # Review: code-reviewer (P2, Finding 2, 2026-07-12-codereview-slicecartography-
    # substrate-b-wave) — guard target_root at the handler boundary, mirroring
    # cartography.tree/file_index, so a malformed root is rejected up front
    # (descriptive PathEscapeError) rather than surfacing incidentally, deep
    # inside the first file's per-file path_guard call.
    guarded_root = path_guard(target_root, ".")
    return build_edges(guarded_root, files, path_system_map=path_system_map)


@register_op("cartography.count_references")
def _cartography_count_references(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "cartography.count_references" handler.

    Args (via params):
        target_root (str): containment root for every path in `files`.
        module_name (str): dotted module name to count import references to
            (matched against build_edges' per-edge "to" field, kind=="import",
            exact string equality — the same dotted-name shape
            coordinator_core.cartography.edges._module_name_for derives for
            the module a file DEFINES; this handler counts references TO that
            name from OTHER files' import statements).
        files (list[str]): *.py file paths to scan for references, same
            contract as cartography.edges' `files` param.

    Returns:
        {"reference_count": int, "referencing_files": list[str]}
        reference_count is the total count of qualifying import edges
        (a file importing module_name more than once contributes more than
        one to the count); referencing_files is the sorted, de-duplicated
        list of per-file "path" values (relative to target_root) that
        contributed at least one qualifying edge.

    Raises:
        ValueError — if `target_root`, `module_name`, or `files` is missing
        (descriptive message naming the required param), matching the
        cartography.edges error contract.
        coordinator_core.cartography._guard.PathEscapeError — propagated,
        uncaught, if any entry in `files` resolves outside `target_root`.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.count_references requires param: target_root")
    module_name = params.get("module_name")
    if not module_name:
        raise ValueError("cartography.count_references requires param: module_name")
    files = params.get("files")
    if not files:
        raise ValueError("cartography.count_references requires param: files")
    guarded_root = path_guard(target_root, ".")
    graph = build_edges(guarded_root, files)

    reference_count = 0
    referencing_files: set[str] = set()
    for per_file in graph["edges"]:
        matches = sum(
            1
            for edge in per_file["edges"]
            if edge["kind"] == "import" and edge["to"] == module_name
        )
        if matches:
            reference_count += matches
            referencing_files.add(per_file["path"])

    return {
        "reference_count": reference_count,
        "referencing_files": sorted(referencing_files),
    }
