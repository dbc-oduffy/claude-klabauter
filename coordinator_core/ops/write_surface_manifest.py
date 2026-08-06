"""
coordinator_core.ops.write_surface_manifest — JSON-RPC "write_surface.emit_manifest"
operation.

Purpose: the C4 emission op the writer-declared write-surface manifest plan exists to
produce — the single artifact a sibling repo (example-doctrine-repo) reads in their lockstep test, walked
FROM the writers' `WRITE_SURFACE` declarations (`coordinator_core.install.
write_surface.WriteSurfaceDeclaration`), never hand-transcribed (Anti-scope: "the manifest
is emitted *from* the declarations").

Self-registration: importing this module calls
register_op("write_surface.emit_manifest", ...) as a side-effect (same pattern as
ops/ping.py). coordinator_core.ops.__init__ imports it so the registration fires at
start_server()/eager-import time.

Collection strategy — DISCOVER declarations, never enumerate writers by hand:

  A prior version of this module hand-listed writer sites in a `_WRITER_MODULE_SITES`
  tuple. That roster went stale within hours of being written — 9 listed writers while 13
  more modules already exported `WRITE_SURFACE` on disk, invisible to the manifest. A
  hand-maintained roster of writers rots exactly like a hand-transcribed surface; this
  module now discovers declaring modules from the tree instead of naming them.

  Two-phase discovery keeps the cold-start property `_registry_map.py` exists to protect
  (~150-250ms/module import cost on Windows, paid by every dispatch since this op module
  is imported eagerly by `coordinator_core/ops/__init__.py`, not only a manifest-emission
  dispatch):

    1. CHEAP TEXT/AST SCAN (`_declares_write_surface`) over every top-level `.py` file
       under `_SCAN_ROOTS` below — no import, just `ast.parse` + a check for a
       module-level `WRITE_SURFACE` binding. This costs nothing proportional to the
       number of modules that do NOT declare.
    2. IMPORT ONLY THE CANDIDATES that scan flags, lazily INSIDE the handler
       (`asyncio.to_thread`-offloaded), so the import cost lands only on an actual
       `write_surface.emit_manifest` dispatch and is proportional to declaration count,
       not tree size.

  `_SCAN_ROOTS` is still a hand-maintained list — but of ROOT DIRECTORIES to walk, not
  writer identities. That is a categorically smaller and more honest kind of staleness: a
  stale/missing root just means a writer under an unlisted directory is invisible until
  the root list is extended, exactly the same failure mode `test_write_reaching_modules_
  declare.py`'s own `_INSTALL_DIR` constant already accepts for its narrower one-directory
  walk. Deriving roots structurally (e.g. from `coordinator_core/ops/_registry_map.py`'s
  own eager-import module list) was considered and rejected: that map enumerates op
  modules for dispatch routing, not writer-bearing directories — `coordinator/bin` and
  `coordinator/scripts` carry declaring modules and are never op-registry members at all.

  `undeclared` no longer means "a known writer_id failed to import" (nothing here
  precomputes writer_id — it stays unknown until a candidate module is actually imported).
  It now means "a module whose static scan matched `WRITE_SURFACE = ...` failed to import,
  or the imported object under that name is not a `WriteSurfaceDeclaration`" — the same
  observable failure, keyed by source path since no writer_id is available. A directory
  that scans to zero candidate files (e.g. a root that no longer exists) produces zero
  entries for it and is not itself an error — see the module's negative spec.

  Non-identifier filenames (e.g. `coordinator/bin/seed-marketplace-enabledplugins.py`,
  hyphenated, outside any package) generalize rather than special-case: `_load_candidate`
  always tries `importlib.import_module` on the file's dotted module path first and falls
  back to `importlib.util.spec_from_file_location` + `spec.loader.exec_module` on
  `ImportError`/`ModuleNotFoundError` — this covers a non-identifier filename AND a
  directory with no `__init__.py` (`coordinator/bin`, `coordinator/scripts` have neither)
  uniformly, without pre-computing "is this name a valid identifier" as a separate branch.

`undeclared` representation: a candidate module that fails to import, or whose module
lacks a `WRITE_SURFACE` attribute (or whose attribute is not a `WriteSurfaceDeclaration`),
contributes exactly ONE entry to the flat `entries` list with `"status": "undeclared"` and
a `"reason"` naming the failure — never a silent drop and never a separate side-channel the
rest of the manifest depends on a consumer knowing to check. This satisfies the plan's
requirement that undeclared be "representable ... not just detected." Loudness without
blocking: a per-module import/attribute failure never raises out of the handler — the
whole manifest still emits, with that one module's entry flagged.

Duplicate `writer_id`s: two declaring modules claiming the same `writer_id` are NOT
silently collapsed. The second (and any subsequent) module to claim an already-seen
`writer_id` still emits its own declared entries in full, plus one extra synthetic entry
tagged `"status": "duplicate_writer_id"` naming both source modules — loud, non-blocking,
same pattern as `undeclared`.

Validation is per the frozen protocol's own escape hatch: `write_surface.validate()`
returns structured `ValidationError`s rather than raising, precisely so ONE writer's bad
declaration becomes a per-entry problem, never a dead emission — this module honors that
by still emitting every entry from a bad declaration, each carrying its own
`"validation_errors"` list (empty when clean) rather than dropping the writer.

Deletions count: an entry from a `clause`/`entry` pair where either carries
`effect="delete"` is emitted with `"effect": "delete"`, never filtered — example-doctrine-repo ruled
2026-08-06 that "every declared entry carries a named disposition" is the enforced
property, and a delete slots into that unchanged.

The `ShapedClause.discovered_by` wart: it is unconstrained free text (see
`write_surface.py` module docstring) — nothing stops two writers spelling the same
discovery mechanism differently, and this was deliberately left unfixed mid-declaration-
wave. This module does NOT change the protocol. It adds a normalized SIBLING field,
`discovered_by_normalized` (casefolded, whitespace-collapsed), alongside the raw
`discovered_by`, purely as an emission-time convenience for a consumer that wants
exact-match grouping without inheriting the free-text drift risk on the raw field.

Spec backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md, chunk C4

Negative-spec — this module does NOT:
    - author, edit, or hand-transcribe any writer's declared entries — every emitted
      declared entry is read straight off that writer's own `WriteSurfaceDeclaration`;
    - hand-maintain a registry of writer identities — see "Collection strategy" above;
      the only hand-maintained list left is `_SCAN_ROOTS`, a set of directories, not
      writers;
    - write anything to disk — the manifest is the JSON-RPC `result` payload only
      (COMPUTE_ONLY: reads writer modules' declarations and returns a computed value;
      no coordinator substrate is written);
    - validate that a declared surface matches what a writer's source actually does at
      runtime (a lockstep/drift check example-doctrine-repo owns on their side);
    - filter out `effect="delete"` entries, or drop an undeclared/duplicate module from
      the output;
    - recurse into nested directories (e.g. a `tests/` subpackage) under a scan root —
      only that root's own top-level `.py` files are candidates.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import time
from pathlib import Path
from typing import Any

from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    validate,
)
from coordinator_core.ipc import register_op

# Directories to scan for candidate WRITE_SURFACE-declaring modules, repo-root-relative.
# Hand-maintained ROOT list (see module docstring) — not a registry of writer identities.
# A stale/missing root degrades gracefully: a writer under an unlisted directory is
# invisible until the root is added, never wrong about a writer that IS scanned.
_SCAN_ROOTS: tuple[str, ...] = (
    "coordinator_core/install",
    "coordinator_core/ops",
    "coordinator/bin",
    "coordinator/scripts",
)


def _module_level_nodes(tree: ast.Module):
    # Review: code-reviewer — a module-level `WRITE_SURFACE` nested inside an `if`/`try`
    # (platform branch, import-fallback) is a child of that node, not of `tree.body`, so a
    # `tree.body`-only walk misses it. Walk the whole tree but stop descending into
    # `FunctionDef`/`AsyncFunctionDef`/`ClassDef` subtrees — a `WRITE_SURFACE` bound inside
    # a function or class body is not a module-level declaration and matching it would be a
    # false positive on the protocol's reserved name, not a safe fail-open.
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _declares_write_surface(module_path: Path) -> bool:
    """Cheap static check: does this module bind the bare name `WRITE_SURFACE` anywhere at
    module level — including nested inside a module-level `if`/`try`/`else` (e.g. a
    platform branch or an import-fallback pattern)? No import — mirrors
    `test_write_reaching_modules_declare.py`'s own `_declares_write_surface`, which this
    module's docstring cites as the AST-walk precedent for finding write-reaching modules
    without a hand list. Does NOT match a `WRITE_SURFACE` bound inside a function or class
    body — see `_module_level_nodes`."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in _module_level_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "WRITE_SURFACE":
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "WRITE_SURFACE":
                return True
    return False


