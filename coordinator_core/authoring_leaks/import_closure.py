"""import_closure detector: a committed .py may not import what HEAD will lack after the commit.

Post-commit presence of a path = it is a gate path present in the worktree, or it is in HEAD and
not a gate path absent from the worktree (a deletion in the commit). First-party roots are
top-level directories holding `__init__.py` post-commit or in the worktree; everything else is
skipped. A target is parsed only when it is in the commit, or when the worktree differs from HEAD
(then HEAD's bytes are what the commit leaves behind); an unchanged target is assumed to satisfy
its names. A committed file is parsed only when an added line looks like an import, or reads
`alias.name` through an import alias of a first-party module that is in the commit or differs from
HEAD, so an ordinary commit pays no `ast.parse`. That attribute arm refuses a read whose name only
the uncommitted worktree target binds (a caller landing without its callee); a name bound nowhere is
not a landing hazard and is not reported. A syntax error in a committed file is the author's to fix:
no finding.

Zero spawns. Wholly absent targets (in neither HEAD nor the worktree) are not findings: generated
or compiled modules would false-refuse.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from coordinator_core.authoring_leaks import HeadView, LeakFinding, added_lines
from coordinator_core.authoring_leaks.module_attrs import AttrRef, attribute_refs, module_aliases
from coordinator_core.pyimports import ImportRecord, collect_imports, module_name_for_file, resolve_relative_module

NAME = "import_closure"
KIND = "import_closure"

# Import statements, plus bare `name,` and `)` lines that edit a parenthesised import.
_IMPORT_LINE = re.compile(r"^\s*(from\s+\S+\s+import\b|import\s+\S|[A-Za-z_][\w.]*\s*(?:as\s+\w+\s*)?,\s*(?:#.*)?$|\)\s*(?:#.*)?$)")


def candidates(gate_paths: Sequence[str]) -> Iterable[str]:
    """Gate `.py` paths: their HEAD text feeds the added-line prefilter."""
    return [p for p in gate_paths if p.endswith(".py")]


def _module_files(module: str) -> List[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _target_bound_names(text: str) -> Optional[Set[str]]:
    """Module-level bound names of `text`; `None` when it star-imports, defines `__getattr__`, or does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names: Set[str] = set()
    stack: List[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                return None
            names.add(node.name)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    return None
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            targets: List[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
                targets = [node.target]
            else:
                targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
            for t in targets:
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            if isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                stack.extend(node.body)
                stack.extend(getattr(node, "orelse", []))
        elif isinstance(node, ast.If):
            stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.While):
            stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.Try):
            stack.extend(node.body)
            stack.extend(node.orelse)
            stack.extend(node.finalbody)
            for h in node.handlers:
                stack.extend(h.body)
    return names


def _absolute_base(rec: ImportRecord, rel: str) -> Optional[str]:
    if rec.level == 0:
        return rec.module or None
    is_pkg = rel.rsplit("/", 1)[-1] == "__init__.py"
    return resolve_relative_module(rec.module, rec.level, module_name_for_file(rel), is_pkg)


def _needed_paths(records: Iterable[Tuple[str, ImportRecord]]) -> Set[str]:
    needed: Set[str] = set()
    for rel, rec in records:
        base = _absolute_base(rec, rel)
        if not base:
            continue
        needed.add(f"{base.split('.')[0]}/__init__.py")
        needed.update(_module_files(base))
        for name in rec.names:
            if name != "*":
                needed.update(_module_files(f"{base}.{name}"))
    return needed


class _Closure:
    def __init__(self, root: Path, gate: Set[str], view: HeadView) -> None:
        self.root = root
        self.gate = gate
        self.view = view

    def worktree_has(self, path: str) -> bool:
        return (self.root / path).is_file()

    def post_has(self, path: str) -> bool:
        if path in self.gate:
            return self.worktree_has(path)
        return self.view.in_head(path)

    def locate(self, module: str, has) -> Optional[str]:
        for cand in _module_files(module):
            if has(cand):
                return cand
        return None

    def first_party(self, top: str) -> bool:
        init = f"{top}/__init__.py"
        return self.post_has(init) or self.worktree_has(init)

    def post_names(self, target: str, force: bool = False) -> Optional[Set[str]]:
        """Names `target` binds once the commit lands; `None` = assume satisfied (or unparseable)."""
        if target in self.gate:
            text = _read_text(self.root / target)
        elif force or self.view.worktree_differs(target):
            text = self.view.head_text(target)
        else:
            return None
        return None if text is None else _target_bound_names(text)

    def check(self, rel: str, rec: ImportRecord) -> List[LeakFinding]:
        base = _absolute_base(rec, rel)
        if not base or not self.first_party(base.split(".")[0]):
            return []
        post_file = self.locate(base, self.post_has)
        if post_file is None:
            wt_file = self.locate(base, self.worktree_has)
            head_file = self.locate(base, self.view.in_head)
            if wt_file and not head_file:
                return [self._finding(rel, rec, base, base, wt_file, "uncommitted in the worktree")]
            if head_file:
                return [self._finding(rel, rec, base, base, head_file, "deleted by this commit")]
            return []
        out: List[LeakFinding] = []
        post_names: Optional[Set[str]] = None
        loaded = False
        is_package = post_file.endswith("/__init__.py")
        for name in rec.names:
            if name == "*":
                continue
            sub = f"{base}.{name}"
            if is_package and self.locate(sub, self.post_has):
                continue
            wt_sub = is_package and self.locate(sub, self.worktree_has)
            if wt_sub:
                # A submodule only the worktree has: the package text decides, even if unchanged.
                names = self.post_names(post_file, force=True)
            else:
                if not loaded:
                    post_names, loaded = self.post_names(post_file), True
                names = post_names
            if names is None or name in names:
                continue
            wt_bound = False
            if post_file in self.gate:
                wt_bound = False
            else:
                wt_text = _read_text(self.root / post_file)
                wt_names = _target_bound_names(wt_text) if wt_text is not None else None
                wt_bound = wt_names is None or name in wt_names
            reason = "uncommitted in the worktree" if (wt_sub or wt_bound) else "absent from HEAD"
            out.append(self._finding(rel, rec, name, base, wt_sub or post_file, reason))
        return out

    @staticmethod
    def _finding(rel: str, rec: ImportRecord, name: str, module: str, target: str, reason: str) -> LeakFinding:
        return LeakFinding(rel, rec.lineno, KIND, f"imports {name} from {module}; {target} is {reason}")


