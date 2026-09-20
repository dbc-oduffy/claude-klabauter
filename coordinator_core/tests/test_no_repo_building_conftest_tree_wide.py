"""
coordinator_core.tests.test_no_repo_building_conftest_tree_wide

C11(c) of docs/plans/2026-08-13-archive-family-coverage-restoration.md. Extends C5(c)'s
archive-family-scoped guard (coordinator_core/ops/fleet/tests/test_archive_family_fixture_guard.py)
tree-wide, per that chunk's body:

  1. No conftest.py anywhere in the tree constructs a repo (the 2026-08-07 incident shape --
     a real `git init`, an unwrapped subprocess spawn, or an ambient repo-fixture name -- in a
     file Rule 4 cannot tier, since a conftest carries no marker).
  2. Every committed family roster's real-git test-function total stays at or below its own
     stated cap (currently just Population 1 / the archive family, cap 25, per
     state/audits/2026-09-10-archive-family-disposition-list.md; a future family roster is
     picked up automatically once it is added to `_FAMILY_ROSTERS` below).
  3. Every real-git roster row in a registered family carries an AC5b justification-register
     docstring.

Negative-spec: does NOT re-enforce "a spawning test file must be cadence-tiered" (Rule 4's job,
coordinator_core/tests/test_no_new_spawning_tests.py) or re-implement the grandfather-clause
check (test_no_grandfather_clause_is_reintroduced) -- authoring either here would drift from
Rule 4 rather than extend it. Does NOT re-run C5(c)'s own archive-family-scoped assertions --
this module's cap/justification checks operate over the same registered roster files C5(c) reads,
so its assertions are a superset by construction, not a duplicate implementation.

Does NOT author a Population-2 (wider-residue) roster or cap -- per
state/audits/2026-09-10-restoration-disposition-list-full.md (b), Population 2 is entirely
`pending`: C-RESIDUE-APPLY has landed no sub-chunk, so there is no committed roster/cap for this
guard to enforce yet. `_FAMILY_ROSTERS` is the extension point for when one lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Registered family rosters: (roster path, real-git cap). Extend this tuple, do not fork a
# parallel guard, when a future chunk commits a Population-2 (or later) roster + cap.
_FAMILY_ROSTERS: tuple[tuple[Path, int], ...] = (
    (_REPO_ROOT / "state/audits/2026-09-10-archive-family-disposition-list.md", 25),
)

_STATUSES_REQUIRING_PRESENCE = frozenset({"ported", "rewritten", "dropped-as-obsolete"})

_YAML_FENCE_PATTERN = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)

# Repo-building tells: a real `git init`, an unwrapped subprocess spawn, or an ambient
# ("fleet_repo"/"norm_repo"/"session_repo"/"handoff_repo"/"HandoffRepo") fixture name -- the
# exact shape the 2026-08-07 incident cut. Same pattern as C5(c), tree-wide scope.
_REPO_BUILD_PATTERN = re.compile(
    r"git\s+init"
    r"|subprocess\.(run|Popen|check_call|check_output)\("
    r"|\bHandoffRepo\b"
    r"|\bfleet_repo\b"
    r"|\bnorm_repo\b"
    r"|\bsession_repo\b"
)
_MOCK_WRAPPER_PATTERN = re.compile(r"\bpatch\(|\bMock\(|\bMagicMock\(")

# Directories that are not this tree's own test surface: vendored/build/publish mirrors would
# otherwise be double-scanned copies of files this guard already covers at their source location.
_EXCLUDED_DIR_PARTS = frozenset({"dist", "node_modules", ".git", "__pycache__"})


def _all_conftest_paths() -> list[Path]:
    return sorted(
        p
        for p in _REPO_ROOT.rglob("conftest.py")
        if not _EXCLUDED_DIR_PARTS.intersection(p.relative_to(_REPO_ROOT).parts)
    )


class TestNoRepoBuildingConftestTreeWide:
    """Tree-wide AC1: no conftest.py anywhere in scope constructs a repo."""

    @pytest.mark.parametrize(
        "conftest_path",
        _all_conftest_paths(),
        ids=lambda p: str(p.relative_to(_REPO_ROOT)),
    )
    def test_conftest_builds_no_repo(self, conftest_path: Path) -> None:
        text = conftest_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _REPO_BUILD_PATTERN.search(line) and not _MOCK_WRAPPER_PATTERN.search(line):
                pytest.fail(
                    f"{conftest_path}:{lineno} looks like it constructs a real repo ambiently: "
                    f"{line.strip()!r}. A repo-building conftest cannot be cadence-tiered by "
                    "Rule 4 (no marker on a conftest), which is exactly why this guard exists."
                )


def _load_roster(roster_path: Path) -> list[dict]:
    if not roster_path.exists():
        pytest.fail(
            f"Registered family roster missing: {roster_path}. A roster named in "
            "_FAMILY_ROSTERS must stay present -- fail loud rather than passing vacuously."
        )
    text = roster_path.read_text(encoding="utf-8")
    match = _YAML_FENCE_PATTERN.search(text)
    if not match:
        pytest.fail(f"Family roster at {roster_path} carries no parseable ```yaml fence.")
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        pytest.fail(f"Family roster YAML at {roster_path} failed to parse: {exc}")
    rows = parsed.get("roster") if isinstance(parsed, dict) else None
    if not isinstance(rows, list) or not rows:
        pytest.fail(f"Family roster at {roster_path} parsed but has no non-empty `roster:` list.")
    return rows


def _module_docstring(module_path: Path) -> str:
    text = module_path.read_text(encoding="utf-8")
    match = re.match(r'\s*"""(.*?)"""', text, re.DOTALL)
    return match.group(1) if match else ""


def _is_justification_register(docstring: str) -> bool:
    if len(docstring.strip()) < 80:
        return False
    return "git" in docstring.lower()


class TestRealGitCapPerRegisteredFamily:
    """AC5 / AC5b, generalized to every family with a committed roster + stated cap."""

    @pytest.mark.parametrize(
        "roster_path,cap",
        _FAMILY_ROSTERS,
        ids=lambda v: str(v) if isinstance(v, Path) else str(v),
    )
    def test_family_roster_cap_holds(self, roster_path: Path, cap: int) -> None:
        rows = _load_roster(roster_path)
        total_real_git = 0
        missing_landed: list[str] = []
        unjustified: list[str] = []

        for row in rows:
            status = row.get("status")
            module = row.get("module")
            if not module:
                pytest.fail(f"Roster row missing `module` key in {roster_path}: {row!r}")
            module_path = _REPO_ROOT / module
            real_git = row.get("real_git", 0)

            if status == "pending":
                if real_git:
                    pytest.fail(
                        f"{module} is `pending` but claims real_git={real_git} in {roster_path}."
                    )
                continue

            if status not in _STATUSES_REQUIRING_PRESENCE:
                pytest.fail(f"{module} has unrecognised status {status!r} in {roster_path}.")

            if not module_path.exists():
                missing_landed.append(module)
                continue

            total_real_git += int(real_git)

            if real_git:
                docstring = _module_docstring(module_path)
                if not _is_justification_register(docstring):
                    unjustified.append(module)

        if missing_landed:
            pytest.fail(
                f"Roster row(s) in {roster_path} claim a landed status but the module is not on "
                f"disk: {missing_landed}."
            )

        if unjustified:
            pytest.fail(
                f"Real-git roster row(s) in {roster_path} missing an AC5b justification-register "
                f"docstring: {unjustified}."
            )

        assert total_real_git <= cap, (
            f"Real-git test-function total for roster {roster_path} is {total_real_git}, over "
            f"its stated cap of {cap}."
        )
