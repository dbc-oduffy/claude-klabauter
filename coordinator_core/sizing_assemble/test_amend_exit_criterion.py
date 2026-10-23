"""Tests for `sizing-assemble --amend-exit-criterion`: statement replaced, acceptance kept, ruling recorded."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

_NAME = "2026-10-08-amend.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_RULING = "docs/plans/ruling.md"
_NEW = "The amended criterion text is observable and green."
_DOC = """schema: sizing-object
intent: "Amend the thing"
estimate:
  tshirt: XL
  provisional: true
route: plan
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
interaction_mode: {mode}
{criterion}"""
_CRITERION = """exit_criterion:
  statement: The old criterion.
  accepted:
    source: apm
    apm_ruling: first ruling
    ruling_ref: run-1
    'on': '2026-10-01'
    mode: ceo
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _RULING).write_text("ruling\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _sizing(repo: Path, mode: str = "ceo", criterion: str = _CRITERION) -> tuple[Path, str]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DOC.format(mode=mode, criterion=criterion), encoding="utf-8")
    return path, Path(record_homes.record_path("", "sizings", _NAME)).as_posix()


def test_amend_replaces_statement_keeps_acceptance_and_records_ruling(repo: Path) -> None:
    path, rel = _sizing(repo)
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", _RULING])
    assert code == sa.EXIT_OK
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    crit = doc["exit_criterion"]
    assert crit["statement"] == _NEW
    assert crit["accepted"]["ruling_ref"] == "run-1"
    (entry,) = crit["amendments"]
    assert entry["ruling_ref"] == _RULING and entry["statement"] == _NEW and entry["source"] == "apm"
    assert validate_frontmatter(doc, _SCHEMA) == []


def test_second_amend_appends(repo: Path) -> None:
    path, rel = _sizing(repo)
    assert sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", _RULING]) == sa.EXIT_OK
    assert sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW + " Again.", "--ruling", _RULING]) == sa.EXIT_OK
    crit = yaml.safe_load(path.read_text(encoding="utf-8"))["exit_criterion"]
    assert len(crit["amendments"]) == 2 and crit["statement"].endswith("Again.")


def test_missing_ruling_flag_is_usage_error(repo: Path) -> None:
    path, rel = _sizing(repo)
    before = path.read_text(encoding="utf-8")
    assert sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW]) == sa.EXIT_USAGE
    assert path.read_text(encoding="utf-8") == before


def test_ruling_path_that_is_not_a_file_is_refused(repo: Path) -> None:
    path, rel = _sizing(repo)
    before = path.read_text(encoding="utf-8")
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", "docs/plans/nope.md"])
    assert code == sa.EXIT_BUSINESS_FAIL
    assert path.read_text(encoding="utf-8") == before


def test_absolute_ruling_is_refused(repo: Path) -> None:
    _, rel = _sizing(repo)
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", str(repo / _RULING)])
    assert code == sa.EXIT_BUSINESS_FAIL


def test_sizing_without_exit_criterion_is_refused(repo: Path) -> None:
    path, rel = _sizing(repo, criterion="")
    before = path.read_text(encoding="utf-8")
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", _RULING])
    assert code == sa.EXIT_BUSINESS_FAIL
    assert path.read_text(encoding="utf-8") == before


def test_hands_on_sizing_admits_no_apm_ruling(repo: Path) -> None:
    path, rel = _sizing(repo, mode="hands-on")
    before = path.read_text(encoding="utf-8")
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", _RULING])
    assert code == sa.EXIT_BUSINESS_FAIL
    assert path.read_text(encoding="utf-8") == before


def test_pm_mode_sizing_admits_apm_ruling(repo: Path) -> None:
    path, rel = _sizing(repo, mode="pm")
    code = sa.main(["--amend-exit-criterion", rel, "--exit-criterion", _NEW, "--ruling", _RULING])
    assert code == sa.EXIT_OK
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["exit_criterion"]["amendments"][0]["mode"] == "pm"


def test_path_outside_sizings_is_refused(repo: Path) -> None:
    code = sa.main(["--amend-exit-criterion", _RULING, "--exit-criterion", _NEW, "--ruling", _RULING])
    assert code == sa.EXIT_BUSINESS_FAIL


def test_help_lists_the_route_beside_amend_research(capsys: pytest.CaptureFixture[str]) -> None:
    sa.main(["--help"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "--amend-exit-criterion" in text and "--amend-research" in text
