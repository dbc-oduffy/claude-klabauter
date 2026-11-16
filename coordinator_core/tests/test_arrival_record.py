"""Tests for coordinator_core.arrival_record."""

import shutil
import subprocess
import sys

import pytest

from coordinator_core import arrival_record as ar
from coordinator_core.session_hub_path import hub_for_common
from pathlib import Path


def test_append_then_has_arrived(tmp_path: Path) -> None:
    ar.append_arrival(tmp_path, "s1", "a1", "coordinator-executor", "2026-10-22T00:00:00Z")
    assert ar.has_arrived(tmp_path, "s1", "a1")
    line = ar.arrival_log_path(tmp_path, "s1").read_text().strip()
    assert line == '{"agent_id":"a1","agent_type":"coordinator-executor","stopped_at":"2026-10-22T00:00:00Z"}'


def test_path_shape(tmp_path: Path) -> None:
    assert ar.arrival_log_path(tmp_path, "s1") == hub_for_common(tmp_path) / "s1" / "subagent-arrivals.jsonl"


def test_survives_subagents_dir_deletion(tmp_path: Path) -> None:
    sub = hub_for_common(tmp_path) / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "x").write_text("x")
    ar.append_arrival(tmp_path, "s1", "a1", "t", "now")
    shutil.rmtree(sub)
    assert ar.has_arrived(tmp_path, "s1", "a1")


def test_unknown_agent_and_missing_file_false(tmp_path: Path) -> None:
    assert not ar.has_arrived(tmp_path, "s1", "a1")
    ar.append_arrival(tmp_path, "s1", "a1", "t", "now")
    assert not ar.has_arrived(tmp_path, "s1", "a2")
    assert not ar.has_arrived(tmp_path, "s1", "a")


def test_append_is_unconditional(tmp_path: Path) -> None:
    for _ in range(2):
        ar.append_arrival(tmp_path, "s1", "a1", "t", "now")
    assert len(ar.arrival_log_path(tmp_path, "s1").read_text().splitlines()) == 2


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../x", "a\x00b"])
def test_unsafe_ids_refused(tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError):
        ar.arrival_log_path(tmp_path, bad)
    with pytest.raises(ValueError):
        ar.append_arrival(tmp_path, "s1", bad, "t", "now")
    with pytest.raises(ValueError):
        ar.has_arrived(tmp_path, "s1", bad)
    assert not hub_for_common(tmp_path).exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_does_not_pull_hooks() -> None:
    code = "import sys, coordinator_core.arrival_record; sys.exit(1 if 'coordinator_core.hooks' in sys.modules else 0)"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert subprocess.run([sys.executable, "-c", code], creationflags=flags).returncode == 0
