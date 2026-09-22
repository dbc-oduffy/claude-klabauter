"""
coordinator_core.ops.fleet.tests.test_archive_family_fixture_guard

C5(c) of docs/plans/2026-08-13-archive-family-coverage-restoration.md. Reads
state/audits/2026-09-10-archive-family-disposition-list.md -- the committed roster AC5's cap is
measured over -- and asserts three things Rule 4
(coordinator_core/tests/test_no_new_spawning_tests.py) does not already cover:

  1. No conftest.py under coordinator_core/ops/fleet/tests/ or coordinator_core/ops/tests/
     constructs a repo (AC1).
  2. The archive family's real-git test-function total, summed over the roster's rows, stays at
     or below AC5's cap of 25.
  3. Every real-git roster row carries a justification-register docstring (AC5b).

Negative-spec: does NOT re-enforce "a spawning test file must be cadence-tiered" (Rule 4's job)
or re-implement the grandfather-clause check (test_no_grandfather_clause_is_reintroduced) --
authoring either here would drift from Rule 4 rather than extend it, per this chunk's own body.

The roster is the denominator, not a directory glob: a `pending` row is allowed to be absent on
disk (not yet ported) and always counts 0 toward the cap; any other status names a module that
must exist -- a roster row naming a landed module that isn't on disk is this guard's failure, not
a silent skip. A missing or unparseable roster file fails loud rather than passing vacuously,
because a vacuous pass here is exactly the "roster the guard cannot parse" hazard the chunk's own
body names.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROSTER_PATH = _REPO_ROOT / "state/audits/2026-09-10-archive-family-disposition-list.md"

_REAL_GIT_CAP = 25

_CONFTEST_PATHS = (
    _REPO_ROOT / "coordinator_core/ops/fleet/tests/conftest.py",
    _REPO_ROOT / "coordinator_core/ops/tests/conftest.py",
)

# Repo-building tells: a real `git init`, a subprocess spawn not wrapped in patch(...)/Mock(...),
# or an ambient ("fleet_repo"/"norm_repo"/"session_repo"/"handoff_repo"/"HandoffRepo") fixture
# name -- the exact shape the 2026-08-07 incident cut.
_REPO_BUILD_PATTERN = re.compile(
    r"git\s+init"
    r"|subprocess\.(run|Popen|check_call|check_output)\("
    r"|\bHandoffRepo\b"
    r"|\bfleet_repo\b"
    r"|\bnorm_repo\b"
    r"|\bsession_repo\b"
)
_MOCK_WRAPPER_PATTERN = re.compile(r"\bpatch\(|\bMock\(|\bMagicMock\(")

_YAML_FENCE_PATTERN = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)

_STATUSES_REQUIRING_PRESENCE = frozenset({"ported", "rewritten", "dropped-as-obsolete"})


def _load_roster() -> list[dict]:
    if not _ROSTER_PATH.exists():
        pytest.fail(
            f"Archive-family roster missing: {_ROSTER_PATH}. C5(c)'s guard fails loud rather "
            "than passing vacuously when its denominator is absent."
        )
    text = _ROSTER_PATH.read_text(encoding="utf-8")
    match = _YAML_FENCE_PATTERN.search(text)
    if not match:
        pytest.fail(
            f"Archive-family roster at {_ROSTER_PATH} carries no parseable ```yaml fence -- the "
            "roster the guard cannot parse is a named failure mode, not a pass."
        )
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        pytest.fail(f"Archive-family roster YAML at {_ROSTER_PATH} failed to parse: {exc}")
    rows = parsed.get("roster") if isinstance(parsed, dict) else None
    if not isinstance(rows, list) or not rows:
        pytest.fail(
            f"Archive-family roster at {_ROSTER_PATH} parsed but has no non-empty `roster:` list."
        )
    return rows


def _module_path_for(row: dict) -> Path:
    module = row.get("module")
    if not module:
        pytest.fail(f"Roster row missing `module` key: {row!r}")
    return _REPO_ROOT / module


class TestNoRepoBuildingConftest:
    """AC1: no conftest.py in scope constructs a repo."""

    @pytest.mark.parametrize("conftest_path", _CONFTEST_PATHS, ids=lambda p: str(p))
    def test_conftest_builds_no_repo(self, conftest_path: Path) -> None:
        if not conftest_path.exists():
            return
        text = conftest_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _REPO_BUILD_PATTERN.search(line) and not _MOCK_WRAPPER_PATTERN.search(line):
                pytest.fail(
                    f"{conftest_path}:{lineno} looks like it constructs a real repo ambiently: "
                    f"{line.strip()!r}. AC1 forbids a repo-building conftest in this scope."
                )


class TestArchiveFamilyRealGitCap:
    """AC5 / AC5b, measured over the committed roster, not a directory glob."""

    def test_roster_rows_resolve_and_cap_holds(self) -> None:
        rows = _load_roster()
        total_real_git = 0
        missing_landed: list[str] = []
        unjustified: list[str] = []

        for row in rows:
            status = row.get("status")
            module_path = _module_path_for(row)
            real_git = row.get("real_git", 0)

            if status == "pending":
                if real_git:
                    pytest.fail(
                        f"{row.get('module')} is `pending` but claims real_git={real_git}; a "
                        "not-yet-ported module cannot spend cap it hasn't landed."
                    )
                continue

            if status not in _STATUSES_REQUIRING_PRESENCE:
                pytest.fail(
                    f"{row.get('module')} has unrecognised status {status!r} -- the roster the "
                    "guard cannot parse is a named failure mode."
                )

            if not module_path.exists():
                missing_landed.append(str(row.get("module")))
                continue

            total_real_git += int(real_git)

            if real_git:
                docstring = _module_docstring(module_path)
                if not _is_justification_register(docstring):
                    unjustified.append(str(row.get("module")))

        if missing_landed:
            pytest.fail(
                "Roster row(s) claim a landed status (ported/rewritten/dropped-as-obsolete) but "
                f"the module is not on disk: {missing_landed}. A roster row naming a module that "
                "does not exist is this guard's subject, not a silent skip."
            )

        if unjustified:
            pytest.fail(
                "Real-git roster row(s) missing an AC5b justification-register docstring naming "
                f"the specific git-state fact the filesystem cannot exhibit: {unjustified}."
            )

        assert total_real_git <= _REAL_GIT_CAP, (
            f"Archive-family real-git test-function total is {total_real_git}, over AC5's cap of "
            f"{_REAL_GIT_CAP}. Sum is over the committed roster at {_ROSTER_PATH}, not a "
            "directory glob."
        )


def _module_docstring(module_path: Path) -> str:
    text = module_path.read_text(encoding="utf-8")
    match = re.match(r'\s*"""(.*?)"""', text, re.DOTALL)
    return match.group(1) if match else ""


def _is_justification_register(docstring: str) -> bool:
    if len(docstring.strip()) < 80:
        return False
    return "git" in docstring.lower()
