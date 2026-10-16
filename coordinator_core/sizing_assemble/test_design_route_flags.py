"""Tests for `sizing-assemble --design-route`: the flag writes a schema-valid `design_route` block."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

_NAME = "2026-10-08-d.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_DRAFT = """schema: sizing-object
intent: "Design the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _path(repo: Path) -> Path:
    return Path(record_homes.record_path(str(repo), "sizings", _NAME))


def _run(repo: Path, draft: str | None, *extra: str) -> tuple[int, dict]:
    path = _path(repo)
    if draft is not None:
        path.write_text(draft, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, "--json", *extra])
    return code, yaml.safe_load(path.read_text(encoding="utf-8"))


def _valid(doc: dict) -> list:
    return validate_frontmatter(doc, _SCHEMA)


@pytest.mark.parametrize("route", ["canvas", "reviewer", "none"])
def test_route_writes_four_key_block(repo: Path, route: str) -> None:
    code, doc = _run(repo, _DRAFT, "--design-route", route)
    assert code == sa.EXIT_OK
    assert doc["design_route"] == {"route": route, "authority": None, "board_url": None, "approval": None}
    assert _valid(doc) == []


def test_no_flag_writes_no_key(repo: Path) -> None:
    code, doc = _run(repo, _DRAFT)
    assert code == sa.EXIT_OK
    assert "design_route" not in doc


def test_unknown_route_refuses_and_writes_nothing(repo: Path, capsys: pytest.CaptureFixture) -> None:
    code, doc = _run(repo, _DRAFT, "--design-route", "bogus")
    assert code == sa.EXIT_USAGE
    assert "--design-route must be one of canvas|reviewer|none" in capsys.readouterr().err
    assert "design_route" not in doc
    assert doc["route"] == "dispatch"


def test_requires_write(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert sa.main(["--tshirt", "XS", "--design-route", "canvas"]) == sa.EXIT_USAGE
    assert "requires --write" in capsys.readouterr().err


def test_same_route_rerun_keeps_prior_approval(repo: Path) -> None:
    _run(repo, _DRAFT, "--design-route", "canvas")
    path = _path(repo)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["design_route"]["board_url"] = "https://example.test/board"
    doc["design_route"]["approval"] = {"by": "pm", "at": "2026-10-17T00:00:00Z"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    code, after = _run(repo, None, "--design-route", "canvas")
    assert code == sa.EXIT_OK
    assert after["design_route"]["approval"] == {"by": "pm", "at": "2026-10-17T00:00:00Z"}
    assert after["design_route"]["board_url"] == "https://example.test/board"
    assert _valid(after) == []


def test_route_change_drops_prior_approval(repo: Path) -> None:
    _run(repo, _DRAFT, "--design-route", "canvas")
    path = _path(repo)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["design_route"]["board_url"] = "https://example.test/board"
    doc["design_route"]["approval"] = {"by": "pm", "at": "2026-10-17T00:00:00Z"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    code, after = _run(repo, None, "--design-route", "reviewer")
    assert code == sa.EXIT_OK
    assert after["design_route"] == {"route": "reviewer", "authority": None, "board_url": None, "approval": None}
