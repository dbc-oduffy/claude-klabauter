"""Falsifier: replay of example-stats-repo' 2026-10-07 all-requirements sizing shape.

The fixture is a copy (header quotes the source refs); no test reads a sibling checkout.
Run: python -m pytest coordinator_core/ops/tests/test_requirement_register_fifa_replay.py -q -p no:cacheprovider
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.sizing_ship as ship_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

FIXTURE = Path(__file__).parent / "fixtures" / "requirement_register" / (
    "2026-10-07-all-requirements-and-roadmap-remainder.yaml"
)
PLAN = "docs/plans/2026-10-07-fifa-gap-closure.md"
RULING = {"source": "pm", "quote": "defer it", "ref": "ruling-1", "on": "2026-10-09"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    return root


def seed(repo: Path, *, route: str | None = None, fix: dict | None = None) -> Path:
    """The replayed sizing in `repo/state/sizings`; `fix` maps row id to field overrides."""
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    if route:
        data["route"] = route
    for row in data["requirement_register"]["rows"]:
        row.update((fix or {}).get(row["id"], {}))
    path = repo / "state" / "sizings" / "2026-10-07-all-requirements-and-roadmap-remainder.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


ALL_SHIPPABLE = {
    "R-partial": {"status": "met", "wired": True},
    "R-unreachable": {"wired": True},
    "R-unclaimed": {"status": "met", "wired": True, "claimed_by": [PLAN]},
    "R-deferred": {"ruling": RULING},
}


def _ship(repo: Path, path: Path) -> dict:
    return ship_mod._handler({"sizing_path": str(path)}, repo_root=repo)


def _cascade(repo: Path, path: Path):
    return cascade_mod._advance_one_sizing(path, PLAN, repo / ".git")


def _status(path: Path) -> str:
    return read_fm_field_unquoted(path.read_text(encoding="utf-8"), "status")


@pytest.mark.parametrize("route", [None, "plan"])
def test_sizing_ship_refuses_the_replayed_register_naming_the_rollup(repo, route):
    path = seed(repo, route=route, fix=ALL_SHIPPABLE)
    result = _ship(repo, path)
    assert result["exit_code"] == 1 and not result["applied"]
    assert "requirement register" in result["error"] and "rollup" in result["error"]
    assert _status(path) == "routed"


@pytest.mark.parametrize("row,needle", [
    ("R-partial", "partial"),
    ("R-unreachable", "met but not wired"),
    ("R-unclaimed", "open"),
    ("R-deferred", "without a recorded ruling"),
])
def test_cascade_refuses_naming_the_blocking_row(repo, row, needle):
    fix = {k: v for k, v in ALL_SHIPPABLE.items() if k != row}
    path = seed(repo, route="plan", fix=fix)
    advanced, refusal = _cascade(repo, path)
    assert advanced is False
    assert f"row {row}" in refusal and needle in refusal
    assert _status(path) == "routed"
    if row != "R-deferred":  # a ruling-less deferral is schema-invalid, so nothing is written
        assert read_fm_field_unquoted(path.read_text(encoding="utf-8"), "plan") == PLAN


def test_cascade_ships_once_every_row_is_met_and_wired_or_ruled_deferred(repo):
    path = seed(repo, route="plan", fix=ALL_SHIPPABLE)
    advanced, refusal = _cascade(repo, path)
    assert (advanced, refusal) == (True, None)
    assert _status(path) == "shipped"
