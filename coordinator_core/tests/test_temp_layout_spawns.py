"""Pins the fleet Temp layout paths and the reserved ``_fleet`` repo name."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core import temp_layout

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def fake_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "systemp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    return base


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_next_creation_reaps_dead_owner_dir_and_keeps_live_and_foreign(fake_temp) -> None:
    run_temp = temp_layout.managed_tempdir() / temp_layout.RUN_TEMP_DIRNAME
    run_temp.mkdir()
    dead = run_temp / f"{_dead_pid()}-abcdef-old"
    live = run_temp / f"{os.getppid()}-abcdef-live"
    foreign = run_temp / "not-ours"
    for p in (dead, live, foreign):
        (p / "sub").mkdir(parents=True)
    with temp_layout.managed_temp_dir("new"):
        assert not dead.exists()
        assert live.exists() and foreign.exists()


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


def test_reap_is_capped_per_creation(fake_temp) -> None:
    run_temp = temp_layout.managed_tempdir() / temp_layout.RUN_TEMP_DIRNAME
    run_temp.mkdir()
    dead_pid = _dead_pid()
    n = temp_layout._REAP_CAP + 3
    for i in range(n):
        (run_temp / f"{dead_pid}-{i:06x}-old").mkdir()
    with temp_layout.managed_temp_dir("new"):
        left = [p for p in run_temp.iterdir() if p.name.endswith("-old")]
    assert len(left) == 3


_MOUNTINFO_TMPFS_TMP = (
    "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    "30 20 0:25 / /tmp rw - tmpfs tmpfs rw\n"
    "31 20 8:1 /v /var/tmp rw - ext4 /dev/sda1 rw\n"
    r"32 20 0:26 / /mnt/with\040space rw - tmpfs tmpfs rw" "\n"
)
