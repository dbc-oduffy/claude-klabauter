"""apply_in_run_resize: a reported t-shirt continues under the step rule or flags."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import sizing_fire as sf

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "state/sizings/20260101-a.yaml"
_ROUTE = {"XS": "dispatch", "S": "spec-dispatch", "M": "plan", "L": "plan", "XL": "plan"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _seed(tmp_path: Path, tshirt: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    path = repo / REL
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                "intent: Test intent, verbatim.",
                "estimate:",
                f"  tshirt: {tshirt}",
                "  provisional: true",
                f"route: {_ROUTE[tshirt]}",
                "detents: []",
                "fork: null",
                "xl_exit: null",
                "status: routed",
                "premise:",
                "  provenance: read",
                "  evidence: test fixture, no real premise verified",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo


def _apply(repo: Path, new: str):
    sizing = sf.load_sizing(repo, REL)
    return sf.apply_in_run_resize(sizing, REL, new, repo, "planner reported a larger change")


@pytest.mark.parametrize(
    "old,new,arm",
    [("S", "L", sf.ARM_M_PLUS), ("M", "XL", sf.ARM_M_PLUS), ("XS", "L", sf.ARM_M_PLUS), ("XS", "S", sf.ARM_S)],
)
def test_continue_amends_sizing_and_re_resolves_arm(tmp_path, old, new, arm):
    repo = _seed(tmp_path, old)
    got_arm, amended, _ = _apply(repo, new)
    assert got_arm == arm
    assert amended["estimate"]["tshirt"] == new
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["estimate"]["tshirt"] == new


@pytest.mark.parametrize("old,new", [("XS", "XL"), ("S", "XXL"), ("L", "XXL"), ("M", "XXL")])
def test_flag_refuses_naming_possible_engine_error_and_leaves_sizing(tmp_path, old, new):
    repo = _seed(tmp_path, old)
    with pytest.raises(sf.SizingFireRefused, match="possible engine error"):
        _apply(repo, new)
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["estimate"]["tshirt"] == old


def test_xl_to_xxl_continues_with_xxl_detents_raised(tmp_path):
    repo = _seed(tmp_path, "XL")
    arm, amended, detents = _apply(repo, "XXL")
    assert amended["estimate"]["tshirt"] == "XXL"
    assert arm == sf.resolve_arm(amended)
    assert "xxl_unprobed" in detents and "xxl_infra_check_owed" in detents


def test_unchanged_size_is_a_no_op(tmp_path):
    repo = _seed(tmp_path, "M")
    before = (repo / REL).read_text(encoding="utf-8")
    arm, _, detents = _apply(repo, "M")
    assert arm == sf.ARM_M_PLUS and detents == ()
    assert (repo / REL).read_text(encoding="utf-8") == before
