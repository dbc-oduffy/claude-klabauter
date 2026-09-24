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

Withheld exception: `auto_push.py` is NOT required to import `daily_branch` here. Its
conversion is plan row C2, gated on C0's git-push probe (epistemic-premise). C0's spine
row is absent from this dispatch, so C2 did not run and `auto_push.py` still carries the
bare literal at `branch_gate`. Per the plan's Exit criteria § 3, this is the one case
where the census command may print `1` instead of `0`, and this pin test names
`auto_push.py` as that one withheld site rather than failing the whole pin on it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "coordinator_core"

_LITERAL_RE = re.compile(r'startswith\("work/"\)|re\.compile\(r"\^work/"\)')

_ORACLE_MODULE = CORE_ROOT / "daily_branch.py"

# The withheld exception, per the plan's Exit criteria § 3 (C0 absent from this
# dispatch, so C2 -- auto_push.py's conversion -- did not run).
_WITHHELD_LITERAL_SITES = {CORE_ROOT / "hooks" / "auto_push.py"}

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
        text = path.read_text(encoding="utf-8")
        if _LITERAL_RE.search(text):
            offenders.append(path)

    assert set(offenders) == _WITHHELD_LITERAL_SITES, (
        "only auto_push.py (C2, withheld per Exit criteria § 3) may still carry the "
        f"bare work/ literal; found: {sorted(str(p) for p in offenders)}"
    )


def test_six_sites_import_the_oracle_except_the_withheld_one():
    for path in _SIX_SITES:
        text = path.read_text(encoding="utf-8")
        imports_oracle = "from coordinator_core.daily_branch import" in text
        if path in _WITHHELD_LITERAL_SITES:
            assert not imports_oracle, (
                f"{path} is recorded as withheld (C2 did not run) but now imports "
                "daily_branch -- update this pin's exception list"
            )
            continue
        assert imports_oracle, f"{path} must import from coordinator_core.daily_branch"
