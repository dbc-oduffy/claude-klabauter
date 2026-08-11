"""
coordinator_core.ops.cartography_symbols — JSON-RPC "cartography.symbols" operation.

Purpose: RPC wrapper merging two producers into the one `{"files": [...]}`
envelope: `.py` files go through the existing, unchanged
coordinator_core.cartography.symbols.build_symbols AST path; every other
file is routed through coordinator_core.ops.foreign_symbols.build_foreign_symbols
(example-retrieval-repo's `symbol_extract` tree-sitter adapter), and an extension that
adapter's own `languages` resolution cannot name is recorded in-band as
unsupported rather than silently dropped. Scope "none" / COMPUTE_ONLY,
mirrors ops/ping.py's registration pattern.

Self-registration: importing this module calls register_op("cartography.symbols",
_cartography_symbols) as a side-effect. Wired into
coordinator_core.ops.__init__, which imports this module — this op is LIVE on
the dispatch path.

Consumption status: UNCONSUMED — no call site exists today. Example-doctrine-repo's
frozen contract (`docs/contracts/arch-engine-scripts.md`) names this op
under its `arch-census` lane, but the survey's Workflow script does not call
it; only `cartography.chunk_table` and `cartography.churn` have call sites
(docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names"). Would be consumed by the
`arch-census`-lane annotation step this contract describes, if and when that
lane's Workflow script is wired.

Wire params:
    target_root (str, required) — containment root; every path in `files` is
                                   validated (post-resolve()) to be contained
                                   under this root before being read.
    files       (list[str], required) — absolute or target_root-relative
                                   paths to extract symbols for; `.py` files
                                   go through the AST path, everything else
                                   through the foreign_symbols adapter.

Reply fields:
    {"files": [<per-file symbol table>, ...]}  — one entry per requested
    file, in request order. A `.py` entry matches
    coordinator_core.cartography.symbols.symbol_table_for_file's shape
    verbatim (including the "error" field on a per-file parse failure). A
    non-Python entry matches foreign_symbols._envelope_for_file's shape
    (also carrying "error" on a per-file parse failure), or, for an
    extension foreign_symbols cannot resolve a language for, a distinct
    `{"path": ..., "unsupported": true, "detail": ...}` entry — never
    silently omitted.

    Graceful degradation (chunk C4a): when `symbol_extract` is not
    installed, every requested extraction-eligible non-Python file gets a
    distinct `{"path": ..., "unavailable": true, "detail": ...}` entry
    instead — deliberately NOT carrying "classes"/"functions"/"constants"
    keys at all, so it can never be confused with a successful-but-empty
    extraction. The reply additionally carries a top-level
    `"coverage_note"` string naming why non-Python symbol coverage was not
    attempted and the remedy (the `example-retrieval-repo-symbol-extract` extra,
    import name `symbol_extract`). The `.py` path is entirely unaffected —
    Python files in the same request still extract normally.

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler reads source text (via `Path.read_text` for `.py` files, via
     `symbol_extract.extract`'s internal read for everything else) and
     returns a computed dict; no file is opened for write, no git object is
     touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     Every file touched is opened read-only; no tempfile, no sentinel, no
     os.replace.
  4. Mutates shared mutable state outside its own module?                No.
     `ast.parse` and `symbol_extract.extract` both operate on read-only
     source text; no shared/global state is written by this handler, by
     coordinator_core.cartography.symbols, or by
     coordinator_core.ops.foreign_symbols.
  5. Persistent state changes observable across process boundaries?     No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller (or a raised, non-persistent error).
  Git-shelling-is-read-only precedent: this handler shells out to nothing —
  it is pure-Python `ast.parse` plus a third-party tree-sitter parse over
  already-read source text, a strictly narrower profile than coverage.gate's
  affirmed-COMPUTE_ONLY git subprocess reads (DR-208's own table: "all
  subprocess calls are read-only git queries"). No subprocess call is made
  here at all, on either path.

  `symbol_extract.extract()`'s own containment story, stated explicitly
  because it differs from the `.py` path's: `extract()` WALKS the whole tree
  under `target_root` internally (a cheap `os.walk`-class directory
  enumeration) but PARSES only the files this handler passes it via
  `foreign_symbols.build_foreign_symbols`'s `changed_files` set — so this
  handler's own read/return boundary stays `files`-scoped, the walk itself
  is the one residual whole-tree cost, and it never reads or returns
  anything outside the requested `files` list.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C4 (cartography.symbols); non-Python routing added by
docs/plans/2026-08-08-cartography-consumes-symbol-extract.md § chunk C3.

Negative-spec (AC5): `cartography.edges` / `cartography.op_edges` are NOT
routed to `symbol_extract` here or anywhere else — its tree-sitter tier
emits no edges, and this module adds no edge extraction of any kind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.symbols import build_symbols
from coordinator_core.ops.foreign_symbols import (
    build_foreign_symbols,
    claimed_extensions,
    classify_foreign_symbol_coverage,
)

# Coverage states that are infrastructure faults ONLY when symbol_extract IS
# installed and something is genuinely broken (chunk C4a). A finding in this
# set must fail the whole op loudly rather than returning a quietly-thin or
# quietly-empty symbol table for the extensions it names. `parse_failure` is
# deliberately excluded — it is already attributed onto its file's own
# envelope entry as an "error" field by foreign_symbols, matching
# cartography/symbols.py's existing per-file resilience, and must not fail
# the whole batch.
#
# `name_invariant_drop` is deliberately EXCLUDED from this set for the same
# reason as `parse_failure`: it is corpus hygiene, not an infrastructure
# fault. A drop diagnostic records that upstream's
# `ExtractionResult.__post_init__` choke point rejected ONE symbol's NAME
# (a line terminator or an over-length name) — the file itself parsed
# cleanly, and the drop is already routed onto that file's own envelope
# entry via `name_invariant_drops` (never `error`, by
# `foreign_symbols.build_foreign_symbols`) rather than being surfaced here.
# Including it in this set would fail the whole batch over a single
# malformed heading/identifier, exactly the kind of quietly-thin-vs-loudly-
# wrong tradeoff this set exists to draw a line under.
#
# `dependency_absent` is deliberately EXCLUDED from this set (chunk C4a — PM
# ruling 2026-08-08: "fails gracefully if the user doesn't have access to
# example-retrieval-repo"). `symbol_extract` ships from a private repo; a user without
# access must be able to install and use claude-klabauter without hitting an
# exception. This state is handled separately below: it degrades to a
# distinct, unmistakably-"not attempted" in-band marker per requested file,
# plus a top-level coverage note — never raised, never folded into an
# empty-but-successful extraction.
_LOUD_COVERAGE_STATES = frozenset({"missing_grammar", "partial_coverage"})

# The remedy surfaced in both the per-file "unavailable" marker's `detail`
# and the top-level coverage note when `symbol_extract` cannot be imported.
_SYMBOL_EXTRACT_REMEDY = (
    "install the optional extra to enable non-Python symbol extraction: "
    'python -m pip install -e ".[symbols]" from the claude-klabauter repo root '
    "(requires read access to the private example-retrieval-repo repo; the extra resolves "
    "a git-ref pin, and the underlying distribution is example-retrieval-repo-symbol-extract, "
    "import name symbol_extract)"
)
# Naming the distribution as the pip target would be actively misleading:
# coordinator_core is not published, so `pip install example-retrieval-repo-symbol-extract`
# reaches PyPI, finds nothing, and fails with "no matching distribution" —
# the exact confusing failure the spike verdict flagged. The extra is the only
# working entrypoint; the distribution name is context, not a command.


class CoverageFaultError(ValueError):
    """Raised when `classify_foreign_symbol_coverage` reports a loud
    coverage fault (`missing_grammar`/`partial_coverage`) for the requested
    non-Python files.

    Review: code-reviewer (P2, coordinatorcode-reviewer-1624a2c9.md) — the
    loud raise must stay loud (never softened to a return), but must not
    discard already-computed, unrelated-to-the-fault results. This subclass
    of `ValueError` carries those results so a catching caller can still use
    them; existing `except ValueError` callers are unaffected.

    Attributes:
        partial_reply (dict): the `{"files": [...]}` envelope entries that
            were already computed before the fault was detected — i.e. every
            requested `.py` file's symbol table plus every requested
            unsupported-extension file's in-band marker, in request order.
            Entries for the still-unresolved `other_files` batch (the one
            that triggered the fault) are absent. Never includes a
            `coverage_note` key.
    """

    def __init__(self, message: str, partial_reply: Dict[str, Any]):
        super().__init__(message)
        self.partial_reply = partial_reply


@register_op("cartography.symbols")
def _cartography_symbols(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.symbols" handler.

    Args (via params):
        target_root (str): containment root for every path in `files`.
        files (list[str]): file paths to extract symbol tables for; `.py`
            files use the stdlib `ast` path, everything else routes through
            `coordinator_core.ops.foreign_symbols`.

    Returns:
        {"files": [<symbol table dict per file>, ...]} — see this module's
        docstring "Reply fields" for the per-entry shape.

    Raises:
        ValueError — if `target_root` or `files` is missing (descriptive
        message naming the required param), matching the cartography.tree /
        cartography.file_index error contract; or if
        `classify_foreign_symbol_coverage` reports `missing_grammar` or
        `partial_coverage` for any extension among the requested non-Python
        files (chunk C3: these are infrastructure faults, never quietly
        downgraded to an empty or thin symbol table) — raised as
        `CoverageFaultError` (a `ValueError` subclass), which carries the
        already-computed `.py` and unsupported-extension results on its
        `partial_reply` attribute rather than discarding them; a caller not
        specifically catching `CoverageFaultError` still sees a loud
        `ValueError`. `dependency_absent` does NOT raise (chunk C4a) —
        `symbol_extract` simply not being installed is an expected, benign
        configuration; the op degrades gracefully instead (see module
        docstring "Reply fields").
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
        raise ValueError("cartography.symbols requires param: target_root")
    files = params.get("files")
    if not files:
        raise ValueError("cartography.symbols requires param: files")
    # Review: code-reviewer (P2, Finding 2, 2026-07-12-codereview-slicecartography-
    # substrate-b-wave) — guard target_root at the handler boundary, mirroring
    # cartography.tree/file_index, so a malformed root is rejected up front
    # (descriptive PathEscapeError) rather than surfacing incidentally, deep
    # inside the first file's per-file path_guard call.
    guarded_root = path_guard(target_root, ".")

    # Three-way partition, resolved BEFORE any adapter call: `.py` -> the AST
    # path; an extension the foreign-extraction seam claims -> the adapter;
    # everything else -> recorded in-band as unsupported without ever
    # calling `build_foreign_symbols` (chunk C3-fix). Calling the adapter
    # (and therefore `classify_foreign_symbol_coverage`) only when a request
    # actually asks for an extraction-eligible file keeps a request with no
    # such files from ever raising a coverage fault, even when the
    # `symbol_extract` dependency is absent.
    claimed = claimed_extensions() - {".py"}

    rel_by_request: Dict[str, str] = {}
    py_files: List[str] = []
    other_files: List[str] = []
    unsupported_files: List[str] = []
    for f in files:
        resolved = path_guard(guarded_root, f)
        rel = resolved.relative_to(guarded_root).as_posix()
        rel_by_request[f] = rel
        if resolved.suffix == ".py":
            py_files.append(f)
        elif resolved.suffix in claimed:
            other_files.append(f)
        else:
            unsupported_files.append(f)

    entries_by_rel: Dict[str, Dict[str, Any]] = {}

    if py_files:
        for entry in build_symbols(guarded_root, py_files)["files"]:
            entries_by_rel[entry["path"]] = entry

    for f in unsupported_files:
        rel = rel_by_request[f]
        ext = Path(rel).suffix
        entries_by_rel[rel] = {
            "path": rel,
            "unsupported": True,
            "detail": f"no symbol producer for extension {ext!r}",
        }

    coverage_note: Optional[str] = None

    if other_files:
        foreign_result = build_foreign_symbols(guarded_root, other_files)

        census: Dict[str, int] = {}
        for f in other_files:
            ext = Path(rel_by_request[f]).suffix
            census[ext] = census.get(ext, 0) + 1

        findings = classify_foreign_symbol_coverage(foreign_result, census)

        if "unavailable" in foreign_result:
            # Graceful degradation (chunk C4a): the extractor is simply not
            # installed — an expected, benign configuration, not a fault.
            # Every requested extraction-eligible file gets an in-band entry
            # that is unmistakably "not attempted", never an
            # empty-but-successful shape (no "classes"/"functions"/
            # "constants" keys at all — an empty list would read as a
            # successful-but-empty extraction).
            coverage_note = (
                "non-Python symbol coverage was not attempted: "
                f"{foreign_result['unavailable']}; {_SYMBOL_EXTRACT_REMEDY}"
            )
            for f in other_files:
                rel = rel_by_request[f]
                entries_by_rel[rel] = {
                    "path": rel,
                    "unavailable": True,
                    "detail": coverage_note,
                }
        else:
            loud = [finding for finding in findings if finding["state"] in _LOUD_COVERAGE_STATES]
            if loud:
                detail = "; ".join(
                    f"{finding['state']} (extension={finding['extension']!r}, "
                    f"language={finding['language']!r}): {finding['detail']}"
                    for finding in loud
                )
                # Review: code-reviewer (P2, coordinatorcode-reviewer-1624a2c9.md) —
                # keep raising loud (never soften to a return), but carry the
                # already-computed .py/unsupported results so a catching
                # caller doesn't lose unrelated, independently-computed work.
                partial_reply: Dict[str, Any] = {
                    "files": [
                        entries_by_rel[rel_by_request[f]]
                        for f in files
                        if rel_by_request[f] in entries_by_rel
                    ]
                }
                raise CoverageFaultError(
                    "cartography.symbols: foreign symbol coverage fault(s) — " + detail,
                    partial_reply,
                )

            languages = foreign_result.get("languages", {})
            for entry in foreign_result["files"]:
                ext = Path(entry["path"]).suffix
                if languages.get(ext) is None:
                    # Review: code-reviewer (Nit, coordinatorcode-reviewer-1624a2c9.md) —
                    # merge rather than overwrite so a pre-existing per-file
                    # "error" diagnostic on this entry survives the
                    # unsupported re-marking instead of being silently
                    # discarded.
                    entries_by_rel[entry["path"]] = {
                        "path": entry["path"],
                        "unsupported": True,
                        "detail": f"no symbol producer for extension {ext!r}",
                        **({"error": entry["error"]} if "error" in entry else {}),
                    }
                else:
                    entries_by_rel[entry["path"]] = entry

    reply: Dict[str, Any] = {"files": [entries_by_rel[rel_by_request[f]] for f in files]}
    if coverage_note is not None:
        reply["coverage_note"] = coverage_note
    return reply