def _alias_hits(wt_text: str, rel: str, added: List[Tuple[int, str]]) -> Dict[str, frozenset]:
    """Aliases (and their candidate modules) that an added line reads as `alias.name`."""
    if not added:
        return {}
    aliases = module_aliases(wt_text, rel)
    if not aliases:
        return {}
    pattern = re.compile(r"\b(?:%s)\.\w" % "|".join(re.escape(a) for a in aliases))
    if not any(pattern.search(text) for _, text in added):
        return {}
    return aliases


def _check_attr(closure: _Closure, rel: str, ref: AttrRef) -> Optional[LeakFinding]:
    top = ref.module.split(".")[0]
    if not closure.first_party(top):
        return None
    target = closure.locate(ref.module, closure.post_has)
    if target is None or target in closure.gate:
        return None
    sub = f"{ref.module}.{ref.name}"
    is_package = target.endswith("/__init__.py")
    if is_package and closure.locate(sub, closure.post_has):
        return None
    wt_sub = bool(is_package and closure.locate(sub, closure.worktree_has))
    names = closure.post_names(target, force=wt_sub)
    if names is None or ref.name in names:
        return None
    if not wt_sub:
        wt_text = _read_text(closure.root / target)
        wt_names = _target_bound_names(wt_text) if wt_text is not None else None
        if wt_names is None or ref.name not in wt_names:
            return None
    return LeakFinding(rel, ref.lineno, KIND, f"references {ref.name} from {ref.module}; {target} is uncommitted in the worktree")


def detect(root: Path, gate_paths: Sequence[str], head: HeadView) -> List[LeakFinding]:
    root = Path(root)
    gate = set(gate_paths)
    texts: Dict[str, str] = {}
    import_files: Set[str] = set()
    hits: Dict[str, Dict[str, frozenset]] = {}
    for rel in sorted(p for p in gate if p.endswith(".py")):
        wt_text = _read_text(root / rel)
        if wt_text is None:
            continue
        added = added_lines(head.head_text(rel), wt_text)
        texts[rel] = wt_text
        if any(_IMPORT_LINE.match(text) for _, text in added):
            import_files.add(rel)
        alias_map = _alias_hits(wt_text, rel, added)
        if alias_map:
            hits[rel] = alias_map
    probe: Set[str] = set()
    for alias_map in hits.values():
        for mods in alias_map.values():
            for mod in mods:
                probe.update(f for f in _module_files(mod) if (root / f).is_file())
    probe_view = HeadView(root, probe) if probe else None
    attr_files: Set[str] = set()
    if probe_view is not None:
        for rel, alias_map in hits.items():
            if any(f in gate or probe_view.worktree_differs(f) for mods in alias_map.values() for mod in mods for f in _module_files(mod) if f in probe):
                attr_files.add(rel)
    parsed: List[Tuple[str, ImportRecord]] = []
    refs: List[Tuple[str, AttrRef]] = []
    for rel in sorted(import_files | attr_files):
        try:
            tree = ast.parse(texts[rel])
        except (SyntaxError, ValueError):
            continue
        if rel in import_files:
            records, _ = collect_imports(tree)
            parsed.extend((rel, rec) for rec in records)
        if rel in attr_files:
            refs.extend((rel, ref) for ref in attribute_refs(tree, rel))
    if not parsed and not refs:
        return []
    needed = _needed_paths(parsed)
    for _, ref in refs:
        needed.add(f"{ref.module.split('.')[0]}/__init__.py")
        needed.update(_module_files(ref.module))
        needed.update(_module_files(f"{ref.module}.{ref.name}"))
    closure = _Closure(root, gate, HeadView(root, needed))
    findings: List[LeakFinding] = []
    for rel, rec in parsed:
        findings.extend(closure.check(rel, rec))
    for rel, ref in refs:
        finding = _check_attr(closure, rel, ref)
        if finding is not None:
            findings.append(finding)
    return findings