def _candidate_modules(repo_root: Path) -> list[Path]:
    """Every top-level `.py` file under `_SCAN_ROOTS` whose static scan matched a
    module-level `WRITE_SURFACE` binding — `__init__.py` and `test_*.py` excluded, same
    exclusions as `test_write_reaching_modules_declare.py`'s `_install_modules`."""
    candidates: list[Path] = []
    for root_rel in _SCAN_ROOTS:
        root = repo_root / root_rel
        if not root.is_dir():
            continue
        for module_path in sorted(root.glob("*.py")):
            if module_path.name == "__init__.py" or module_path.name.startswith("test_"):
                continue
            if _declares_write_surface(module_path):
                candidates.append(module_path)
    return candidates


def _dotted_module_path(repo_root: Path, module_path: Path) -> str | None:
    """The dotted import path for `module_path`, or None if any path segment is not a
    valid Python identifier (e.g. the hyphenated `seed-marketplace-enabledplugins.py`)."""
    rel = module_path.relative_to(repo_root).with_suffix("")
    parts = rel.parts
    if not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _repo_root_fallback() -> Path:
    """Best-effort repo root when `repo_root` is not injected (this op is
    "none"-keyed; `register_op`'s contract allows `repo_root=None` for such ops).
    Three parents up from this file: ops/ -> coordinator_core/ -> repo root."""
    return Path(__file__).resolve().parents[2]


