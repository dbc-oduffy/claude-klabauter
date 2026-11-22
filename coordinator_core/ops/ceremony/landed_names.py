"""Landed-tree name checker: refuse a landing `.py` that names something only the worktree has.

One AST pass over the bytes a land would commit (`read_landed`), compared against the worktree, for
every intra-repo import (module-level and function-local), every `alias.attr` Load and every
two-argument `getattr(alias, "<literal>")` rooted at such an import. Pure Python, no subprocess.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

_Bindings = Tuple[Set[str], bool]


class _Checker:
    def __init__(self, root: Path, read_landed: Callable[[str], Optional[bytes]]) -> None:
        # Lazy: commit_gates imports this module's gate wrapper, so a top-level import would cycle.
        from coordinator_core.ops.ceremony import commit_gates

        self._gates = commit_gates
        self._root = root
        self._read_landed = read_landed
        self._landed: Dict[str, Optional[bytes]] = {}
        self._landed_b: Dict[str, _Bindings] = {}
        self._wt_file: Dict[Tuple[str, ...], Optional[str]] = {}
        self._wt_b: Dict[str, _Bindings] = {}

    def landed(self, rel: str) -> Optional[bytes]:
        if rel not in self._landed:
            self._landed[rel] = self._read_landed(rel)
        return self._landed[rel]

    def landed_file(self, parts: Sequence[str]) -> Optional[str]:
        if not parts:
            return None
        base = "/".join(parts)
        for rel in (base + ".py", base + "/__init__.py"):
            if self.landed(rel) is not None:
                return rel
        return None

    def wt_file(self, parts: Sequence[str]) -> Optional[str]:
        key = tuple(parts)
        if key not in self._wt_file:
            found = None
            if parts:
                base = "/".join(parts)
                for rel in (base + ".py", base + "/__init__.py"):
                    if (self._root / rel).is_file():
                        found = rel
                        break
            self._wt_file[key] = found
        return self._wt_file[key]

    def _bindings(self, data: bytes) -> _Bindings:
        try:
            return self._gates._module_bindings(ast.parse(data))
        except (SyntaxError, ValueError, RecursionError):
            return (set(), True)

    def landed_bindings(self, rel: str) -> _Bindings:
        if rel not in self._landed_b:
            self._landed_b[rel] = self._bindings(self.landed(rel) or b"")
        return self._landed_b[rel]

    def wt_bindings(self, rel: str) -> _Bindings:
        if rel not in self._wt_b:
            try:
                data = (self._root / rel).read_bytes()
            except OSError:
                data = b""
            self._wt_b[rel] = self._bindings(data)
        return self._wt_b[rel]

    def top_landed(self, top: str) -> bool:
        return self.landed(top + "/__init__.py") is not None or self.landed(top + ".py") is not None

    def resolve(self, parts: Sequence[str], name: str) -> Tuple[str, List[str]]:
        """(kind, parts+[name]); kind is 'module' (landed submodule), 'refuse' or 'ok'."""
        full = list(parts) + [name]
        if self.landed_file(full) is not None:
            return "module", full
        module = self.landed_file(parts)
        worktree = self.wt_file(parts)
        if module is None:
            return ("refuse" if worktree is not None else "ok"), full
        names, is_open = self.landed_bindings(module)
        if is_open or name in names:
            return "ok", full
        if self.wt_file(list(parts) + [name]) is not None:
            return "refuse", full
        if worktree is not None:
            w_names, w_open = self.wt_bindings(worktree)
            if not w_open and name in w_names:
                return "refuse", full
        return "ok", full

    def check_file(self, rel: str, out: List[str]) -> None:
        data = self.landed(rel)
        if data is None:
            return
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError, RecursionError):
            return
        pkg = rel.split("/")[:-1]
        bound: Dict[str, Set[Tuple[str, ...]]] = {}

        def report(lineno: int, shown: str) -> None:
            msg = f"{rel}:{lineno}: names {shown}, which exists only uncommitted"
            if msg not in out:
                out.append(msg)

        def walk_chain(lineno: int, start: Sequence[str], attrs: Sequence[str]) -> Optional[List[str]]:
            cur = list(start)
            for attr in attrs:
                kind, value = self.resolve(cur, attr)
                if kind == "refuse":
                    report(lineno, ".".join(value))
                    return None
                if kind != "module":
                    return None
                cur = value
            return cur

        for node in self._gates._unguarded_imports(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if not self.top_landed(parts[0]):
                        continue
                    full = walk_chain(node.lineno, parts[:1], parts[1:])
                    if alias.asname:
                        if full is not None:
                            bound.setdefault(alias.asname, set()).add(tuple(full))
                    else:
                        bound.setdefault(parts[0], set()).add((parts[0],))
                continue
            assert isinstance(node, ast.ImportFrom)
            if node.level:
                keep = len(pkg) - (node.level - 1)
                if keep < 0:
                    continue
                base = pkg[:keep] + (node.module.split(".") if node.module else [])
            else:
                base = node.module.split(".") if node.module else []
                if not base or not self.top_landed(base[0]):
                    continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                kind, value = self.resolve(base, alias.name)
                if kind == "refuse":
                    report(node.lineno, ".".join(value))
                elif kind == "module":
                    bound.setdefault(alias.asname or alias.name, set()).add(tuple(value))

        roots = {name: next(iter(targets)) for name, targets in bound.items() if len(targets) == 1}
        if not roots:
            return

        def chain(node: ast.AST) -> Optional[Tuple[str, List[str]]]:
            attrs: List[str] = []
            while isinstance(node, ast.Attribute):
                attrs.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name) and node.id in roots:
                return node.id, attrs[::-1]
            return None

        class Visitor(ast.NodeVisitor):
            def visit_Attribute(self_v, node: ast.Attribute) -> None:  # noqa: N802
                found = chain(node) if isinstance(node.ctx, ast.Load) else None
                if found is None:
                    self_v.generic_visit(node)
                    return
                walk_chain(node.lineno, roots[found[0]], found[1])

            def visit_Call(self_v, node: ast.Call) -> None:  # noqa: N802
                fn = node.func
                if (
                    isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) == 2 and not node.keywords
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                ):
                    target = node.args[0]
                    if isinstance(target, ast.Name) and target.id in roots:
                        walk_chain(node.lineno, roots[target.id], [node.args[1].value])
                    else:
                        found = chain(target) if isinstance(target, ast.Attribute) else None
                        if found is not None:
                            walk_chain(node.lineno, roots[found[0]], found[1] + [node.args[1].value])
                self_v.generic_visit(node)

        Visitor().visit(tree)


def uncommitted_name_refusals(
    worktree_root: Path,
    paths: Sequence[str],
    read_landed: Callable[[str], Optional[bytes]],
) -> Optional[list[str]]:
    """Refusal lines for landing `.py` paths naming a module or name that exists only in the worktree.

    Each line is `<path>:<line>: names <mod>.<name>, which exists only uncommitted`. `[]` when clean;
    `None` when the check could not decide (the caller records gate_unverified, DR-402). A name
    absent from both trees, an open module (star import, `__getattr__`), a non-literal `getattr`
    and an `except ImportError`-guarded import all pass.
    """
    try:
        checker = _Checker(Path(worktree_root), read_landed)
        out: List[str] = []
        for rel in paths:
            if rel.endswith(".py"):
                checker.check_file(rel, out)
        return out
    except Exception:  # noqa: BLE001 -- a guard that cannot run allows and says so
        return None
