"""lost_definition_leg_profile: the lost-definition leg stays under the bar and adds no spawn."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import lost_definition_leg_profile as lp


def test_lost_definition_leg_under_bar_and_adds_no_spawn(tmp_path: Path) -> None:
    base, end = lp.profile(tmp_path / "repo")
    assert base["leg"] == "import_closure_findings" and end["present"] is True
    assert base["findings"] == lp.IMPORTERS and end["findings"] == lp.IMPORTERS
    assert base["process_time_ms"] < lp.BAR_MS and end["process_time_ms"] < lp.BAR_MS
    assert end["spawns"] <= base["spawns"]


def test_leg_refuses_when_the_target_is_landed(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    root = tmp_path / "repo"
    paths = lp.build_fixture(root)
    targets = [f"{lp.PKG}/m{m}.py" for m in range(lp.MODULES)]
    assert li.lost_definition_findings(root, paths, []) != []
    assert li.lost_definition_findings(root, paths + targets, [], carried=()) == []