def _normalize_discovered_by(raw: str) -> str:
    return " ".join(raw.casefold().split())


def _entry_to_dict(
    *,
    writer_id: str,
    source_module: str,
    form: str,
    kind: str,
    key: str | None,
    path: str | None,
    begin_marker: str | None,
    end_marker: str | None,
    effect: str,
    reason: str | None,
    discovered_by: str | None,
    validation_errors: list[str],
) -> dict[str, Any]:
    return {
        "status": "declared",
        "writer_id": writer_id,
        "source_module": source_module,
        "form": form,
        "kind": kind,
        "key": key,
        "path": path,
        "begin_marker": begin_marker,
        "end_marker": end_marker,
        "effect": effect,
        "reason": reason,
        "discovered_by": discovered_by,
        "discovered_by_normalized": (
            _normalize_discovered_by(discovered_by) if discovered_by else None
        ),
        "validation_errors": validation_errors,
    }


def _undeclared_entry(source_hint: str, reason: str) -> dict[str, Any]:
    # `writer_id` is unknown at this point — the real writer_id lives on the
    # `WriteSurfaceDeclaration` this entry failed to obtain, so the discovered source
    # path is the only stable identity available. Consumers keying on `writer_id` should
    # treat `status == "undeclared"` rows as keyed by `source_module` instead.
    return {
        "status": "undeclared",
        "writer_id": source_hint,
        "source_module": source_hint,
        "form": None,
        "kind": None,
        "key": None,
        "path": None,
        "begin_marker": None,
        "end_marker": None,
        "effect": None,
        "reason": reason,
        "discovered_by": None,
        "discovered_by_normalized": None,
        "validation_errors": [],
    }


def _duplicate_writer_id_entry(writer_id: str, source_module: str, first_source: str) -> dict[str, Any]:
    return {
        "status": "duplicate_writer_id",
        "writer_id": writer_id,
        "source_module": source_module,
        "form": None,
        "kind": None,
        "key": None,
        "path": None,
        "begin_marker": None,
        "end_marker": None,
        "effect": None,
        "reason": f"writer_id {writer_id!r} already used by {first_source!r}",
        "discovered_by": None,
        "discovered_by_normalized": None,
        "validation_errors": [],
    }


