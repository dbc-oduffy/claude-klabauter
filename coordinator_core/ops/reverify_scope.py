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


#: Most owner tests (beyond the touched ones) a reverify adds: past it the run holds the suite mutex like a suite run.
_SUITE_BOUND = 10

_TEST_STEM_RE = re.compile(r"^test_(.+)\.py$")
_JS_TEST_STEM_RE = re.compile(r"^(.+)\.(?:test|spec)\.[jt]sx?$")
_JS_SOURCE_RE = re.compile(r"\.[jt]sx?$")
_JS_TEST_PATHSPECS = ("*.test.[jt]s", "*.test.[jt]sx", "*.spec.[jt]s", "*.spec.[jt]sx")
#: Index key prefix keeping JS/TS stems apart from Python stems.
_JS_KEY = "js:"


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


def _is_js_test_file(path: str) -> bool:
    return bool(_JS_TEST_STEM_RE.match(PurePosixPath(path).name))


def _is_test_file(path: str, testpaths: List[str]) -> bool:
    """Python tests are gated on pytest ``testpaths``; JS/TS tests have none, so are never gated."""
    if _is_js_test_file(path):
        return True
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
    """Basename stem -> tracked test files (one ``git ls-files`` spawn).

    Python tests sit under ``testpaths``; JS/TS ``*.test|spec.[jt]sx?`` files index under
    ``_JS_KEY`` + stem with no ``testpaths`` gate. Replaces ``map_source_to_tests``, which
    re-walks the whole tree on every call.
    """
    proc = run_git(["ls-files", "--", "*test_*.py", *_JS_TEST_PATHSPECS], cwd=str(repo_root))
    if proc.returncode != 0:
        raise ValueError(f"select_tests: git ls-files failed: {proc.stderr.strip()}")
    index: Dict[str, Set[str]] = {}
    for ln in proc.stdout.splitlines():
        rel = ln.strip()
        if not rel:
            continue
        name = PurePosixPath(rel).name
        js = _JS_TEST_STEM_RE.match(name)
        if js:
            index.setdefault(_JS_KEY + js.group(1), set()).add(rel)
            continue
        m = _TEST_STEM_RE.match(name)
        if m and _under_testpaths(rel, testpaths):
            index.setdefault(m.group(1), set()).add(rel)
    return {stem: sorted(paths) for stem, paths in index.items()}


def _mapped(index: Dict[str, List[str]], source: str) -> List[str]:
    stem = PurePosixPath(source).stem
    if _JS_SOURCE_RE.search(source):
        return index.get(_JS_KEY + stem, [])
    return index.get(stem.replace("-", "_"), [])


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
        rf"^[[:space:]]*(from|import)[[:space:]][^#]*(^|[^[:alnum:]_])({alt})([^[:alnum:]_]|$)"
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
    """The touched test files plus the direct test owners of each touched source file.

    A direct owner is the stem-mapped test of a changed module, or a test file importing it. A
    non-test importer's own tests are never pulled in. Owners beyond
    ``_SUITE_BOUND`` files (stem-mapped first; touched tests always kept) are dropped and
    ``notice`` says so.

    Returns ``{"tests": sorted posix paths, "changed": posix paths, "fully_mapped": bool,
    "notice": str, "tripwires": sorted posix paths}`` (``notice`` is empty unless the selection was narrowed).
    ``tripwires`` are importing tests that are neither touched nor stem-mapped, not bounded by the
    suite bound.
    ``fully_mapped`` is False when a changed non-test ``.py`` or ``.js/.ts`` source has no mapped or importing test.
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
        if not (path.endswith(".py") or _JS_SOURCE_RE.search(path)):
            continue
        if _is_test_file(path, testpaths):
            tests.add(path)
        else:
            changed_src.add(path)
    touched = set(tests)
    importers = _importers(repo_root, {p for p in changed_src if p.endswith(".py")})
    importing_tests = {imp for imp in importers if _is_test_file(imp, testpaths)}
    mapped: Set[str] = set()
    for path in sorted(changed_src):
        found = set(_mapped(index, path))
        mapped.update(found)
        if not found:
            tests_dir = PurePosixPath(path).parent / "tests"
            found = {
                imp for imp, hit in importers.items()
                if path in hit and PurePosixPath(imp).parent == tests_dir and _is_test_file(imp, testpaths)
            }
            mapped.update(found)
        if not found:
            fully_mapped = False
    extras = sorted(mapped - touched) + sorted(importing_tests - touched - mapped)
    kept = extras[:_SUITE_BOUND]
    notice = ""
    if len(extras) > len(kept):
        notice = (
            f"reverify selection narrowed: {len(touched) + len(extras)} test files would run, over the "
            f"{_SUITE_BOUND}-owner-file bound; kept {len(touched)} touched and {len(kept)} owner tests, "
            f"dropped {len(extras) - len(kept)} owner tests (importers or stem-mapped); quote this notice with any pass."
        )
    return {
        "tests": sorted(touched | set(kept)),
        "changed": changed,
        "fully_mapped": fully_mapped,
        "notice": notice,
        "tripwires": sorted(importing_tests - touched - mapped),
    }


def split_pre_existing(failing: Iterable[str], failing_at_base: Iterable[str]) -> dict:
    """Split ``failing`` into run-charged reds and ``pre_existing`` ones that also fail at the base."""
    at_base = set(failing_at_base)
    charged: List[str] = []
    pre: List[str] = []
    for node in failing:
        (pre if node in at_base else charged).append(node)
    return {"failing": charged, "pre_existing": pre}
