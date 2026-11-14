"""Pins the fleet Temp layout paths and the reserved ``_fleet`` repo name."""

from __future__ import annotations

import atexit
import os
import re
import stat
import subprocess
import sys
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


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_managed_temp_dir_is_pid_stamped_and_removed_on_exit(fake_temp):
    with temp_layout.managed_temp_dir("probe", "/work/my-repo") as d:
        assert d.parent == fake_temp / "coordinator" / "my-repo" / temp_layout.RUN_TEMP_DIRNAME
        assert re.fullmatch(rf"{os.getpid()}-[0-9a-f]{{6}}-probe", d.name)
        (d / "f.txt").write_text("x")
    assert not d.exists()


def test_managed_temp_dir_removed_when_body_raises(fake_temp):
    with pytest.raises(RuntimeError):
        with temp_layout.managed_temp_dir("probe") as d:
            raise RuntimeError("boom")
    assert not d.exists()


def test_managed_temp_dir_removes_read_only_contents(fake_temp):
    with temp_layout.managed_temp_dir("ro") as d:
        f = d / "ro.txt"
        f.write_text("x")
        os.chmod(f, stat.S_IREAD)
    assert not d.exists()


def test_next_creation_reaps_dead_owner_dir_and_keeps_live_and_foreign(fake_temp):
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


def test_make_managed_temp_dir_registers_atexit_removal(fake_temp, monkeypatch):
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    d = temp_layout.make_managed_temp_dir("handoff")
    assert d.is_dir() and len(registered) == 1
    registered[0]()
    assert not d.exists()


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


def test_kept_dir_is_not_reaped_and_registers_no_atexit(fake_temp, monkeypatch):
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    kept, cleanup = temp_layout.create_managed_temp_dir("keep")
    with temp_layout.managed_temp_dir("later"):
        assert kept.is_dir()
    assert not registered
    cleanup()
    assert not kept.exists()


def test_kept_dir_is_reaped_only_once_older_than_the_age_bar(fake_temp):
    run_temp = temp_layout.managed_tempdir() / temp_layout.RUN_TEMP_DIRNAME
    run_temp.mkdir()
    fresh, stale = run_temp / "kept-aaaaaa-fresh", run_temp / "kept-bbbbbb-stale"
    fresh.mkdir()
    stale.mkdir()
    old = stale.stat().st_mtime - temp_layout._KEPT_MAX_AGE_S - 60
    os.utime(stale, (old, old))
    with temp_layout.managed_temp_dir("new"):
        assert fresh.is_dir()
        assert not stale.exists()


def test_reap_is_capped_per_creation(fake_temp):
    run_temp = temp_layout.managed_tempdir() / temp_layout.RUN_TEMP_DIRNAME
    run_temp.mkdir()
    dead_pid = _dead_pid()
    n = temp_layout._REAP_CAP + 3
    for i in range(n):
        (run_temp / f"{dead_pid}-{i:06x}-old").mkdir()
    with temp_layout.managed_temp_dir("new"):
        left = [p for p in run_temp.iterdir() if p.name.endswith("-old")]
    assert len(left) == 3


def test_teardown_error_does_not_replace_body_exception(fake_temp, monkeypatch):
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(ValueError, match="body"):
        with temp_layout.managed_temp_dir("x"):
            raise ValueError("body")


def test_mkdtemp_in_creates_under_chosen_parent(tmp_path):
    d = temp_layout.mkdtemp_in("pre-", tmp_path)
    assert d.parent == tmp_path and d.name.startswith("pre-") and d.is_dir()


def test_strict_managed_temp_dir_raises_naming_leaked_path_on_failed_removal(fake_temp, monkeypatch):
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(OSError, match="leaked") as ei:
        with temp_layout.managed_temp_dir("strict-probe-", strict=True) as d:
            pass
    assert str(d) in str(ei.value)
    assert isinstance(ei.value.__cause__, PermissionError)


def test_non_strict_managed_temp_dir_swallows_failed_removal(fake_temp, monkeypatch):
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with temp_layout.managed_temp_dir("lenient-probe-"):
        pass


def test_strict_never_masks_body_exception_and_notes_the_leak(fake_temp, monkeypatch):
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(ValueError, match="body") as ei:
        with temp_layout.managed_temp_dir("strict-body-", strict=True) as d:
            raise ValueError("body")
    assert any(str(d) in n for n in ei.value.__notes__)


def test_strict_clean_removal_is_silent(fake_temp):
    with temp_layout.managed_temp_dir("strict-ok-", strict=True) as d:
        assert d.is_dir()
    assert not d.exists()


def test_mkdtemp_in_none_parent_is_managed_run_temp_never_bare_temp(fake_temp):
    d = temp_layout.mkdtemp_in("nop-", None)
    run_temp = fake_temp / "coordinator" / "_fleet" / "run-temp"
    assert d.parent == run_temp
    assert d.name.startswith(f"{os.getpid()}-") and d.name.endswith("-nop-")
    assert d.parent != fake_temp
    temp_layout._rmtree_strict(d)


def test_temp_dir_in_none_parent_lands_under_managed_root(fake_temp):
    with temp_layout.temp_dir_in("tdn-", None) as d:
        assert d.parent == fake_temp / "coordinator" / "_fleet" / "run-temp"
    assert not d.exists()


def test_managed_dir_does_not_renest_the_skeleton_inside_a_session_dir(fake_temp, monkeypatch):
    session = fake_temp / "coordinator" / "_fleet" / "run-temp" / "1-abcdef-pytest-session"
    session.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(session))
    made = temp_layout.make_managed_temp_dir("x-")
    assert made.parent == session
    assert not (session / "coordinator").exists()


def test_managed_dir_outside_a_session_dir_still_uses_the_skeleton(fake_temp):
    made = temp_layout.make_managed_temp_dir("y-")
    assert made.parent == fake_temp / "coordinator" / "_fleet" / "run-temp"