def _load_via_file_location(module_path: Path) -> Any:
    """Generic fallback loader for a candidate whose dotted import path either doesn't
    exist (non-identifier filename, e.g. a hyphenated script) or isn't importable
    (a directory with no `__init__.py`, e.g. `coordinator/bin`, `coordinator/scripts`)."""
    safe_stem = re.sub(r"[^0-9A-Za-z_]", "_", module_path.stem)
    spec = importlib.util.spec_from_file_location(
        f"coordinator._write_surface_manifest_{safe_stem}", module_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build an import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_candidate(repo_root: Path, module_path: Path) -> Any:
    """Load a discovered candidate module. Tries a normal dotted import first (cheap,
    reuses `sys.modules` if some other op already imported it); falls back to
    file-location loading on `ImportError` — covers both a non-identifier filename and a
    non-package directory uniformly, per the module docstring."""
    dotted = _dotted_module_path(repo_root, module_path)
    if dotted is not None:
        try:
            return importlib.import_module(dotted)
        except ImportError:
            pass
    return _load_via_file_location(module_path)


def _errors_for_entry(
    all_errors: tuple[Any, ...], clause_index: int, entry_index: int | None
) -> list[str]:
    return [
        e.message
        for e in all_errors
        if e.clause_index == clause_index and e.entry_index == entry_index
    ]


def _flatten_declaration(decl: WriteSurfaceDeclaration) -> list[dict[str, Any]]:
    errors = validate(decl)
    entries: list[dict[str, Any]] = []

    for clause_index, clause in enumerate(decl.clauses):
        if isinstance(clause, StaticClause):
            for entry_index, entry in enumerate(clause.entries):
                effect = "delete" if "delete" in (clause.effect, entry.effect) else "write"
                entries.append(
                    _entry_to_dict(
                        writer_id=decl.writer_id,
                        source_module=decl.source_module,
                        form="static",
                        kind=entry.kind,
                        key=entry.key,
                        path=entry.path,
                        begin_marker=entry.begin_marker,
                        end_marker=entry.end_marker,
                        effect=effect,
                        reason=entry.reason,
                        discovered_by=None,
                        validation_errors=_errors_for_entry(errors, clause_index, entry_index),
                    )
                )
        elif isinstance(clause, ShapedClause):
            entry = clause.entry_template
            effect = "delete" if "delete" in (clause.effect, entry.effect) else "write"
            entries.append(
                _entry_to_dict(
                    writer_id=decl.writer_id,
                    source_module=decl.source_module,
                    form="shaped",
                    kind=entry.kind,
                    key=entry.key,
                    path=entry.path,
                    begin_marker=entry.begin_marker,
                    end_marker=entry.end_marker,
                    effect=effect,
                    reason=entry.reason,
                    discovered_by=clause.discovered_by,
                    validation_errors=_errors_for_entry(errors, clause_index, None),
                )
            )
        else:  # pragma: no cover — validate() already flags this shape defensively
            entries.append(
                _entry_to_dict(
                    writer_id=decl.writer_id,
                    source_module=decl.source_module,
                    form="unknown",
                    kind="unknown",
                    key=None,
                    path=None,
                    begin_marker=None,
                    end_marker=None,
                    effect="write",
                    reason=None,
                    discovered_by=None,
                    validation_errors=_errors_for_entry(errors, clause_index, None),
                )
            )

    if not decl.clauses:
        # DECLARED-EMPTY (write_surface.py): an explicit `clauses=()` is a claim the
        # writer writes nothing, not a gap — represent it, don't emit zero rows silently.
        entries.append(
            {
                "status": "declared-empty",
                "writer_id": decl.writer_id,
                "source_module": decl.source_module,
                "form": None,
                "kind": None,
                "key": None,
                "path": None,
                "begin_marker": None,
                "end_marker": None,
                "effect": None,
                "reason": None,
                "discovered_by": None,
                "discovered_by_normalized": None,
                "validation_errors": [],
            }
        )

    return entries


def discover_declarations(
    repo_root: Path,
) -> tuple[dict[str, WriteSurfaceDeclaration], list[tuple[str, str]]]:
    """Public AST-scan discovery seam, shared by this module's own manifest
    emission (indirectly, via the same `_candidate_modules`/`_load_candidate`
    primitives `_collect_manifest_entries` below also calls) and by
    `coordinator_core.install.maximalist._collect_writer_declarations`
    (receipt-coverage collection) -- so a caller outside this module no
    longer needs to reach for `_candidate_modules`/`_load_candidate`
    directly (private names) to get the same roster.

    Review: code-reviewer (P2/P3, 2026-08-06 install-receipt-persistence
    slice) -- `maximalist.py` previously imported the two private names
    above and, on an import failure, silently `continue`d with zero
    logging, degrading a failed-to-import writer's receipt coverage to
    "never asked about" -- indistinguishable from a writer that was never
    part of the install. This function does not itself decide loudness (see
    Returns below); it guarantees no failure is UNOBSERVABLE to a caller
    that checks `failures`.

    Returns ``(declarations, failures)``:
      - ``declarations`` -- ``writer_id -> WriteSurfaceDeclaration``. On a
        ``writer_id`` collision, first-claim wins (matches
        `_collect_manifest_entries`'s own first-claim precedent below).
      - ``failures`` -- ``(source_hint, reason)`` pairs, one per candidate
        module that failed to import or whose `WRITE_SURFACE` was not a
        `WriteSurfaceDeclaration`. `source_hint` is the module's
        repo-root-relative path -- the only stable identity available,
        since the real `writer_id` (if any) was never obtained.

    Deliberately does NOT touch `_collect_manifest_entries`/`build_manifest`
    below -- that function keeps its own independent scan loop so the
    already-frozen, externally-consumed manifest emission shape (example-doctrine-repo's
    cockpit schema) stays provably byte-for-byte unchanged by this seam's
    addition. Both scans call the identical `_candidate_modules`/
    `_load_candidate` primitives, so they can never disagree on WHICH
    modules are candidates or HOW one is loaded -- only on what each caller
    does with a failure, which is exactly the seam this function exists to
    let each caller own without re-deriving the scan itself.
    """
    declarations: dict[str, WriteSurfaceDeclaration] = {}
    failures: list[tuple[str, str]] = []
    for module_path in _candidate_modules(repo_root):
        rel = str(module_path.relative_to(repo_root))
        try:
            module = _load_candidate(repo_root, module_path)
            decl = getattr(module, "WRITE_SURFACE")
        except Exception as exc:  # noqa: BLE001 -- discovery-only, never fatal here
            failures.append((rel, f"{type(exc).__name__}: {exc}"))
            continue
        if not isinstance(decl, WriteSurfaceDeclaration):
            failures.append(
                (rel, f"WRITE_SURFACE present but not a WriteSurfaceDeclaration ({type(decl)!r})")
            )
            continue
        declarations.setdefault(decl.writer_id, decl)
    return declarations, failures


def _collect_manifest_entries(repo_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_writer_ids: dict[str, str] = {}  # writer_id -> first source_module claiming it

    for module_path in _candidate_modules(repo_root):
        rel = str(module_path.relative_to(repo_root))
        try:
            module = _load_candidate(repo_root, module_path)
            decl = getattr(module, "WRITE_SURFACE")
        except Exception as exc:  # noqa: BLE001 — loudness without blocking (see docstring)
            entries.append(_undeclared_entry(rel, f"{type(exc).__name__}: {exc}"))
            continue
        if not isinstance(decl, WriteSurfaceDeclaration):
            entries.append(
                _undeclared_entry(
                    rel,
                    f"WRITE_SURFACE present but not a WriteSurfaceDeclaration ({type(decl)!r})",
                )
            )
            continue

        if decl.writer_id in seen_writer_ids:
            entries.append(
                _duplicate_writer_id_entry(decl.writer_id, rel, seen_writer_ids[decl.writer_id])
            )
        else:
            seen_writer_ids[decl.writer_id] = rel
        entries.extend(_flatten_declaration(decl))

    return entries


def build_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    """Build the write-surface manifest dict. Exposed as a plain function (not only the
    JSON-RPC handler) so a caller with a resolved repo root already in hand can build the
    manifest without going through dispatch — used by this module's own test."""
    root = repo_root if repo_root is not None else _repo_root_fallback()
    entries = _collect_manifest_entries(root)
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "entries": entries,
    }


@register_op("write_surface.emit_manifest")
async def _emit_write_surface_manifest(params: dict, repo_root: Path | None = None) -> dict:
    """JSON-RPC "write_surface.emit_manifest" handler.

    Params: none consumed.

    Returns the manifest dict built by `build_manifest` — `schema_version`, `generated_at`,
    and a flat `entries` list mixing `status: declared` / `declared-empty` / `undeclared` /
    `duplicate_writer_id` rows for every module discovered under `_SCAN_ROOTS` that
    statically declares `WRITE_SURFACE` (see module docstring for the collection strategy
    and the `undeclared`/`duplicate_writer_id` representations).
    """
    return build_manifest(repo_root)
