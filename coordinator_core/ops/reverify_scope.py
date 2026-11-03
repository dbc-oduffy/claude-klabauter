"""Reverify scope: the tests covering a run's own changed files, and the pre_existing split.

``select_tests`` names the tests a delivery reverify runs for ``base..head``;
``split_pre_existing`` keeps a red that already fails at the base off the run's charge.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Set

from coordinator_core.git.run import run_git
from coordinator_core.diff_scoped_tests import _TEST_FILE_RE, _read_testpaths, _under_testpaths

from coordinator_core.pyimports import module_name_for_file, resolve_relative_module


def _module_files(module: str) -> List[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


_TEST_STEM_RE = re.compile(r"^test_(.+)\.py$")


def _changed_paths(repo_root: Path, base: str, head: str) -> List[str]:
    """Posix paths changed between ``base`` and ``head``, deletions excluded (one git spawn)."""
    if not base:
        raise ValueError("select_tests: base is unknown (None or empty)")
    proc = run_git(["diff", "--name-only", "--diff-filter=d", base, head], cwd=str(repo_root))
    if proc.returncode == 127:
        raise ValueError(f"select_tests: git diff spawn failed: {proc.stderr.strip()}")
    if proc.returncode != 0:
        raise ValueError(f"select_tests: git diff {base}..{head} failed: {proc.stderr.strip()}")
    return sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()})


def _is_test_file(path: str, testpaths: List[str]) -> bool:
    return bool(_TEST_FILE_RE.match(PurePosixPath(path).name)) and _under_testpaths(path, testpaths)


_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|(?:[^\n\\]|\\\n)*)"
    r"|import[ \t]+((?:[^\n\\]|\\\n)*))",
    re.M,
)


def _imported(repo_root: Path, rel: str, sources: Set[str]) -> Set[str]:
    """The ``sources`` that an import statement in ``rel`` names.

    A regex scan of import statements, not an AST parse: parsing thousands of candidates costs
    seconds. A docstring line shaped like an import over-selects, never under-selects.
    """
    try:
        text = (repo_root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    hit: Set[str] = set()
    is_pkg = rel.rsplit("/", 1)[-1] == "__init__.py"
    here = module_name_for_file(rel)
    for m in _IMPORT_RE.finditer(text):
        wanted: List[str] = []
        if m.group(4) is not None:
            for part in m.group(4).replace("\\\n", " ").split(","):
                tok = part.split("#")[0].split()
                if tok:
                    wanted.extend(_module_files(tok[0]))
        else:
            dots, mod, names = m.group(1), m.group(2), m.group(3)
            base = (
                resolve_relative_module(mod, len(dots), here, is_pkg) if dots else (mod or None)
            )
            if not base:
                continue
            wanted.extend(_module_files(base))
            for part in names.replace("\\\n", " ").strip("()").split(","):
                tok = part.split("#")[0].split()
                if tok and tok[0] != "*":
                    wanted.extend(_module_files(f"{base}.{tok[0]}"))
        hit.update(p for p in wanted if p in sources)
    return hit


def _tracked_test_index(repo_root: Path, testpaths: List[str]) -> Dict[str, List[str]]:
    """Basename stem -> tracked test files under ``testpaths`` (one ``git ls-files`` spawn).

    Replaces ``map_source_to_tests``, which re-walks the whole tree on every call.
    """
    proc = run_git(["ls-files", "--", "*test_*.py"], cwd=str(repo_root))
    if proc.returncode != 0:
        raise ValueError(f"select_tests: git ls-files failed: {proc.stderr.strip()}")
    index: Dict[str, Set[str]] = {}
    for ln in proc.stdout.splitlines():
        rel = ln.strip()
        m = _TEST_STEM_RE.match(PurePosixPath(rel).name) if rel else None
        if m and _under_testpaths(rel, testpaths):
            index.setdefault(m.group(1), set()).add(rel)
    return {stem: sorted(paths) for stem, paths in index.items()}


def _mapped(index: Dict[str, List[str]], source: str) -> List[str]:
    return index.get(PurePosixPath(source).stem.replace("-", "_"), [])


def _importers(repo_root: Path, sources: Set[str]) -> Dict[str, Set[str]]:
    """Tracked ``.py`` file -> the ``sources`` it imports: one ``git grep`` prefilter, regex-confirmed."""
    leaves = set()
    for src in sources:
        p = PurePosixPath(src)
        leaves.add(p.parent.name if p.stem == "__init__" else p.stem)
    leaves.discard("")
    if not leaves:
        return {}
    alt = "|".join(re.escape(leaf) for leaf in sorted(leaves))
    # An import line naming a leaf, or a continuation line of a parenthesised import list carrying one.
    pattern = (
        rf"^[[:space:]]*(from|import)[[:space:]][^#]*\b({alt})\b"
        rf"|^[[:space:]]+([A-Za-z_][A-Za-z_0-9]*([[:space:]]+as[[:space:]]+[A-Za-z_0-9]+)?[[:space:]]*,[[:space:]]*)*"
        rf"({alt})([[:space:]]+as[[:space:]]+[A-Za-z_0-9]+)?[[:space:]]*,?[[:space:]]*(#.*)?$"
    )
    proc = run_git(["grep", "-l", "-E", "-e", pattern, "--", "*.py"], cwd=str(repo_root))
    if proc.returncode not in (0, 1):
        raise ValueError(f"select_tests: git grep failed: {proc.stderr.strip()}")
    out: Dict[str, Set[str]] = {}
    for cand in (ln.strip() for ln in proc.stdout.splitlines()):
        if cand and cand not in sources:
            hit = _imported(repo_root, cand, sources)
            if hit:
                out[cand] = hit
    return out


def select_tests(repo_root: Path, base: str, head: str = "HEAD") -> dict:
    """Tests covering the files changed in ``base..head``, and those of each changed module's importers.

    Importer closure is one hop: a changed module's direct importers contribute their own mapped
    tests (a test of an importer need not import the changed module).

    Returns ``{"tests": sorted posix paths, "changed": posix paths, "fully_mapped": bool}``.
    ``fully_mapped`` is False when a changed non-test ``.py`` has no mapped or importing test.
    Raises ``ValueError`` when ``base`` is unknown or the diff fails; never an empty set for that.
    """
    repo_root = Path(repo_root)
    changed = _changed_paths(repo_root, base, head)
    root = str(repo_root)
    testpaths = _read_testpaths(root)
    tests: Set[str] = set()
    fully_mapped = True
    changed_src: Set[str] = set()
    index = _tracked_test_index(repo_root, testpaths)
    for path in changed:
        if not path.endswith(".py"):
            continue
        if _is_test_file(path, testpaths):
            tests.add(path)
        else:
            changed_src.add(path)
    importers = _importers(repo_root, changed_src)
    for importer in importers:
        if _is_test_file(importer, testpaths):
            tests.add(importer)
        else:
            tests.update(_mapped(index, importer))
    for path in sorted(changed_src):
        found = set(_mapped(index, path))
        if not found:
            tests_dir = PurePosixPath(path).parent / "tests"
            found = {
                imp for imp, hit in importers.items()
                if path in hit and PurePosixPath(imp).parent == tests_dir and _is_test_file(imp, testpaths)
            }
        if not found:
            fully_mapped = False
        tests.update(found)
    return {"tests": sorted(tests), "changed": changed, "fully_mapped": fully_mapped}


def split_pre_existing(failing: Iterable[str], failing_at_base: Iterable[str]) -> dict:
    """Split ``failing`` into run-charged reds and ``pre_existing`` ones that also fail at the base."""
    at_base = set(failing_at_base)
    charged: List[str] = []
    pre: List[str] = []
    for node in failing:
        (pre if node in at_base else charged).append(node)
    return {"failing": charged, "pre_existing": pre}
