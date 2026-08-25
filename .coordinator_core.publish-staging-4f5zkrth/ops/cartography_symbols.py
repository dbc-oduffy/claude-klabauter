"""
coordinator_core.ops.cartography_symbols — JSON-RPC "cartography.symbols" operation.

Purpose: RPC wrapper merging two producers into the one `{"files": [...]}`
envelope: `.py` files go through the existing, unchanged
coordinator_core.cartography.symbols.build_symbols AST path; every other
file is routed through coordinator_core.ops.foreign_symbols.build_foreign_symbols
(project-rag's `symbol_extract` tree-sitter adapter), and an extension that
adapter's own `languages` resolution cannot name is recorded in-band as
unsupported rather than silently dropped. Scope "none" / COMPUTE_ONLY,
mirrors ops/ping.py's registration pattern.

Self-registration: importing this module calls register_op("cartography.symbols",
_cartography_symbols) as a side-effect. Wired into
coordinator_core.ops.__init__, which imports this module — this op is LIVE on
the dispatch path.

Consumption status: UNCONSUMED — no call site exists today. DoE-claude's
frozen contract (`docs/contracts/arch-engine-scripts.md`) names this op
under its `arch-census` lane, but the survey's Workflow script does not call
it; only `cartography.chunk_table` and `cartography.churn` have call sites
(docs/plans/2026-08-06-makima-ize-the-survey-census.md § "The survey calls
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
    run_id      (str, required)  — caller-supplied, safe_id-validated path
                                   segment; confines the emit write to this
                                   run's own subdirectory (D6(i)). Required
                                   unconditionally (even when emit is
                                   false/absent) so a bare compute-only call
                                   and an emitting call share one param
                                   contract — no separate un-run_id'd shape,
                                   mirroring cartography.chunk_table's
                                   run_id contract.
    emit        (bool, optional, default false) — controls the disk write
                                   (DR-228 § D6 scratch-tier convention,
                                   mirrors cartography.chunk_table's emit).

Reply fields:
    With `emit` absent/false: `{"files": [<per-file symbol table>, ...]}`
    (plus an optional top-level "coverage_note", unchanged from before this
    param existed) — one entry per requested file, in request order. A
    `.py` entry matches coordinator_core.cartography.symbols.symbol_table_
    for_file's shape verbatim (including the "error" field on a per-file
    parse failure). A non-Python entry matches
    foreign_symbols._envelope_for_file's shape (also carrying "error" on a
    per-file parse failure), or, for an extension foreign_symbols cannot
    resolve a language for, a distinct `{"path": ..., "unsupported": true,
    "detail": ...}` entry — never silently omitted. No file is opened for
    write on this path (AC1, AC7): the reply is byte-identical to every
    pre-emit-param run.

    With `emit: true`: writes a `schema_version`-pinned JSON artifact to
    <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json
    (SCHEMA_VERSION as its first key, per DR-228 § D6(v)), carrying
    `schema_version`, `run_id`, `target_root`, `files` (the same per-file
    envelope as above — including each entry's own `other_symbols` /
    `unmapped_kinds`-attributed content), `counts`, and, when present,
    `coverage_note` / `completeness` (the foreign-extraction
    completeness/coverage surface — see foreign_symbols._envelope_for_file
    and build_foreign_symbols). The reply itself is the artifact dict minus
    `files` (AC2's "does NOT carry files" — the bulky body stays on disk),
    plus `symbols_path` (rel-posix to `target_root`).

    Graceful degradation (chunk C4a): when `symbol_extract` is not
    installed, every requested extraction-eligible non-Python file gets a
    distinct `{"path": ..., "unavailable": true, "detail": ...}` entry
    instead — deliberately NOT carrying "classes"/"functions"/"constants"
    keys at all, so it can never be confused with a successful-but-empty
    extraction. The reply additionally carries a top-level
    `"coverage_note"` string naming why non-Python symbol coverage was not
    attempted and the remedy (the `project-rag-symbol-extract` extra,
    import name `symbol_extract`). The `.py` path is entirely unaffected —
    Python files in the same request still extract normally.

DR-208 five-question affirmation (MUTATING; citing this handler — the
authoritative affirmation lives at coordinator_core/authz/classification.py's
"cartography.symbols" entry, this is a summary):
  1. Writes, deletes, or reorders any state file, queue, or git object?  YES,
     conditionally. When `emit` is truthy, `write_symbols_artifact`
     (mkstemp+os.replace) writes exactly one whole-JSON artifact to
     <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json.
     When `emit` is absent/false, no file is opened for write on either the
     `.py` (`Path.read_text`) or foreign (`symbol_extract.extract`'s
     internal read) path — read-only, same as before this param existed.
  2. Writes into rag's relational store?                                 No.
     Writes only the local scratch-tier JSON artifact; no rag interaction.
  3. Opens any file for write (including sentinel creation)?          YES,
     conditionally — same `emit` gate as #1.
  4. Mutates shared mutable state outside its own module?                YES,
     conditionally. target_root/state/scratch/ is coordinator substrate
     (DR-228 § D6, extended to this op's own named subdirectory) when
     `emit` is truthy; otherwise no shared/global state is written.
  5. Persistent state changes observable across process boundaries?     YES,
     conditionally — a consumer reading the emitted artifact back off disk
     sees it, when `emit` was truthy; nothing is written otherwise.
  Classified MUTATING unconditionally (per-op, not per-call, per DR-208's
  classification granularity) even though a bare compute-and-return call
  (emit absent/false) performs no write — a writing op sitting at
  COMPUTE_ONLY, even for calls that don't write, is the authz hole DR-208's
  fail-closed default exists to close.
  Git-shelling-is-read-only precedent (for the read side of both paths):
  this handler shells out to nothing — it is pure-Python `ast.parse` plus a
  third-party tree-sitter parse over already-read source text, a strictly
  narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess
  reads (DR-208's own table: "all subprocess calls are read-only git
  queries"). No subprocess call is made here at all, on either path.

  `symbol_extract.extract()`'s own containment story, stated explicitly
  because it differs from the `.py` path's: `extract()` WALKS the whole tree
  under `target_root` internally (a cheap `os.walk`-class directory
  enumeration) but PARSES only the files this handler passes it via
  `foreign_symbols.build_foreign_symbols`'s `changed_files` set — so this
  handler's own read/return boundary stays `files`-scoped, the walk itself
  is the one residual whole-tree cost, and it never reads or returns
  anything outside the requested `files` list.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: pln-makima-cartography-substrate-a-26eb2e
§ chunk C4 (cartography.symbols); non-Python routing added by
docs/plans/2026-08-08-cartography-consumes-symbol-extract.md § chunk C3.

Negative-spec (AC5): `cartography.edges` / `cartography.op_edges` are NOT
routed to `symbol_extract` here or anywhere else — its tree-sitter tier
emits no edges, and this module adds no edge extraction of any kind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.symbols import build_symbols
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.foreign_symbols import (
    build_foreign_symbols,
    claimed_extensions,
    classify_foreign_symbol_coverage,
)

__all__ = [
    "SCHEMA_VERSION",
    "check_schema_version",
    "build_symbols_artifact",
    "write_symbols_artifact",
]

#: Schema version for this op's emitted JSON artifact — always the first key
#: written on disk (mirrors cartography_chunk_table.SCHEMA_VERSION's
#: convention, DR-228 § D6(v)). A standalone constant because this artifact
#: is a genuinely different shape (per-file symbol tables, not a
#: bucket/chunk reduction) with its own independent version lineage.
SCHEMA_VERSION: int = 1

#: Every schema_version this module can consume without failing loud — see
#: cartography_chunk_table._KNOWN_SCHEMA_VERSIONS for why this is a set, not
#: a single ceiling comparison, even though only one version exists today.
_KNOWN_SCHEMA_VERSIONS: frozenset[int] = frozenset({SCHEMA_VERSION})


class SymbolsSchemaError(ValueError):
    """Raised when a cartography.symbols JSON artifact carries an unknown
    FORWARD schema_version (newer than this module knows) — fail-loud
    consumption, per DR-228 § D6(v)."""


def check_schema_version(payload: Dict[str, Any]) -> None:
    """Fail loud on an unknown forward schema_version; silent on a known
    version — DR-228 § D6(v)'s consumption contract, mirroring
    cartography_chunk_table.check_schema_version."""
    version = payload.get("schema_version")
    if not isinstance(version, int) or version not in _KNOWN_SCHEMA_VERSIONS:
        raise SymbolsSchemaError(
            f"cartography.symbols artifact has unknown forward schema_version "
            f"{version!r} (this module knows {sorted(_KNOWN_SCHEMA_VERSIONS)!r})"
        )


def _count_entries(files: List[Dict[str, Any]]) -> Dict[str, int]:
    """Small per-artifact census over the per-file envelope entries —
    top-level counts, not a per-file breakdown (mirrors
    cartography_chunk_table's `counts` field)."""
    counts = {"files": len(files), "classes": 0, "functions": 0, "constants": 0, "other_symbols": 0}
    for entry in files:
        counts["classes"] += len(entry.get("classes") or [])
        counts["functions"] += len(entry.get("functions") or [])
        counts["constants"] += len(entry.get("constants") or [])
        counts["other_symbols"] += len(entry.get("other_symbols") or [])
    return counts


def build_symbols_artifact(
    guarded_root: Path,
    *,
    run_id: str,
    reply: Dict[str, Any],
    completeness: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Shape the already-computed per-file `reply` into the schema_version-
    pinned artifact dict (schema_version first, per D6(v)).

    `reply` is the same `{"files": [...], "coverage_note"?: ...}` dict the
    non-emit path returns unmodified — this function does not recompute
    anything, only wraps it. `completeness` is the foreign-extraction
    completeness/coverage surface from `build_foreign_symbols` (carries
    `unmapped_kinds` when any non-Python symbol kind fell outside the
    class/function/constant/type_alias buckets), `None` when no non-Python
    file was requested. AC5: the completeness/coverage surface — per-file
    `other_symbols` (already embedded in each `reply["files"]` entry),
    `completeness["unmapped_kinds"]`, and the `symbol_extract`-absent
    `coverage_note` — all survive onto the written artifact.
    """
    artifact: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "target_root": str(guarded_root),
        "files": reply["files"],
        "counts": _count_entries(reply["files"]),
    }
    if "coverage_note" in reply:
        artifact["coverage_note"] = reply["coverage_note"]
    if completeness is not None:
        artifact["completeness"] = completeness
    return artifact


def write_symbols_artifact(target_root: Path, run_id: str, artifact: Dict[str, Any]) -> Path:
    """Write the symbols artifact to
    <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json,
    atomically (mkstemp + os.replace, DR-228 § D6(ii) create-or-full-rewrite
    only), mirroring cartography_chunk_table.write_chunk_table.

    Write-confined (D6(i)): only this run-id's own subdirectory under
    state/scratch/cartography-symbols/ is touched.
    """
    run_dir = target_root / "state" / "scratch" / "cartography-symbols" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "symbols.json"

    body = json.dumps(artifact, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return target

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
# project-rag"). `symbol_extract` ships from a private repo; a user without
# access must be able to install and use makima without hitting an
# exception. This state is handled separately below: it degrades to a
# distinct, unmistakably-"not attempted" in-band marker per requested file,
# plus a top-level coverage note — never raised, never folded into an
# empty-but-successful extraction.
_LOUD_COVERAGE_STATES = frozenset({"missing_grammar", "partial_coverage"})

# The remedy surfaced in both the per-file "unavailable" marker's `detail`
# and the top-level coverage note when `symbol_extract` cannot be imported.
_SYMBOL_EXTRACT_REMEDY = (
    "install the optional extra to enable non-Python symbol extraction: "
    'python -m pip install -e ".[symbols]" from the makima repo root '
    "(requires read access to the extra's source repo; the extra resolves "
    "a git-ref pin, and the underlying distribution is project-rag-symbol-extract, "
    "import name symbol_extract)"
)
# Naming the distribution as the pip target would be actively misleading:
# coordinator_core is not published, so `pip install project-rag-symbol-extract`
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
    emit = bool(params.get("emit", False))
    # run_id is required ONLY when emit is truthy, and this is a deliberate
    # divergence from cartography.chunk_table's unconditional run_id.
    #
    # Negative spec: do NOT "restore" the unconditional form for symmetry with
    # chunk_table. That op introduced run_id with no pre-existing callers to
    # break; this one has them, in two sibling repos, whose working invocation
    # passes exactly {target_root, files}. Requiring run_id on the
    # compute-only path breaks that call for every existing consumer and
    # contradicts this plan's own AC1 (the default path is unchanged) at the
    # param layer rather than the reply layer.
    #
    # Both run_id rejections raise `CallerFacingValidationError`, not a bare
    # `ValueError`: `ipc.py::_handler_exception_error` preserves an exception's
    # own message only when it carries the `caller_facing_validation` marker,
    # and reduces every unclassified `ValueError` to
    # `-32603 Internal error: ValueError`. A cross-repo caller who omits
    # `run_id` on an emitting call would otherwise get a generic envelope with
    # no indication of which param is wrong -- the precise failure that class
    # was introduced to close. It subclasses `ValueError`, so direct-call sites
    # and `pytest.raises(ValueError, ...)` are unaffected.
    run_id = params.get("run_id")
    if emit:
        if not isinstance(run_id, str) or not run_id:
            raise CallerFacingValidationError(
                "cartography.symbols requires a non-empty string param: run_id "
                "when emit is true"
            )
        if not safe_id(run_id):
            raise CallerFacingValidationError(
                f"cartography.symbols: run_id is not a safe path segment: {run_id!r}"
            )
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
    completeness: Optional[Dict[str, Any]] = None

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

            # AC5: captured here (not merely threaded through per-file
            # entries) so an emitting call's artifact carries the top-level
            # completeness/coverage surface (unmapped_kinds census) the
            # inline reply's per-file `other_symbols` entries only imply.
            completeness = foreign_result.get("completeness")
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

    if not emit:
        # AC1, AC7: no file opened, no disk write — reply stays exactly the
        # shape above, byte-identical to every pre-emit-param run.
        return reply

    # `run_id` is validated as a non-empty safe_id in the `if emit:` branch
    # above; rebind it as a `str` so the emit-only call sites below carry the
    # narrowed type rather than the `str | None` the params dict yields.
    emit_run_id: str = str(run_id)
    artifact = build_symbols_artifact(
        guarded_root,
        run_id=emit_run_id,
        reply=reply,
        completeness=completeness,
    )
    written_path = write_symbols_artifact(guarded_root, emit_run_id, artifact)
    result = {k: v for k, v in artifact.items() if k != "files"}
    result["symbols_path"] = written_path.resolve().relative_to(guarded_root).as_posix()
    return result
