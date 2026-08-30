"""
coordinator_core.install.write_surface_discovery — AST-scan discovery of every
module that declares a `WRITE_SURFACE` (`coordinator_core.install.write_surface.
WriteSurfaceDeclaration`), plus the flattening of one declaration into entry dicts.

Purpose: give `maximalist._build_and_persist_receipt` (and the uninstall-judgment
page guard) one roster of writer declarations walked FROM the declarations
themselves, never hand-transcribed. A hand-maintained roster of writers rots: the
prior `_WRITER_MODULE_SITES` tuple went stale by 13 entries within hours. This
module discovers declaring modules from the tree instead of naming them.

Two-phase discovery:

  1. CHEAP TEXT/AST SCAN (`_declares_write_surface`) over every top-level `.py`
     file under `_SCAN_ROOTS` — no import, just `ast.parse` + a check for a
     module-level `WRITE_SURFACE` binding. Costs nothing proportional to the
     number of modules that do NOT declare.
  2. IMPORT ONLY THE CANDIDATES that scan flags, so the import cost is
     proportional to declaration count, not tree size.

`_SCAN_ROOTS` is hand-maintained — but of ROOT DIRECTORIES to walk, not writer
identities. A stale/missing root just means a writer under an unlisted directory
is invisible until the root list is extended, the same failure mode
`test_write_reaching_modules_declare.py`'s own `_INSTALL_DIR` constant accepts for
its narrower one-directory walk. Deriving roots structurally from the op registry
was considered and rejected: that map enumerates op modules for dispatch routing,
not writer-bearing directories — `coordinator/bin` and `coordinator/scripts` carry
declaring modules and are never op-registry members at all.

Non-identifier filenames (e.g. `coordinator/bin/seed-marketplace-enabledplugins.py`,
hyphenated, outside any package) generalize rather than special-case:
`_load_candidate` always tries `importlib.import_module` on the file's dotted module
path first and falls back to `importlib.util.spec_from_file_location` +
`spec.loader.exec_module` on `ImportError`/`ModuleNotFoundError` — covering a
non-identifier filename AND a directory with no `__init__.py` (`coordinator/bin`,
`coordinator/scripts` have neither) uniformly.

Validation is per the frozen protocol's own escape hatch: `write_surface.validate()`
returns structured `ValidationError`s rather than raising, precisely so ONE writer's
bad declaration becomes a per-entry problem, never a dead flattening — this module
honors that by still producing every entry from a bad declaration, each carrying its
own `"validation_errors"` list (empty when clean) rather than dropping the writer.

Deletions count: an entry from a `clause`/`entry` pair where either carries
`effect="delete"` is produced with `"effect": "delete"`, never filtered.

History: this discovery half was extracted from `ops/write_surface_manifest.py` when
`write_surface.emit_manifest` was gravestoned (kill ledger K-029 / K-103 — no live
consumer in either repo). The op's emission half — a JSON-RPC manifest for a DoE
lockstep test that was checked at DoE `042963e67` and found never to have existed —
went with it. Reviving the manifest requires a real consumer, not this module.

Negative-spec — this module does NOT:
    - author, edit, or hand-transcribe any writer's declared entries — every entry is
      read straight off that writer's own `WriteSurfaceDeclaration`;
    - hand-maintain a registry of writer identities — the only hand-maintained list
      left is `_SCAN_ROOTS`, a set of directories, not writers;
    - write anything to disk, or register a JSON-RPC op;
    - validate that a declared surface matches what a writer's source actually does at
      runtime;
    - filter out `effect="delete"` entries;
    - recurse into nested directories (e.g. a `tests/` subpackage) under a scan root —
      only that root's own top-level `.py` files are candidates.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from pathlib import Path
from typing import Any

from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    validate,
)

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
        "validation_errors": validation_errors,
    }
def _load_via_file_location(module_path: Path) -> Any:
    """Generic fallback loader for a candidate whose dotted import path either doesn't
    exist (non-identifier filename, e.g. a hyphenated script) or isn't importable
    (a directory with no `__init__.py`, e.g. `coordinator/bin`, `coordinator/scripts`)."""
    safe_stem = re.sub(r"[^0-9A-Za-z_]", "_", module_path.stem)
    spec = importlib.util.spec_from_file_location(
        f"coordinator._write_surface_discovery_{safe_stem}", module_path
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
                "validation_errors": [],
            }
        )

    return entries


def discover_declarations(
    repo_root: Path,
) -> tuple[dict[str, WriteSurfaceDeclaration], list[tuple[str, str]]]:
    """Public AST-scan discovery seam: `writer_id -> WriteSurfaceDeclaration`
    for every module the scan finds, so a caller outside this module never
    reaches for `_candidate_modules`/`_load_candidate` (private names) to
    rebuild the same roster. Sole production caller is
    `coordinator_core.install.maximalist._collect_writer_declarations`
    (receipt-coverage collection).

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
        ``writer_id`` collision, first-claim wins.
      - ``failures`` -- ``(source_hint, reason)`` pairs, one per candidate
        module that failed to import or whose `WRITE_SURFACE` was not a
        `WriteSurfaceDeclaration`. `source_hint` is the module's
        repo-root-relative path -- the only stable identity available,
        since the real `writer_id` (if any) was never obtained.
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
