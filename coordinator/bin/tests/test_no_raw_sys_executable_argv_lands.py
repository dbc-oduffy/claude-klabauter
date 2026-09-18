"""
Landing guard: a shrink-only ratchet on raw `[sys.executable, ...]` argv sites.

Purpose: `sys.executable` is the running image, not a trustworthy "a python
interpreter" -- under an installed forwarder exe the two differ and a child
handed that path silently never runs (see
`coordinator/bin/lib/python_interp.py`). New call sites should route through
that shared resolver instead of re-deriving the ladder inline. This guard
does not forbid `sys.executable` outright (the resolver module and its one
sanctioned inline duplicate in `claude-doe.py` both use it deliberately) --
it walks `coordinator/bin` and `coordinator/lib` with `ast`, counts list
literals whose first element is `sys.executable`, and refuses a count ABOVE
the recorded baseline in `raw_sys_executable_argv_baseline.json`. The count
is shrink-only: a lower actual count than the baseline is ALSO a failure
("baseline is stale, lower it to N"), so the number ratchets down over time
and never silently drifts back up.

Negative spec: never a regex over source text (misses multi-line literals,
false-positives on comments/strings), never a `subprocess` spawn (this is a
pure `ast` walk over files already on disk), never a `git` invocation
(the walk is filesystem-scoped, not diff-scoped). The excluded-file rule
(directory-name and basename-prefix patterns) lives as data in the baseline
JSON, not as a second hardcoded list here -- one source of truth for what
"a test file" means for this sweep.

Spec: docs/plans/2026-08-31-the-sys-executable-class-one-shared-inte.md, C6.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

BASELINE_PATH = Path(__file__).parent / "raw_sys_executable_argv_baseline.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
SWEPT_DIRS = ("coordinator/bin", "coordinator/lib")


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _is_excluded(rel_posix: str, baseline: dict) -> bool:
    for pattern in baseline["exclude_dir_patterns"]:
        if pattern in f"/{rel_posix}":
            return True
    basename = rel_posix.rsplit("/", 1)[-1]
    for prefix in baseline["exclude_basename_prefixes"]:
        if basename.startswith(prefix):
            return True
    return False


def _count_raw_sites(path: Path) -> int:
    """Count `[sys.executable, ...]` list literals in one file via `ast`.

    Only a non-empty list literal whose FIRST element is the attribute access
    `sys.executable` counts -- `sys.executable` used bare (not as an argv
    list's head), or appearing later in a list, is a different shape this
    guard does not police.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return 0
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if (
            isinstance(first, ast.Attribute)
            and first.attr == "executable"
            and isinstance(first.value, ast.Name)
            and first.value.id == "sys"
        ):
            count += 1
    return count


def _sweep() -> dict:
    baseline = _load_baseline()
    per_file: dict[str, int] = {}
    for swept_dir in SWEPT_DIRS:
        base = REPO_ROOT / swept_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel_posix = path.relative_to(REPO_ROOT).as_posix()
            if _is_excluded(rel_posix, baseline):
                continue
            count = _count_raw_sites(path)
            if count:
                per_file[rel_posix] = count
    return per_file


def test_raw_sys_executable_argv_count_does_not_exceed_baseline():
    baseline = _load_baseline()
    per_file = _sweep()
    total = sum(per_file.values())
    baseline_total = baseline["total"]

    assert total <= baseline_total, (
        f"raw `[sys.executable, ...]` argv sites grew from {baseline_total} to "
        f"{total} across {SWEPT_DIRS} -- route new call sites through "
        f"coordinator/bin/lib/python_interp.py"
    )
    assert total >= baseline_total, (
        f"raw `[sys.executable, ...]` argv sites shrank from {baseline_total} "
        f"to {total} -- baseline is stale, lower it to {total}"
    )


def test_raw_sys_executable_argv_baseline_matches_swept_files():
    """The per-file breakdown itself is shrink-only-consistent, not just the total.

    Catches a case the total-only check would miss: one file's count rose while
    another's fell by the same amount, netting to an unchanged total.
    """
    baseline = _load_baseline()
    per_file = _sweep()
    for rel_posix, count in per_file.items():
        recorded = baseline["per_file"].get(rel_posix, 0)
        assert count <= recorded, (
            f"{rel_posix}: raw `[sys.executable, ...]` argv sites grew from "
            f"{recorded} to {count} -- route new call sites through "
            f"coordinator/bin/lib/python_interp.py"
        )
