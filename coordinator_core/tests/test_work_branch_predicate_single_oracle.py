"""Pins `is_work_branch` as the single work-branch-name oracle.

Six sites read the `work/*` branch shape at runtime:
`coordinator_core/hooks/auto_push.py`, `coordinator_core/hooks/day_branch_assert.py`,
`coordinator_core/hooks/runtime_tripwire_em_check.py`,
`coordinator_core/pickup_assemble/__init__.py`,
`coordinator_core/orientation/regenerate_cache.py`, and
`coordinator_core/ops/review_brightline_gate.py`. Each must ask
`coordinator_core.daily_branch.is_work_branch` (or `has_remote_prefix`), never a bare
`branch.startswith("work/")` or `re.compile(r"^work/")` literal — one site fixed alone
relocates the gap rather than closing it (docs/plans/2026-09-22-work-branch-predicates-
read-an-origin-prefixed-name.md).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "coordinator_core"

_LITERAL_RE = re.compile(r'startswith\("work/"\)|re\.compile\(r"\^work/"\)')

_ORACLE_MODULE = CORE_ROOT / "daily_branch.py"

_SIX_SITES = [
    CORE_ROOT / "hooks" / "auto_push.py",
    CORE_ROOT / "hooks" / "day_branch_assert.py",
    CORE_ROOT / "hooks" / "runtime_tripwire_em_check.py",
    CORE_ROOT / "pickup_assemble" / "__init__.py",
    CORE_ROOT / "orientation" / "regenerate_cache.py",
    CORE_ROOT / "ops" / "review_brightline_gate.py",
]


def _is_test_path(rel: Path) -> bool:
    if "tests" in rel.parts:
        return True
    return rel.name.startswith("test_")


def _non_test_python_files():
    for path in CORE_ROOT.rglob("*.py"):
        rel = path.relative_to(CORE_ROOT)
        if _is_test_path(rel):
            continue
        yield path


def test_no_stray_work_slash_literal_outside_the_oracle():
    offenders = []
    for path in _non_test_python_files():
        if path == _ORACLE_MODULE:
            continue
        if _LITERAL_RE.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))

    assert offenders == [], f"bare work/ literal outside daily_branch.py: {sorted(offenders)}"


def test_six_sites_import_the_oracle():
    for path in _SIX_SITES:
        text = path.read_text(encoding="utf-8")
        assert "from coordinator_core.daily_branch import" in text, (
            f"{path} must import from coordinator_core.daily_branch"
        )
