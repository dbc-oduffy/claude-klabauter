"""A re-sized ask's new sizing carries the prior sizing's recorded design_route approval; nothing is invented."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.ops.dispatch_emit import design_route as dr
from coordinator_core.session import record_homes

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
_APPROVED = {
    "route": "canvas",
    "authority": "g-em",
    "board_url": "https://claude.ai/artifact/abc123",
    "approval": {"by": "pm", "at": "2026-11-05T10:00:00Z"},
}


def _path(repo: Path, name: str) -> Path:
    path = Path(record_homes.record_path(str(repo), "sizings", name))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _prior(repo: Path, name: str, block: object) -> None:
    doc = yaml.safe_load(_DRAFT)
    doc["design_route"] = block
    _path(repo, name).write_text(yaml.safe_dump(doc), encoding="utf-8")


def _resize(repo: Path, name: str, *extra: str) -> dict:
    path = _path(repo, name)
    path.write_text(_DRAFT, encoding="utf-8")
    rel = path.relative_to(repo).as_posix()
    assert sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, "--json", *extra]) == sa.EXIT_OK
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_resize_without_flag_carries_recorded_approval(repo: Path) -> None:
    _prior(repo, "2026-11-05-site-e2c488.yaml", _APPROVED)
    doc = _resize(repo, "2026-11-06-site-build-e2c488.yaml")
    assert doc["design_route"] == _APPROVED
    payload, refusal = dr.read(doc, "state/sizings/2026-11/2026-11-06-site-build-e2c488.yaml")
    assert refusal is None and payload["approved"] is True
    assert dr.stage_refusal(payload) is None


def test_resize_with_same_route_flag_carries_approval(repo: Path) -> None:
    _prior(repo, "2026-11-05-site-e2c488.yaml", _APPROVED)
    doc = _resize(repo, "2026-11-06-site-build-e2c488.yaml", "--design-route", "canvas")
    assert doc["design_route"]["approval"] == _APPROVED["approval"]
    assert doc["design_route"]["board_url"] == _APPROVED["board_url"]
    assert doc["design_route"]["authority"] == "g-em"


def test_other_asks_sizing_never_donates(repo: Path) -> None:
    _prior(repo, "2026-11-05-site-aaaaaa.yaml", _APPROVED)
    doc = _resize(repo, "2026-11-06-site-build-e2c488.yaml")
    assert "design_route" not in doc
    doc = _resize(repo, "2026-11-06-site-build-bbbbbb.yaml", "--design-route", "canvas")
    assert doc["design_route"]["approval"] is None


def test_unapproved_prior_invents_nothing(repo: Path) -> None:
    _prior(repo, "2026-11-05-site-e2c488.yaml", {**_APPROVED, "approval": None, "board_url": None})
    doc = _resize(repo, "2026-11-06-site-build-e2c488.yaml")
    assert "design_route" not in doc


def test_changed_route_does_not_carry(repo: Path) -> None:
    _prior(repo, "2026-11-05-site-e2c488.yaml", _APPROVED)
    doc = _resize(repo, "2026-11-06-site-build-e2c488.yaml", "--design-route", "reviewer")
    assert doc["design_route"] == {"route": "reviewer", "authority": None, "board_url": None, "approval": None}
