"""Symbol-level needed-path check: which top-level names a run changed in a file versus HEAD, and which
landing ``.py`` files import or read those names. Pure AST plus in-process blob reads; no subprocess."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Optional, Sequence

from coordinator_core.authoring_leaks import import_closure as ic
from coordinator_core.pyimports import ImportRecord, collect_imports


def _top_level_dumps(tree: ast.Module) -> Dict[str, str]:
    """``{name: ast.dump}`` for each top-level def, class and assignment target; a name bound twice
    concatenates its dumps so either binding changing is seen."""
    out: Dict[str, str] = {}

    def bind(name: str, node: ast.AST) -> None:
        out[name] = out.get(name, "") + ast.dump(node)

    def targets(node: ast.AST):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                yield from targets(elt)
        elif isinstance(node, ast.Starred):
            yield from targets(node.value)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                for name in targets(tgt):
                    bind(name, node)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in targets(node.target):
                bind(name, node)
    return out


def _parse(data: Optional[bytes]) -> Optional[ast.Module]:
    if data is None:
        return None
    try:
        return ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError):
        return None


def _head_bytes(root: Path, rel: str) -> Optional[bytes]:
    """The file's blob at HEAD, None when HEAD lacks it or the object cannot be read."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
    from coordinator_core.git.since_base_overlap import _head_sha

    head = _head_sha(root)
    if not head:
        return None
    common = resolve_git_common_dir(root)
    blob = _blob_at_commit(common, head, rel)
    if blob is None or blob is ABSENT:
        return None
    obj = git_objects.read_object(common, blob)
    return None if obj is None else obj[1]


def changed_symbols(root: Path, rel: str) -> frozenset[str]:
    """Top-level def/class/assign names of ``rel`` whose ``ast.dump`` differs between HEAD and the
    worktree (new and removed names included). An unparseable HEAD side, or a file HEAD lacks, counts
    every worktree name as changed; an unparseable or missing worktree side yields no names."""
    try:
        wt_bytes: Optional[bytes] = (root / rel).read_bytes()
    except OSError:
        return frozenset()
    wt_tree = _parse(wt_bytes)
    if wt_tree is None:
        return frozenset()
    wt = _top_level_dumps(wt_tree)
    head_tree = _parse(_head_bytes(root, rel))
    if head_tree is None:
        return frozenset(wt)
    head = _top_level_dumps(head_tree)
    return frozenset(n for n in {*wt, *head} if wt.get(n) != head.get(n))


def _chain(node: ast.AST) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _module_bindings(tree: ast.Module, rel: str) -> Dict[str, str]:
    """``{local dotted name: imported module dotted name}`` for the module-valued bindings a ``name.attr``
    read can go through: ``import a.b``, ``import a.b as x`` and ``from p import m [as x]``."""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = ic._absolute_base(ImportRecord(node.module or "", (), node.level, node.lineno), rel)
            if not base:
                continue
            for alias in node.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = f"{base}.{alias.name}"
    return out


def needed_by(
    root: Path, landing: Sequence[str], omitted: Sequence[str],
) -> Dict[str, list[tuple[str, str]]]:
    """``{omitted .py: [(landing importer, name), ...]}``: the importer does ``from <mod> import name`` or reads
    ``<alias>.name`` on an import of ``<mod>`` where ``name`` is a top-level name the run changed in the
    omitted file. Omitted files with no such reader are absent from the result."""
    omit = {p for p in omitted if p.endswith(".py")}
    if not omit:
        return {}
    changed: Dict[str, frozenset] = {p: changed_symbols(root, p) for p in sorted(omit)}
    changed = {p: names for p, names in changed.items() if names}
    if not changed:
        return {}

    hits: Dict[str, set] = {}

    def file_of(module: str, pfx: Sequence[str]) -> Optional[str]:
        return next((f for f in ic.module_files(module, pfx) if f in changed), None)

    for rel in landing:
        if not rel.endswith(".py") or rel in omit:
            continue
        text = ic.read_text(root / rel)
        try:
            tree = ast.parse(text) if text else None
        except SyntaxError:
            tree = None
        if tree is None:
            continue
        pfx = ic.prefixes(rel)
        for rec in collect_imports(tree)[0]:
            base = ic._absolute_base(rec, rel)
            target = file_of(base, pfx) if base else None
            if target:
                for name in rec.names:
                    if name in changed[target]:
                        hits.setdefault(target, set()).add((rel, name))
        bindings = _module_bindings(tree, rel)
        if not bindings:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                module = bindings.get(_chain(node.value) or "")
                target = file_of(module, pfx) if module else None
                if target and node.attr in changed[target]:
                    hits.setdefault(target, set()).add((rel, node.attr))
    return {p: sorted(pairs) for p, pairs in sorted(hits.items())}
