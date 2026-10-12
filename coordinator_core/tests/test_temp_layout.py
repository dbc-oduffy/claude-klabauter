"""Pins the fleet Temp layout paths and the reserved ``_fleet`` repo name."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coordinator_core import temp_layout


@pytest.fixture
def fake_temp(tmp_path, monkeypatch):
    base = tmp_path / "systemp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    return base


def test_coordinator_temp_root_is_two_segments_under_gettempdir(fake_temp):
    assert temp_layout.coordinator_temp_root(Path("/work/my-repo")) == fake_temp / "coordinator" / "my-repo"


def test_fleet_temp_root(fake_temp):
    assert temp_layout.fleet_temp_root() == fake_temp / "coordinator" / "_fleet"


def test_pytest_basetemp_root(fake_temp):
    assert temp_layout.pytest_basetemp_root("/work/my-repo") == fake_temp / "coordinator" / "my-repo" / "pytest"


def test_repo_named_fleet_is_refused(fake_temp):
    with pytest.raises(ValueError):
        temp_layout.coordinator_temp_root(Path("/work/_fleet"))
    with pytest.raises(ValueError):
        temp_layout.pytest_basetemp_root(Path("/work/_fleet"))


def test_kept_marker_name():
    assert temp_layout.PYTEST_KEPT_MARKER == ".coordinator-kept-failing"


@pytest.mark.parametrize(
    "name,expected",
    [("1234-abcdef", 1234), ("0-000000", 0), ("1234-ABCDEF", None), ("1234-abcde", None),
     ("1234-abcdefg", None), ("x-abcdef", None), ("1234-abcdef\n", None), ("", None),
     (".coordinator-kept-failing", None)],
)
def test_parse_pytest_run_dir(name, expected):
    assert temp_layout.parse_pytest_run_dir(name) == expected


def test_resolver_reads_gettempdir_at_call_time(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(first))
    assert temp_layout.fleet_temp_root().parent.parent == first
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(second))
    assert temp_layout.fleet_temp_root().parent.parent == second


def test_managed_tempdir_fleet_and_repo_are_created(fake_temp):
    fleet = temp_layout.managed_tempdir()
    assert fleet == fake_temp / "coordinator" / "_fleet" and fleet.is_dir()
    repo = temp_layout.managed_tempdir("/work/my-repo")
    assert repo == fake_temp / "coordinator" / "my-repo" and repo.is_dir()
    assert temp_layout.managed_tempdir("/work/my-repo") == repo


def test_managed_tempdir_refuses_fleet_named_repo(fake_temp):
    with pytest.raises(ValueError):
        temp_layout.managed_tempdir("/work/_fleet")


def test_legacy_top_level_path_joins_gettempdir(fake_temp):
    assert temp_layout.legacy_top_level_path("rt-bark-once-s1") == fake_temp / "rt-bark-once-s1"


def test_run_temp_dirname():
    assert temp_layout.RUN_TEMP_DIRNAME == "run-temp"


_GLOB_EXAMPLES = {
    "git-pathspec-[0-9]*.txt": "git-pathspec-123-abc.txt",
    "git-index-[0-9]*": "git-index-123-abc",
    "coordinator-pending-dispatch-*": "coordinator-pending-dispatch-sid-ab12cd34.json",
    "coordinator-nudged-*": "coordinator-nudged-sid-ab12cd34",
    "coordinator-dispatch-nudge-*": "coordinator-dispatch-nudge-ok-sid",
    "coordinator-guard-unlock-*": "coordinator-guard-unlock-sid",
    "coordinator-probe-spray-*": "coordinator-probe-spray-sid",
    "advisory-hook-state-*": "advisory-hook-state-sid.json",
    "first-agent-dispatch-advisory-*": "first-agent-dispatch-advisory-sid",
    "rt-bark-once-*": "rt-bark-once-sid",
    "group-em-watch-*": "group-em-watch-sid",
    "workflow-run-*.json": "workflow-run-sid-task.json",
    "em-poll-count-*": "em-poll-count-sid.json",
    "hand-written-plan-nudge-*": "hand-written-plan-nudge-sid-digest",
    "autonomous-run-*": "autonomous-run-sid",
    "pickup-autofire-events.jsonl": "pickup-autofire-events.jsonl",
    "coordinator-bt-python3-invocation-cache.json": "coordinator-bt-python3-invocation-cache.json",
    "coordinator-gitless-advisory-dedupe": "coordinator-gitless-advisory-dedupe",
    "coordinator-fleet-no-repo-root": "coordinator-fleet-no-repo-root",
    "backfill-initiative-fk.lock": "backfill-initiative-fk.lock",
    "coordinator-falsifier": "coordinator-falsifier",
    "bash-dispatch-probe-root": "bash-dispatch-probe-root",
    "altlive-*": "altlive-repo-ab12cd34",
    "sgc-cold-*": "sgc-cold-e2e-ab12cd34",
    "fmhook-test-*": "fmhook-test-ab12cd34",
    "fmhook-lineage-test-*": "fmhook-lineage-test-ab12cd34",
    "runtime-tripwire-em-dispatch-default-cursor-*": "runtime-tripwire-em-dispatch-default-cursor-sid",
    "land_pull_test_*": "land_pull_test_ab12cd34",
    "c[0-9]-census-fixture-*": "c1-census-fixture-ab12cd34",
    "hook-census-fixture-*": "hook-census-fixture-ab12cd34",
    "claude-klabauter-throwaway-tree-*": "claude-klabauter-throwaway-tree-ab12cd34",
    "claude-klabauter-publish-materialize-*": "claude-klabauter-publish-materialize-ab12cd34",
}


def test_every_legacy_glob_has_a_matching_example():
    assert set(_GLOB_EXAMPLES) == set(temp_layout.LEGACY_TOP_LEVEL_GLOBS)
    for example in _GLOB_EXAMPLES.values():
        assert temp_layout.is_legacy_top_level(example), example


@pytest.mark.parametrize("name", ["coordinator", "pytest-of-x", "random.txt", "git-pathspec", "coordinator-falsifier-x", "tmpab12cd34", ".tmpab12cd", "c10-census-fixture-x", "fmhook-claude-root-x", "ddct8c0-scratch", "git-index-foreign", "git-pathspec-tool.txt", "workflow-run-logs"])
def test_non_legacy_names_are_not_matched(name):
    assert not temp_layout.is_legacy_top_level(name)
