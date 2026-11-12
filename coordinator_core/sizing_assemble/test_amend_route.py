"""Tests for `sizing-assemble --amend-route`: route replaced in place, research block kept coherent."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

_NAME = "2026-10-08-reroute.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_DOC = """schema: sizing-object
intent: "Run a benchmark"
estimate:
  tshirt: S
  provisional: true
route: {route}  # dispatch | spec-dispatch | research
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
{research}interaction_mode: ceo
"""
_RESEARCH = """research:
  value_class: spike
  appetite: medium
  sources:
  - web
  depth: deeper
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.record_path(str(tmp_path), "sizings", _NAME)).parent.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _sizing(repo: Path, route: str, research: str = "") -> tuple[Path, str]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DOC.format(route=route, research=research), encoding="utf-8")
    return path, path.relative_to(repo).as_posix()


def test_research_to_spec_dispatch_drops_block_and_validates(repo: Path) -> None:
    path, rel = _sizing(repo, "research", _RESEARCH)
    assert sa.main(["--amend-route", rel, "--route", "spec-dispatch"]) == sa.EXIT_OK
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["route"] == "spec-dispatch"
    assert "research" not in doc
    assert doc["status"] == "routed" and doc["interaction_mode"] == "ceo"
    assert validate_frontmatter(doc, _SCHEMA) == []


def test_non_research_route_change_leaves_record_alone(repo: Path) -> None:
    path, rel = _sizing(repo, "spec-dispatch")
    assert sa.main(["--amend-route", rel, "--route", "dispatch"]) == sa.EXIT_OK
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["route"] == "dispatch" and doc["intent"] == "Run a benchmark"


def test_unknown_route_is_refused_and_file_unchanged(repo: Path) -> None:
    path, rel = _sizing(repo, "research", _RESEARCH)
    before = path.read_text(encoding="utf-8")
    assert sa.main(["--amend-route", rel, "--route", "bogus"]) == sa.EXIT_BUSINESS_FAIL
    assert path.read_text(encoding="utf-8") == before


def test_entering_research_without_block_is_refused(repo: Path) -> None:
    path, rel = _sizing(repo, "spec-dispatch")
    before = path.read_text(encoding="utf-8")
    assert sa.main(["--amend-route", rel, "--route", "research"]) == sa.EXIT_BUSINESS_FAIL
    assert path.read_text(encoding="utf-8") == before


def test_entering_research_with_block_is_accepted(repo: Path) -> None:
    path, rel = _sizing(repo, "spec-dispatch", _RESEARCH)
    assert sa.main(["--amend-route", rel, "--route", "research"]) == sa.EXIT_OK
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["route"] == "research" and doc["research"]["value_class"] == "spike"


def test_same_route_is_refused(repo: Path) -> None:
    _, rel = _sizing(repo, "spec-dispatch")
    assert sa.main(["--amend-route", rel, "--route", "spec-dispatch"]) == sa.EXIT_BUSINESS_FAIL


def test_missing_route_flag_is_usage_error(repo: Path) -> None:
    _, rel = _sizing(repo, "research", _RESEARCH)
    assert sa.main(["--amend-route", rel]) == sa.EXIT_USAGE


def test_path_outside_sizings_is_refused(repo: Path) -> None:
    (repo / "x.yaml").write_text("route: plan\n", encoding="utf-8")
    assert sa.main(["--amend-route", "x.yaml", "--route", "dispatch"]) == sa.EXIT_BUSINESS_FAIL


def test_help_lists_amend_route(capsys: pytest.CaptureFixture[str]) -> None:
    sa.main(["--help"])
    out = capsys.readouterr()
    assert "--amend-route" in out.out + out.err


@pytest.mark.parametrize(
    "extra",
    [
        ["--research-class", "spike"],
        ["--research-source", "web"],
        ["--exit-criterion", "x"],
        ["--amend-exit-criterion", "other.yaml"],
        ["--amend-research", "other.yaml"],
        ["--tshirt", "S"],
    ],
)
def test_other_mode_flags_with_amend_route_are_usage_errors_and_change_nothing(repo: Path, extra: list[str]) -> None:
    path, rel = _sizing(repo, "research", _RESEARCH)
    before = path.read_text(encoding="utf-8")
    assert sa.main(["--amend-route", rel, "--route", "spec-dispatch", *extra]) == sa.EXIT_USAGE
    assert path.read_text(encoding="utf-8") == before
