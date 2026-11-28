"""The git-native publish interface is unbuilt, and the cost falsifier is live at base."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "coordinator" / "lib"))

from coordinator_core.benchmarks import percolate_publish_git_native_cost as bench  # noqa: E402
from percolate import git_tree_publish as gtp  # noqa: E402

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process]


def _spec(tmp_path: Path) -> gtp.PublishTreeSpec:
    return gtp.PublishTreeSpec(
        mirror_git_dir=tmp_path / "mirror" / ".git",
        source_repo=tmp_path / "src",
        pinned_sha="a" * 40,
        row_prefix="coordinator_core",
        ignore=None,  # type: ignore[arg-type]
        transform_signature="sig",
        base_commit=None,
    )


def test_the_builder_is_unbuilt_until_c1(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        gtp.build_publish_commit(_spec(tmp_path), transform=lambda _path, data: data)


def test_the_interface_records_are_frozen_and_carry_exactly_the_agreed_fields(tmp_path: Path) -> None:
    assert [f.name for f in dataclasses.fields(gtp.PublishTreeSpec)] == [
        "mirror_git_dir", "source_repo", "pinned_sha", "row_prefix", "ignore", "transform_signature", "base_commit",
    ]
    assert [f.name for f in dataclasses.fields(gtp.PublishCommit)] == ["commit_sha", "tree_sha", "changed", "removed"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _spec(tmp_path).pinned_sha = "b" * 40  # type: ignore[misc]


def test_the_source_change_touches_exactly_the_requested_files(tmp_path: Path) -> None:
    fixture = bench.build_fixture(tmp_path / "fx", 40)
    before = fixture["sha"]
    after = bench.change_source(fixture["src"], 3, fixture["plugins"])
    diff = subprocess.run(
        ["git", "-C", str(fixture["src"]), "diff", "--name-only", before, after],
        capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.split()
    assert len(diff) == 3 and all(name.endswith(".py") for name in diff)


def test_the_falsifier_is_live_at_base_and_its_gate_caches_warm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = bench.main(["--json", "--files", "60", "--changed", "3", "--workdir", str(tmp_path / "round")])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    cold, warm = result["cold"], result["warm"]
    for row in (cold, warm):
        assert row["per_file_worktree_writes"] > 0, "the base copies every file into the staging tree"
        assert row["git_spawns"] > 0 and row["publish_cpu_s"] > 0 and row["reference_fetch_checkout_cpu_s"] > 0
    assert result["falsifier_live"] is True
    assert cold["ast_parse_calls"] > 0
    assert warm["ast_parse_calls"] < cold["ast_parse_calls"], "a warm round re-parses only what changed"
    assert warm["changed_files"] < cold["changed_files"]
