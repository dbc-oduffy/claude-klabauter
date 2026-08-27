"""Cross-file transitive spawn-wrapper resolution, generic over the caller's
own spawn-classification scanner.

Hoisted out of `coordinator_core/tests/test_no_new_spawning_tests.py` (the
spawn ratchet), where this exact logic used to live duplicated and
un-vendorable: a byte-identical copy of `detect.py` carried none of it, and
nothing signalled the gap (see `detect.py`'s module docstring, corrected in
the same change that added this module). A refresh of `spawn_policy/` now
carries this layer; `detect.py` itself is untouched — its exported surface
(`SpawnKind`, `SpawnSite`, `SpawnParseError`, `ExcludedReport`, and its
existing entrypoints) is pinned (`tasks/shell-spawn-regrowth-gate/
PINNED-API.md`) and this module adds a sibling capability rather than
touching it.

WHAT THIS SOLVES
    A module-level `def _git(...): subprocess.run(...)` defined in file A,
    imported into file B and called there as `_git(...)`, is a real spawn
    site in B — but B's own AST contains no `subprocess`/`os` call at all.
    Resolving that requires walking B's imports, finding which import binds
    the call target, opening THAT file, and checking (transitively) whether
    it itself resolves to a spawn. `detect.py`'s own `_local_helpers` only
    covers same-file, one-hop indirection; this module is the cross-file,
    transitive case.

GENERIC OVER THE CALLER'S SPAWN SCANNER
    This module has no opinion on "what counts as a spawn call" — that
    policy (which (module, attr) pairs are tracked, argv0 evidencing rules,
    alias resolution) stays with the caller. `WrapperResolver` is
    constructed with a `build_scanner(relpath, tree) -> ScannerLike`
    callable; `ScannerLike` is any object exposing `func_spawns`,
    `module_level_spawns`, `generic_calls`, and `module_level_calls` in the
    shapes documented on `WrapperResolver.get_module_info`.

NOT A SINGLETON
    Each `WrapperResolver` owns its own module-info/wrapper caches — no
    module-level mutable cache here. A test-collection-scoped caller
    (the ratchet) constructs one instance for the run; a differently-scoped
    caller (a single-file lint invocation) gets isolated caches for free.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Callable, Protocol


class _ScannerLike(Protocol):
    func_spawns: dict[str, list]
    module_level_spawns: list
    generic_calls: dict[str, list[tuple]]
    module_level_calls: list[tuple[int, tuple]]


@dataclasses.dataclass
class ImportBinding:
    """`kind == "func"`: `orig_name` names a function defined in `module_path`.
    `kind == "module"`: the local name IS `module_path`, used via attribute
    access (`local_name.attr(...)`)."""

    kind: str
    module_path: Path
    orig_name: str | None = None


@dataclasses.dataclass
class ModuleInfo:
    path: Path
    scanner: "_ScannerLike"
    imports: dict[str, ImportBinding]
    top_level_funcs: set[str]


def _resolve_absolute_module(repo_root: Path, dotted: str) -> Path | None:
    base = repo_root.joinpath(*dotted.split("."))
    candidate_file = base.with_suffix(".py")
    if candidate_file.is_file():
        return candidate_file
    candidate_pkg = base / "__init__.py"
    if candidate_pkg.is_file():
        return candidate_pkg
    return None


def _package_dir(current_file: Path, level: int) -> Path:
    d = current_file.parent
    for _ in range(max(level - 1, 0)):
        d = d.parent
    return d


def _resolve_submodule(dir_path: Path, name: str) -> Path | None:
    candidate_file = dir_path / f"{name}.py"
    if candidate_file.is_file():
        return candidate_file
    candidate_pkg = dir_path / name / "__init__.py"
    if candidate_pkg.is_file():
        return candidate_pkg
    return None


def _resolve_relative_module(current_file: Path, module: str | None, level: int) -> Path | None:
    base = _package_dir(current_file, level)
    if module is None:
        return base
    parts = module.split(".")
    parent_dir = base.joinpath(*parts[:-1]) if len(parts) > 1 else base
    return _resolve_submodule(parent_dir, parts[-1])


def _rel(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root).as_posix()


def collect_imports(
    repo_root: Path, tree: ast.Module, current_file: Path, unresolved: set[str]
) -> dict[str, ImportBinding]:
    """Resolves every `import`/`from ... import` in `tree` to an on-disk
    module path where possible. An import that cannot be resolved on disk
    (external package, dynamic import, `importlib`) is recorded into
    `unresolved` and skipped — never guessed."""
    bindings: dict[str, ImportBinding] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base_or_module = _resolve_relative_module(current_file, node.module, node.level)
            else:
                base_or_module = (
                    _resolve_absolute_module(repo_root, node.module) if node.module else None
                )
            for alias in node.names:
                local = alias.asname or alias.name
                if base_or_module is None:
                    unresolved.add(
                        f"{_rel(repo_root, current_file)}: from "
                        f"{'.' * (node.level or 0)}{node.module or ''} import {alias.name}"
                    )
                    continue
                if node.module is None and node.level and node.level > 0:
                    submod = _resolve_submodule(base_or_module, alias.name)
                    if submod is None:
                        unresolved.add(
                            f"{_rel(repo_root, current_file)}: from "
                            f"{'.' * node.level} import {alias.name}"
                        )
                        continue
                    bindings[local] = ImportBinding("module", submod)
                else:
                    bindings[local] = ImportBinding("func", base_or_module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                modfile = _resolve_absolute_module(repo_root, alias.name)
                if modfile is None:
                    unresolved.add(f"{_rel(repo_root, current_file)}: import {alias.name}")
                    continue
                bindings[local] = ImportBinding("module", modfile)
    return bindings


class WrapperResolver:
    """Cross-file, transitive "does calling this function reach a real spawn"
    resolver. See module docstring for the `build_scanner` contract.

    `unresolved_imports` accumulates the same honesty-bucket entries
    `collect_imports` produces, across every module this resolver visits —
    a caller wanting a corpus-wide unresolved-import count reads this set
    directly rather than re-deriving it.
    """

    def __init__(
        self,
        repo_root: Path,
        build_scanner: Callable[[str, ast.Module], "_ScannerLike"],
    ) -> None:
        self.repo_root = repo_root
        self._build_scanner = build_scanner
        self._module_info_cache: dict[Path, ModuleInfo | None] = {}
        self._wrapper_cache: dict[tuple[Path, str], bool] = {}
        # A second, disjoint instance cache (never `_wrapper_cache`) for a
        # differently-scoped "does this reach a spawn" question over the
        # SAME (module, func) key shape -- e.g. the ratchet's own
        # module-scope UNKNOWN-argv0 promotion (see
        # docs/plans/2026-08-20-the-spawn-ratchet-stops-accumulating-
        # arrears.md § C6). Siting it here, rather than as a caller-owned
        # module-level global, keeps it owned by the same resolver whose
        # `visiting`/cycle-truncation invariants it must honour.
        self._promote_unknown_wrapper_cache: dict[tuple[Path, str], bool] = {}
        self.unresolved_imports: set[str] = set()

    def get_module_info(self, path: Path) -> ModuleInfo | None:
        """Parses, scans (via `build_scanner`), and resolves imports for
        `path`. Memoized; returns None for an unparseable/unreadable file."""
        resolved = path.resolve()
        if resolved in self._module_info_cache:
            return self._module_info_cache[resolved]
        try:
            source = resolved.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(resolved))
        except (SyntaxError, OSError):
            self._module_info_cache[resolved] = None
            return None
        scanner = self._build_scanner(_rel(self.repo_root, resolved), tree)
        imports = collect_imports(self.repo_root, tree, resolved, self.unresolved_imports)
        top_level_funcs = {
            n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        info = ModuleInfo(resolved, scanner, imports, top_level_funcs)
        self._module_info_cache[resolved] = info
        return info

    def target_reaches_spawn(
        self, info: ModuleInfo, target: tuple, visiting: frozenset
    ) -> bool:
        result, _truncated = self._target_reaches_spawn_ex(info, target, visiting)
        return result

    def _target_reaches_spawn_ex(
        self, info: ModuleInfo, target: tuple, visiting: frozenset
    ) -> tuple[bool, bool]:
        """Like `target_reaches_spawn`, plus a second `truncated` bool: True
        if this answer passed through a `key in visiting` cycle guard
        anywhere on the path, meaning it must NOT be cached.
        See `_func_is_wrapper_ex` for the invariant this maintains."""
        if target[0] == "name":
            name = target[1]
            if name in info.top_level_funcs:
                return self._func_is_wrapper_ex(info.path, name, visiting)
            binding = info.imports.get(name)
            if binding is not None and binding.kind == "func":
                return self._func_is_wrapper_ex(binding.module_path, binding.orig_name, visiting)
        elif target[0] == "attr":
            obj_name, attr = target[1], target[2]
            binding = info.imports.get(obj_name)
            if binding is not None and binding.kind == "module":
                return self._func_is_wrapper_ex(binding.module_path, attr, visiting)
        return False, False

    def func_is_wrapper(
        self, module_path: Path, func_name: str, visiting: frozenset = frozenset()
    ) -> bool:
        """True if `func_name`, a module-level def in `module_path`, directly
        or transitively (through other module-level wrappers, local or
        imported) contains a real spawn site. Memoized; cycle-safe via
        `visiting`."""
        result, _truncated = self._func_is_wrapper_ex(module_path, func_name, visiting)
        return result

    def _func_is_wrapper_ex(
        self, module_path: Path, func_name: str, visiting: frozenset
    ) -> tuple[bool, bool]:
        """`func_is_wrapper`'s real body, plus a `truncated` bool.

        `truncated` is True when this call's answer depends on a `key in
        visiting` cycle guard -- i.e. some OTHER function currently being
        resolved recurses back into `key` before `key` itself has a real
        answer. That `False` is a placeholder to break the recursion, not a
        proven fact about `key`, so it must never be written into
        `_wrapper_cache`: caching it would let the answer for
        a mutually-recursive function permanently depend on which function
        in the cycle was resolved first. A `True` result is never
        truncation-tainted -- once any route proves a real spawn, that
        proof holds regardless of which other routes were cycle-truncated
        -- so only a truncated `False` withholds the cache write.
        """
        key = (module_path, func_name)
        if key in self._wrapper_cache:
            return self._wrapper_cache[key], False
        if key in visiting:
            return False, True
        info = self.get_module_info(module_path)
        if info is None or func_name not in info.top_level_funcs:
            self._wrapper_cache[key] = False
            return False, False
        if info.scanner.func_spawns.get(func_name):
            self._wrapper_cache[key] = True
            return True, False
        next_visiting = visiting | {key}
        result = False
        truncated = False
        for target in info.scanner.generic_calls.get(func_name, []):
            target_result, target_truncated = self._target_reaches_spawn_ex(
                info, target, next_visiting
            )
            if target_result:
                # A True result is never truncation-tainted, regardless of
                # what earlier sibling targets in this loop hit the cycle
                # guard -- discard any truncation accumulated so far so the
                # cache write below is unconditional for this case.
                result = True
                truncated = False
                break
            if target_truncated:
                truncated = True
        if not truncated:
            self._wrapper_cache[key] = result
        return result, truncated

    def func_reaches_spawn_indirectly(self, info: ModuleInfo, func_name: str) -> bool:
        """True if any call made directly inside `func_name` (any nesting
        depth, not necessarily a module-level def) resolves -- locally or
        through an import -- to a spawn wrapper."""
        for target in info.scanner.generic_calls.get(func_name, []):
            if self.target_reaches_spawn(info, target, frozenset()):
                return True
        return False
