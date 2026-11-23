"""The tracked paths a run's test leg needs, for a scoped clean-overlay export.

`test_closure` turns a run's pytest argv and declared writes into seeds, then into the tracked import
closure, conftest chain and root test config. It reads the index in process and spawns nothing. When
no test seed can be named it returns `paths=None`: the caller does the full fill."""

from __future__ import annotations

import ast
import os
import re
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


_QUOTED_DOTTED_RE = re.compile(r"""["']([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)["']""")


def _bare_dotted(module: str, names: Sequence[str], by_last: dict) -> Set[str]:
    """Tracked files a bare `import a.b` / `from a.b import c` reaches: every dotted prefix of the
    module and each imported name as a submodule, matched by path suffix (`a/b.py`, `a/b/__init__.py`).

    Trap: a sys.path-inserted root (example-game-repo tools/likeness-prototype, hyphenated) never appears in
    the repo-rooted module names, so only a suffix match finds `likeness.pipeline.native_postcreate_stage`;
    matching the top name alone left the package and the submodule out (036bcf collected 0 tests)."""
    parts = module.split(".")
    dotted = [".".join(parts[: i + 1]) for i in range(len(parts))] + [f"{module}.{n}" for n in names if n != "*"]
    out: Set[str] = set()
    for name in dotted:
        tail = name.replace(".", "/")
        wants = (f"{tail}.py", f"{tail}/__init__.py")
        for path in by_last.get(name.rpartition(".")[2], ()):
            if any(path == w or path.endswith("/" + w) for w in wants):
                out.add(path)
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
        helpers |= {modules[name] for name in _QUOTED_DOTTED_RE.findall(text) if name in modules}
        here = conftest.rpartition("/")[0]
        # Trap: a repo-root module has no "/" (its dir is ""); the root conftest's path-loaded sibling
        # (drive_root_write_guard.py) must still ride, or every scoped mirror fails pytest_configure.
        for p in modules.values():
            if p.rpartition("/")[0] == here:
                stem = p.rpartition("/")[2][:-3]
                if not stem.startswith(("test_", "conftest")) and (f'"{stem}' in text or f"'{stem}" in text):
                    helpers.add(p)
    reach: Set[str] = set(conftests) | helpers
    frontier = sorted(reach)
    # Trap: no depth cap. pytest runs every conftest import at collection, so a truncated walk leaves a
    # module out of the mirror and the leg collects 0 tests (6b3402: sizing_assemble's own imports).
    while frontier:
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
                nxt |= _bare_dotted(rec.module, rec.names, by_last)
        nxt -= reach
        reach |= nxt
        frontier = sorted(nxt)
    for rel in list(reach):
        reach |= test_reach._package_inits(rel, tracked)
    return reach


_STRING_LITERAL_RE = re.compile(r"""["']([^"'\\\n]{3,200})["']""")
_ENUMERATES_RE = re.compile(r"\.(?:rglob|glob|iterdir|scandir)\(|os\.(?:listdir|walk)\(")
_SOURCE_RELATIVE_RE = re.compile(r"__file__|\.parents\[|\.parent\.parent")


def _data_reach(root: Path, py_paths: Collection[str], tracked: Collection[str]) -> tuple:
    """`(data, unproven)`: tracked non-Python files a closure module names by a string literal (a full
    tracked path, a path suffix, or a bare file name), and the first closure module that enumerates a
    directory relative to its own source tree, whose reads no literal bounds.

    Trap: the literal may be only the file name (`Path(__file__).parent.parent / "prompts" / "x.txt"`),
    so a name with an extension matches by basename; over-including only widens the mirror."""
    by_base: dict = {}
    for p in tracked:
        if not p.endswith(".py"):
            by_base.setdefault(p.rpartition("/")[2], []).append(p)
    data: Set[str] = set()
    unproven = ""
    for rel in sorted(py_paths):
        if not rel.endswith(".py"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lit in _STRING_LITERAL_RE.findall(text):
            lit = lit.replace("\\", "/")
            while lit.startswith("./"):
                lit = lit[2:]
            base = lit.rpartition("/")[2]
            if "." not in base:
                continue
            for p in by_base.get(base, ()):
                if "/" not in lit or p == lit or p.endswith("/" + lit):
                    data.add(p)
        if not unproven and _ENUMERATES_RE.search(text) and _SOURCE_RELATIVE_RE.search(text):
            unproven = rel
    return data, unproven


def _package_data(py_paths: Collection[str], tracked: Collection[str]) -> Set[str]:
    """Tracked non-Python files under each directory holding a closure module (a package or a namespace
    directory without `__init__.py`), nested non-package directories included.

    Trap: a nested package is its own boundary; its data rides only when the closure reaches one of its
    modules, so a sibling package's data never widens the mirror. The repo root is never a wanted dir."""
    inits = {p[: -len("__init__.py")].rstrip("/") for p in tracked if p.endswith("__init__.py")}
    wanted = {d for d in (p.rpartition("/")[0] for p in py_paths if p.endswith(".py")) if d}
    if not wanted:
        return set()
    boundary = inits | wanted
    out: Set[str] = set()
    for p in tracked:
        if p.endswith(".py"):
            continue
        d = p.rpartition("/")[0]
        while d and d not in boundary:
            d = d.rpartition("/")[0]
        if d in wanted:
            out.add(p)
    return out


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
    # Trap: declared writes join without their package chain; a package dir missing its __init__.py
    # imports as a namespace package and `from pkg import sub` fails at collection (036bcf).
    for rel in list(paths):
        paths |= test_reach._package_inits(rel, tracked)
    data, unproven = _data_reach(root, paths, tracked)
    if unproven:
        return Closure(None, tuple(seeds), f"{unproven} enumerates files relative to its source tree; closure unproven, full fill")
    return Closure(frozenset(paths | data | _package_data(paths, tracked)), tuple(seeds), "")
