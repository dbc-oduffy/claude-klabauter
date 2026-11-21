"""The tracked paths a run's test leg needs, for a scoped clean-overlay export.

`test_closure` turns a run's pytest argv and declared writes into seeds, then into the tracked import
closure, conftest chain and root test config. It reads the index in process and spawns nothing. When
no test seed can be named it returns `paths=None`: the caller does the full fill."""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Collection, FrozenSet, List, Optional, Sequence, Set

from coordinator_core.ops.dispatch_emit import test_reach

ROOT_TEST_CONFIG = ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py")


@dataclass(frozen=True)
class Closure:
    """`paths` is the closure (`None` means "no scoping, do the full fill"); `seeds` the test files it
    grew from; `note` says why `paths` is `None`, empty otherwise."""

    paths: Optional[FrozenSet[str]]
    seeds: tuple
    note: str


def _norm(raw: str, root: Path) -> str:
    rel = raw.replace("\\", "/")
    if os.path.isabs(raw):
        try:
            rel = Path(raw).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return rel
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _argv_seeds(root: Path, argv: Sequence[str], tracked: Collection[str]) -> tuple:
    """`(seeds, directory_tokens)`: tracked `.py` files argv names (node ids cut at `::`), and
    argv tokens naming a tracked directory (a run the closure cannot enumerate)."""
    seeds: List[str] = []
    dirs: List[str] = []
    for token in argv:
        if not token or token.startswith("-"):
            continue
        rel = _norm(token.split("::", 1)[0], root)
        if not rel:
            continue
        if rel in tracked:
            if rel.endswith(".py"):
                seeds.append(rel)
            continue
        prefix = rel.rstrip("/") + "/"
        if any(p.startswith(prefix) for p in tracked):
            dirs.append(rel)
    return seeds, dirs


def _conftest_chain(seed: str, tracked: Collection[str]) -> Set[str]:
    out: Set[str] = set()
    for parent in PurePosixPath(seed).parents:
        name = parent.as_posix()
        conftest = "conftest.py" if name == "." else f"{name}/conftest.py"
        if conftest in tracked:
            out.add(conftest)
    return out


def _pytest_plugins(text: str) -> List[str]:
    """Dotted names a conftest's `pytest_plugins` lists. They are strings, not imports, so the import
    walk never sees them; pytest imports each at collection, and a missing one collects 0 tests."""
    if "pytest_plugins" not in text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in targets):
                continue
            value = node.value
            items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            out += [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]
    return out


def _conftest_reach(root: Path, conftests: Set[str], tracked: Collection[str]) -> Set[str]:
    """What the conftest chain imports at collection: its repo-rooted import closure, plus bare-name
    imports resolved against any tracked module of that name.

    Trap: a conftest that puts a lib dir on `sys.path` (root conftest -> coordinator/bin/lib, then
    `import op_trampoline`) imports by bare name, which the repo-rooted module map cannot resolve;
    without this the scoped export omits the target and the test leg collects 0 tests. A bare name
    matches every module whose last dotted part is that name; over-including only widens the export. A
    conftest's sibling module whose name the conftest quotes rides too: a conftest loads it by file path
    (`spec_from_file_location`, example-retrieval-repo tests/drive_root_write_guard.py) or bare after inserting its dir."""
    modules = test_reach._module_map(tracked)
    by_last: dict = {}
    helpers: Set[str] = set()
    for conftest in conftests:
        try:
            text = (root / conftest).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        helpers |= {modules[name] for name in _pytest_plugins(text) if name in modules}
        here = conftest.rpartition("/")[0]
        for p in modules.values():
            if p.rpartition("/")[0] == here and "/" in p:
                stem = p.rpartition("/")[2][:-3]
                if not stem.startswith(("test_", "conftest")) and f'"{stem}' in text:
                    helpers.add(p)
    reach: Set[str] = set(conftests) | helpers
    frontier = sorted(reach)
    for _ in range(test_reach.DEFAULT_DEPTH):
        nxt: Set[str] = set()
        for rel in frontier:
            nxt |= test_reach._imported_modules(root, rel, modules)
            for rec in test_reach._records_for(root, rel):
                top = rec.module.split(".", 1)[0]
                if rec.level or not top or top in modules or top in sys.stdlib_module_names:
                    continue
                if not by_last:
                    for name, path in modules.items():
                        by_last.setdefault(name.rpartition(".")[2], set()).add(path)
                nxt |= by_last.get(top, set())
        nxt -= reach
        reach |= nxt
        frontier = sorted(nxt)
        if not frontier:
            break
    for rel in list(reach):
        reach |= test_reach._package_inits(rel, tracked)
    return reach


def test_closure(root: Path | str, declared: Sequence[str], argv: Sequence[str]) -> Closure:
    """The closure of the test files `argv` and `declared` name, or `paths=None` with a note when no
    test seed exists or `argv` names a directory (whose tests the closure cannot enumerate)."""
    root = Path(root)
    tracked = test_reach._tracked(root)
    declared_rel = [_norm(d, root) for d in declared]
    argv_seeds, argv_dirs = _argv_seeds(root, argv, tracked)
    if argv_dirs:
        return Closure(None, (), f"argv names a directory ({argv_dirs[0]}); full fill")
    seeds = sorted(
        {*argv_seeds, *(d for d in declared_rel if d.endswith(".py") and PurePosixPath(d).name.startswith("test_")),
         *test_reach.seeds_for_landing(root, declared_rel)}
    )
    if not seeds:
        return Closure(None, (), "no test file in argv or declared writes; full fill")
    paths: Set[str] = set(test_reach.reach_paths(root, seeds)) | set(declared_rel)
    for seed in seeds:
        paths |= _conftest_chain(seed, tracked)
    paths |= {name for name in ROOT_TEST_CONFIG if name in tracked}
    paths |= _conftest_reach(root, {p for p in paths if p.rpartition("/")[2] == "conftest.py"}, tracked)
    return Closure(frozenset(paths), tuple(seeds), "")
