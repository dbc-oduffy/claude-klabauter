"""Reverify scope: the tests covering a run's own changed files, and the pre_existing split.

``select_tests`` names the tests a delivery reverify runs for ``base..head``;
``split_pre_existing`` keeps a red that already fails at the base off the run's charge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Set

from coordinator_core.diff_scoped_tests import _TEST_FILE_RE, _read_testpaths, _under_testpaths

# Private helper imported cross-module: no public import-closure primitive exists.
from coordinator_core.ops.review_reverify import _imported_edited
from coordinator_core.source_test_map import map_source_to_tests


def _changed_paths(repo_root: Path, base: str, head: str) -> List[str]:
    """Posix paths changed between ``base`` and ``head``, deletions excluded (one git spawn)."""
    if not base:
        raise ValueError("select_tests: base is unknown (None or empty)")
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=d", base, head],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ValueError(f"select_tests: git diff spawn failed: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"select_tests: git diff {base}..{head} failed: {proc.stderr.strip()}")
    return sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()})


def _is_test_file(path: str, testpaths: List[str]) -> bool:
    return bool(_TEST_FILE_RE.match(PurePosixPath(path).name)) and _under_testpaths(path, testpaths)


def _closure_tests(repo_root: Path, source: str, testpaths: List[str]) -> Set[str]:
    """Tests in ``source``'s own package ``tests/`` dir that import it."""
    tests_dir = repo_root / PurePosixPath(source).parent / "tests"
    if not tests_dir.is_dir():
        return set()
    hits: Set[str] = set()
    for cand in tests_dir.glob("test_*.py"):
        rel = cand.relative_to(repo_root).as_posix()
        if _under_testpaths(rel, testpaths) and _imported_edited(repo_root, rel, {source}):
            hits.add(rel)
    return hits


def select_tests(repo_root: Path, base: str, head: str = "HEAD") -> dict:
    """Tests covering the files changed in ``base..head``.

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
    for path in changed:
        if not path.endswith(".py"):
            continue
        if _is_test_file(path, testpaths):
            tests.add(path)
            continue
        found = set(map_source_to_tests(path, root))
        if not found:
            found = _closure_tests(repo_root, path, testpaths)
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
