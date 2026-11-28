"""Pins the fleet Temp layout paths and the reserved ``_fleet`` repo name."""

from __future__ import annotations

import atexit
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core import temp_layout


@pytest.fixture
def fake_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "systemp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    return base


def test_coordinator_temp_root_is_two_segments_under_gettempdir(fake_temp) -> None:
    assert temp_layout.coordinator_temp_root(Path("/work/my-repo")) == fake_temp / "coordinator" / "my-repo"


def test_fleet_temp_root(fake_temp) -> None:
    assert temp_layout.fleet_temp_root() == fake_temp / "coordinator" / "_fleet"


def test_pytest_basetemp_root(fake_temp) -> None:
    assert temp_layout.pytest_basetemp_root("/work/my-repo") == fake_temp / "coordinator" / "my-repo" / "pytest"


def test_repo_named_fleet_is_refused(fake_temp) -> None:
    with pytest.raises(ValueError):
        temp_layout.coordinator_temp_root(Path("/work/_fleet"))
    with pytest.raises(ValueError):
        temp_layout.pytest_basetemp_root(Path("/work/_fleet"))


def test_kept_marker_name() -> None:
    assert temp_layout.PYTEST_KEPT_MARKER == ".coordinator-kept-failing"


def _run_dirs(root: Path, count: int, marker: str | None = None) -> list[Path]:
    dirs = []
    for i in range(count):
        d = root / f"{4000 + i}-aaaaa{i}"
        d.mkdir(parents=True)
        (d / "f").write_text("x")
        stamp = d
        if marker:
            stamp = d / marker
            stamp.touch()
        os.utime(stamp, (1000 + i, 1000 + i))
        dirs.append(d)
    return dirs


def test_retain_latest_keeps_newest_two_of_three_kept_runs(tmp_path: Path) -> None:
    oldest, middle, newest = _run_dirs(tmp_path, 3, temp_layout.PYTEST_KEPT_MARKER)
    moved = temp_layout.retain_latest(tmp_path, keep=2, marker=temp_layout.PYTEST_KEPT_MARKER)
    assert not oldest.exists()
    assert middle.is_dir() and newest.is_dir()
    assert moved == [tmp_path / temp_layout.REAP_DIRNAME / oldest.name]
    assert (moved[0] / "f").is_file()


def test_retain_latest_leaves_unmarked_and_foreign_entries(tmp_path: Path) -> None:
    kept = _run_dirs(tmp_path, 3, temp_layout.PYTEST_KEPT_MARKER)
    live = tmp_path / "9999-bbbbbb"
    live.mkdir()
    foreign = tmp_path / "notes"
    foreign.mkdir()
    temp_layout.retain_latest(tmp_path, keep=2, marker=temp_layout.PYTEST_KEPT_MARKER)
    assert live.is_dir() and foreign.is_dir()
    assert not kept[0].exists()


def test_retain_latest_without_marker_orders_by_dir_mtime_and_is_idempotent(tmp_path: Path) -> None:
    dirs = _run_dirs(tmp_path, 4)
    temp_layout.retain_latest(tmp_path)
    assert [d.exists() for d in dirs] == [False, False, True, True]
    assert temp_layout.retain_latest(tmp_path) == []


def test_retain_latest_on_missing_root_is_empty(tmp_path: Path) -> None:
    assert temp_layout.retain_latest(tmp_path / "absent") == []


@pytest.mark.parametrize(
    "name,expected",
    [("1234-abcdef", 1234), ("0-000000", 0), ("1234-ABCDEF", None), ("1234-abcde", None),
     ("1234-abcdefg", None), ("x-abcdef", None), ("1234-abcdef\n", None), ("", None),
     (".coordinator-kept-failing", None)],
)
def test_parse_pytest_run_dir(name, expected) -> None:
    assert temp_layout.parse_pytest_run_dir(name) == expected


def test_resolver_reads_gettempdir_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(first))
    assert temp_layout.fleet_temp_root().parent.parent == first
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(second))
    assert temp_layout.fleet_temp_root().parent.parent == second


def test_managed_tempdir_fleet_and_repo_are_created(fake_temp) -> None:
    fleet = temp_layout.managed_tempdir()
    assert fleet == fake_temp / "coordinator" / "_fleet" and fleet.is_dir()
    repo = temp_layout.managed_tempdir("/work/my-repo")
    assert repo == fake_temp / "coordinator" / "my-repo" and repo.is_dir()
    assert temp_layout.managed_tempdir("/work/my-repo") == repo


def test_managed_tempdir_refuses_fleet_named_repo(fake_temp) -> None:
    with pytest.raises(ValueError):
        temp_layout.managed_tempdir("/work/_fleet")


def test_legacy_top_level_path_joins_gettempdir(fake_temp) -> None:
    assert temp_layout.legacy_top_level_path("rt-bark-once-s1") == fake_temp / "rt-bark-once-s1"


def test_managed_temp_dir_is_pid_stamped_and_removed_on_exit(fake_temp) -> None:
    with temp_layout.managed_temp_dir("probe", "/work/my-repo") as d:
        assert d.parent == fake_temp / "coordinator" / "my-repo" / temp_layout.RUN_TEMP_DIRNAME
        assert re.fullmatch(rf"{os.getpid()}-[0-9a-f]{{6}}-probe", d.name)
        (d / "f.txt").write_text("x")
    assert not d.exists()


def test_managed_temp_dir_removed_when_body_raises(fake_temp) -> None:
    with pytest.raises(RuntimeError):
        with temp_layout.managed_temp_dir("probe") as d:
            raise RuntimeError("boom")
    assert not d.exists()


def test_managed_temp_dir_removes_read_only_contents(fake_temp) -> None:
    with temp_layout.managed_temp_dir("ro") as d:
        f = d / "ro.txt"
        f.write_text("x")
        os.chmod(f, stat.S_IREAD)
    assert not d.exists()


def test_make_managed_temp_dir_registers_atexit_removal(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    d = temp_layout.make_managed_temp_dir("handoff")
    assert d.is_dir() and len(registered) == 1
    registered[0]()
    assert not d.exists()


def test_run_temp_dirname() -> None:
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


def test_every_legacy_glob_has_a_matching_example() -> None:
    assert set(_GLOB_EXAMPLES) == set(temp_layout.LEGACY_TOP_LEVEL_GLOBS)
    for example in _GLOB_EXAMPLES.values():
        assert temp_layout.is_legacy_top_level(example), example


@pytest.mark.parametrize("name", ["coordinator", "pytest-of-x", "random.txt", "git-pathspec", "coordinator-falsifier-x", "tmpab12cd34", ".tmpab12cd", "c10-census-fixture-x", "fmhook-claude-root-x", "ddct8c0-scratch", "git-index-foreign", "git-pathspec-tool.txt", "workflow-run-logs"])
def test_non_legacy_names_are_not_matched(name) -> None:
    assert not temp_layout.is_legacy_top_level(name)


def test_kept_dir_is_not_reaped_and_registers_no_atexit(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    kept, cleanup = temp_layout.create_managed_temp_dir("keep")
    with temp_layout.managed_temp_dir("later"):
        assert kept.is_dir()
    assert not registered
    cleanup()
    assert not kept.exists()


def test_managed_temp_dir_carries_a_pid_owner_mark(fake_temp) -> None:
    from coordinator_core import owner_mark

    with temp_layout.managed_temp_dir("probe", "/work/my-repo") as d:
        mark = owner_mark.read_mark(d)
        assert mark is not None
        assert (mark["owner_kind"], mark["owner_id"]) == ("pid", str(os.getpid()))
        assert mark["repo"] == "/work/my-repo"
        assert owner_mark.owner_alive(mark) is True


def test_make_managed_temp_dir_carries_a_pid_owner_mark(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import owner_mark

    monkeypatch.setattr(atexit, "register", lambda fn, *a: None)
    mark = owner_mark.read_mark(temp_layout.make_managed_temp_dir("handoff"))
    assert mark is not None and mark["owner_id"] == str(os.getpid())


def test_kept_dir_mark_names_the_session_and_the_age_floor(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import owner_mark

    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    kept, cleanup = temp_layout.create_managed_temp_dir("keep")
    mark = owner_mark.read_mark(kept)
    assert mark is not None
    assert (mark["owner_kind"], mark["owner_id"]) == ("session", "11111111-2222-3333-4444-555555555555")
    assert mark["ttl_hours"] == temp_layout._KEPT_MAX_AGE_S / 3600
    cleanup()


def test_a_failing_stamp_never_fails_the_creation(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import owner_mark

    def boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(owner_mark, "write_mark", boom)
    with temp_layout.managed_temp_dir("probe") as d:
        assert d.is_dir() and owner_mark.read_mark(d) is None
    assert not d.exists()


def test_kept_dir_is_reaped_only_once_older_than_the_age_bar(fake_temp) -> None:
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


def test_teardown_error_does_not_replace_body_exception(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(ValueError, match="body"):
        with temp_layout.managed_temp_dir("x"):
            raise ValueError("body")


def test_mkdtemp_in_creates_under_chosen_parent(tmp_path: Path) -> None:
    d = temp_layout.mkdtemp_in("pre-", tmp_path)
    assert d.parent == tmp_path and d.name.startswith("pre-") and d.is_dir()


def test_mkdtemp_in_is_private_and_retries_on_name_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr("secrets.token_hex", lambda _n: next(tokens))
    first = temp_layout.mkdtemp_in("c-", tmp_path)
    second = temp_layout.mkdtemp_in("c-", tmp_path)
    assert first.name == f"c-{os.getpid()}-aaaaaa" and second.name == f"c-{os.getpid()}-bbbbbb"
    if os.name != "nt":
        assert stat.S_IMODE(first.stat().st_mode) == 0o700


def test_mkdtemp_in_chosen_parent_name_carries_the_owner_pid(tmp_path: Path) -> None:
    d = temp_layout.mkdtemp_in("own-", tmp_path)
    m = re.fullmatch(r"own-(\d+)-[0-9a-f]{6}", d.name)
    assert m is not None and int(m.group(1)) == os.getpid()


def test_mkdtemp_in_reaps_a_dead_owner_sibling_and_spares_live_and_foreign_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.group_em import session_registry

    dead, live = 2_000_001, 2_000_002
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == live)
    gone = tmp_path / f"own-{dead}-abcdef"
    kept_live = tmp_path / f"own-{live}-abcdef"
    other_prefix = tmp_path / f"zzz-{dead}-abcdef"
    unparseable = tmp_path / "own-notapid"
    for p in (gone, kept_live, other_prefix, unparseable):
        p.mkdir()
        (p / "f.txt").write_text("x")
    temp_layout.mkdtemp_in("own-", tmp_path)
    assert not gone.exists()
    assert kept_live.exists() and other_prefix.exists() and unparseable.exists()


def test_strict_managed_temp_dir_raises_naming_leaked_path_on_failed_removal(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(OSError, match="leaked") as ei:
        with temp_layout.managed_temp_dir("strict-probe-", strict=True) as d:
            pass
    assert str(d) in str(ei.value)
    assert isinstance(ei.value.__cause__, PermissionError)


def test_non_strict_managed_temp_dir_swallows_failed_removal(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with temp_layout.managed_temp_dir("lenient-probe-"):
        pass


def test_strict_never_masks_body_exception_and_notes_the_leak(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path):
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", boom)
    with pytest.raises(ValueError, match="body") as ei:
        with temp_layout.managed_temp_dir("strict-body-", strict=True) as d:
            raise ValueError("body")
    assert any(str(d) in n for n in ei.value.__notes__)


def test_strict_clean_removal_is_silent(fake_temp) -> None:
    with temp_layout.managed_temp_dir("strict-ok-", strict=True) as d:
        assert d.is_dir()
    assert not d.exists()


def test_mkdtemp_in_none_parent_is_managed_run_temp_never_bare_temp(fake_temp) -> None:
    d = temp_layout.mkdtemp_in("nop-", None)
    run_temp = fake_temp / "coordinator" / "_fleet" / "run-temp"
    assert d.parent == run_temp
    assert d.name.startswith(f"{os.getpid()}-") and d.name.endswith("-nop-")
    assert d.parent != fake_temp
    temp_layout._rmtree_strict(d)


def test_temp_dir_in_none_parent_lands_under_managed_root(fake_temp) -> None:
    with temp_layout.temp_dir_in("tdn-", None) as d:
        assert d.parent == fake_temp / "coordinator" / "_fleet" / "run-temp"
    assert not d.exists()


def test_temp_dir_in_none_parent_registers_no_atexit_and_removes_on_raise(
    fake_temp, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    with pytest.raises(RuntimeError):
        with temp_layout.temp_dir_in("tdr-", None) as d:
            (d / "f.txt").write_text("x")
            raise RuntimeError("boom")
    assert not d.exists()
    assert not registered


def test_temp_dir_in_chosen_parent_removed_on_both_exits(tmp_path: Path) -> None:
    with temp_layout.temp_dir_in("tdp-", tmp_path) as ok:
        assert ok.parent == tmp_path
    with pytest.raises(RuntimeError):
        with temp_layout.temp_dir_in("tdp-", tmp_path) as bad:
            raise RuntimeError("boom")
    assert not ok.exists() and not bad.exists()


def test_make_managed_temp_dir_atexit_swallows_a_failed_removal(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    d = temp_layout.make_managed_temp_dir("held")

    def refuse(_path: Path) -> None:
        raise PermissionError("held")

    monkeypatch.setattr(temp_layout, "_rmtree_strict", refuse)
    registered[0]()
    assert d.is_dir()


def test_only_a_kept_entry_survives_every_creator_on_both_exits(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a: registered.append(fn))
    kept, _cleanup = temp_layout.create_managed_temp_dir("keep", "/work/my-repo")
    run_temp = kept.parent

    with temp_layout.managed_temp_dir("a", "/work/my-repo"):
        pass
    with pytest.raises(RuntimeError):
        with temp_layout.managed_temp_dir("b", "/work/my-repo"):
            raise RuntimeError("boom")
    handed = temp_layout.make_managed_temp_dir("c", "/work/my-repo")
    for fn in registered:
        fn()

    assert not handed.exists()
    assert [p.name for p in run_temp.iterdir()] == [kept.name]


def test_managed_dir_does_not_renest_the_skeleton_inside_a_session_dir(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    session = fake_temp / "coordinator" / "_fleet" / "run-temp" / "1-abcdef-pytest-session"
    session.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(session))
    made = temp_layout.make_managed_temp_dir("x-")
    assert made.parent == session
    assert not (session / "coordinator").exists()


def test_fleet_root_inside_a_session_dir_is_the_enclosing_real_fleet_root(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fleet = fake_temp / "coordinator" / "_fleet"
    session = real_fleet / "run-temp" / "1-abcdef-pytest-session"
    session.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(session))
    assert temp_layout.fleet_temp_root() == real_fleet
    assert temp_layout.managed_tempdir() == real_fleet
    assert os.listdir(session) == []


def test_fleet_root_outside_a_session_dir_is_unchanged(fake_temp) -> None:
    assert temp_layout.fleet_temp_root() == fake_temp / "coordinator" / "_fleet"


def test_managed_dir_outside_a_session_dir_still_uses_the_skeleton(fake_temp) -> None:
    made = temp_layout.make_managed_temp_dir("y-")
    assert made.parent == fake_temp / "coordinator" / "_fleet" / "run-temp"


@pytest.fixture
def redirected_into_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """TEMP redirected to ``<fake repo>/scratch/codex-tmp``; the platform temp is a real sibling dir."""
    repo = tmp_path / "fake-repo"
    (repo / ".git").mkdir(parents=True)
    redirect = repo / "scratch" / "codex-tmp"
    redirect.mkdir(parents=True)
    system = tmp_path / "system-temp"
    system.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(redirect))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    (tmp_path / "lad" / "Temp").mkdir(parents=True)
    return repo, system


def test_pytest_basetemp_root_leaves_a_repo_redirected_temp(redirected_into_repo, monkeypatch) -> None:
    repo, system = redirected_into_repo
    if sys.platform != "win32":
        monkeypatch.setattr(temp_layout, "_inside_worktree", lambda p: Path(p).is_relative_to(repo))
        monkeypatch.setattr(temp_layout.Path, "is_dir", lambda self: True)
    root = temp_layout.pytest_basetemp_root(repo)
    assert not root.is_relative_to(repo)
    assert root.parts[-3:] == ("coordinator", "fake-repo", "pytest")
    fleet = temp_layout.pytest_fleet_basetemp_root()
    assert not fleet.is_relative_to(repo)
    assert fleet.parts[-3:] == ("coordinator", "_fleet", "pytest")


def test_default_basetemp_hook_lands_outside_the_worktree(redirected_into_repo, monkeypatch) -> None:
    import types

    from coordinator_core import conftest as cc

    repo, _system = redirected_into_repo
    if sys.platform != "win32":
        monkeypatch.setattr(temp_layout, "_inside_worktree", lambda p: Path(p).is_relative_to(repo))
        monkeypatch.setattr(temp_layout.Path, "is_dir", lambda self: True)
    config = types.SimpleNamespace(option=types.SimpleNamespace(basetemp=None), rootpath=repo)
    cc.default_basetemp_to_temp_layout(config)
    assert not Path(config.option.basetemp).is_relative_to(repo)


def test_default_basetemp_hook_keeps_a_callers_basetemp(tmp_path) -> None:
    import types

    from coordinator_core import conftest as cc

    given = str(tmp_path / "given")
    config = types.SimpleNamespace(option=types.SimpleNamespace(basetemp=given), rootpath=tmp_path)
    cc.default_basetemp_to_temp_layout(config)
    assert config.option.basetemp == given


def test_pytest_basetemp_root_keeps_a_plain_temp(fake_temp) -> None:
    assert temp_layout.pytest_basetemp_root("/work/r") == fake_temp / "coordinator" / "r" / "pytest"
    assert temp_layout.pytest_fleet_basetemp_root() == fake_temp / "coordinator" / "_fleet" / "pytest"


_MOUNTINFO_TMPFS_TMP = (
    "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    "30 20 0:25 / /tmp rw - tmpfs tmpfs rw\n"
    "31 20 8:1 /v /var/tmp rw - ext4 /dev/sda1 rw\n"
    r"32 20 0:26 / /mnt/with\040space rw - tmpfs tmpfs rw" "\n"
)


@pytest.fixture
def linux_tmpfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fake mountinfo with /tmp tmpfs; the disk fallbacks are real tmp_path dirs outside any worktree."""
    info = tmp_path / "mountinfo"
    info.write_text(_MOUNTINFO_TMPFS_TMP)
    monkeypatch.setattr(temp_layout, "_MOUNTINFO", str(info))
    monkeypatch.setattr(sys, "platform", "linux")
    temp_layout._mount_table.cache_clear()
    disk = tmp_path / "vartmp"
    disk.mkdir()
    xdg = tmp_path / "cache"
    xdg.mkdir()
    monkeypatch.setattr(temp_layout, "_linux_disk_fallbacks", lambda: [disk, xdg])
    monkeypatch.setattr(temp_layout, "_system_temp_dir", lambda: Path("/tmp"))
    monkeypatch.setattr(temp_layout, "_inside_worktree", lambda p: False)
    monkeypatch.setattr(temp_layout, "_real", lambda p: Path(p).as_posix())
    yield disk, xdg
    temp_layout._mount_table.cache_clear()


def test_mount_table_longest_prefix_and_octal_unescape(linux_tmpfs) -> None:
    assert temp_layout._is_tmpfs(Path("/tmp/x/y"))
    assert not temp_layout._is_tmpfs(Path("/var/tmp/x"))
    assert not temp_layout._is_tmpfs(Path("/tmpfoo"))
    assert temp_layout._is_tmpfs(Path("/mnt/with\040space/a"))


def test_pytest_root_prefers_disk_when_the_temp_dir_is_tmpfs(linux_tmpfs) -> None:
    disk, _ = linux_tmpfs
    assert temp_layout.pytest_basetemp_root("/w/r") == disk / "coordinator" / "r" / "pytest"
    assert temp_layout.pytest_fleet_basetemp_root() == disk / "coordinator" / "_fleet" / "pytest"


def test_pytest_root_falls_through_to_the_xdg_cache(linux_tmpfs, monkeypatch) -> None:
    disk, xdg = linux_tmpfs
    monkeypatch.setattr(temp_layout, "_is_tmpfs", lambda p: Path(p).as_posix().startswith("/tmp") or Path(p) == disk)
    assert temp_layout.pytest_basetemp_root("/w/r") == xdg / "coordinator" / "r" / "pytest"


def test_pytest_root_skips_a_fallback_inside_a_worktree(linux_tmpfs, monkeypatch) -> None:
    disk, xdg = linux_tmpfs
    monkeypatch.setattr(temp_layout, "_inside_worktree", lambda p: Path(p) == disk)
    assert temp_layout.pytest_basetemp_root("/w/r") == xdg / "coordinator" / "r" / "pytest"


def test_pytest_root_keeps_tmpfs_when_no_fallback_qualifies(linux_tmpfs, monkeypatch) -> None:
    monkeypatch.setattr(temp_layout, "_linux_disk_fallbacks", lambda: [Path("/nonexistent-a/b")])
    assert temp_layout.pytest_basetemp_root("/w/r") == Path("/tmp/coordinator/r/pytest")


def test_pytest_root_unchanged_when_the_temp_dir_is_not_tmpfs(linux_tmpfs, monkeypatch) -> None:
    monkeypatch.setattr(temp_layout, "_system_temp_dir", lambda: Path("/var/tmp"))
    assert temp_layout.pytest_basetemp_root("/w/r") == Path("/var/tmp/coordinator/r/pytest")


def test_pytest_root_unchanged_off_linux(linux_tmpfs, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert temp_layout.pytest_basetemp_root("/w/r") == Path("/tmp/coordinator/r/pytest")


def test_sweep_roots_cover_the_system_temp_and_the_disk_fallbacks(linux_tmpfs) -> None:
    disk, xdg = linux_tmpfs
    roots = temp_layout.pytest_sweep_roots()
    assert disk / "coordinator" in roots and xdg / "coordinator" in roots
    assert Path("/tmp/coordinator") in roots
    assert len(roots) == len(set(roots))


def test_coordinator_root_lands_on_disk_under_a_tmpfs_temp(linux_tmpfs, monkeypatch) -> None:
    disk, _ = linux_tmpfs
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    assert temp_layout.coordinator_temp_root("/w/r") == disk / "coordinator" / "r"
    assert temp_layout.fleet_temp_root() == disk / "coordinator" / "_fleet"
    assert temp_layout.managed_tempdir() == disk / "coordinator" / "_fleet"


def test_coordinator_root_unchanged_off_tmpfs_and_off_linux(linux_tmpfs, monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/var/tmp")
    assert temp_layout.coordinator_temp_root("/w/r") == Path("/var/tmp/coordinator/r")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(sys, "platform", "win32")
    assert temp_layout.coordinator_temp_root("/w/r") == Path("/tmp/coordinator/r")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert temp_layout.fleet_temp_root() == Path("/tmp/coordinator/_fleet")


def test_coordinator_root_keeps_tmpfs_when_no_fallback_qualifies(linux_tmpfs, monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(temp_layout, "_linux_disk_fallbacks", lambda: [Path("/nonexistent-a/b")])
    assert temp_layout.coordinator_temp_root("/w/r") == Path("/tmp/coordinator/r")


def test_registry_pytest_run_root_still_wins_over_the_disk_root(linux_tmpfs, monkeypatch, tmp_path) -> None:
    from coordinator_core.testing import scoped_pytest

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    override = tmp_path / "override"
    monkeypatch.setattr(scoped_pytest.machine_resolver, "registry_get", lambda key: str(override))
    assert scoped_pytest.per_run_root("/w/r") == override


def test_managed_dir_is_created_on_the_disk_root(linux_tmpfs, monkeypatch) -> None:
    disk, _ = linux_tmpfs
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(temp_layout, "_legacy_root_reaped", True)
    with temp_layout.managed_temp_dir("x", "/w/r") as path:
        assert path.parent == disk / "coordinator" / "r" / "run-temp"


def _legacy_tree(tmp_path: Path, monkeypatch):
    """Old tmpfs root with a dead-owner, a live-owner run-temp dir and a stale/fresh clean mirror."""
    old_base = tmp_path / "oldtmp"
    disk = tmp_path / "disk"
    run_temp = old_base / "coordinator" / "r" / "run-temp"
    dead, live = run_temp / "999991-aaaaaa-x", run_temp / "999992-bbbbbb-x"
    mirrors = old_base / "coordinator" / "r" / "clean-mirror"
    stale, fresh = mirrors / "stale", mirrors / "fresh"
    for d in (dead, live, stale, fresh, disk):
        d.mkdir(parents=True)
    os.utime(stale, (1, 1))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(old_base))
    monkeypatch.setattr(temp_layout, "_is_tmpfs", lambda p: Path(p) == old_base)
    monkeypatch.setattr(temp_layout, "_linux_disk_fallbacks", lambda: [disk])
    monkeypatch.setattr(temp_layout, "_inside_worktree", lambda p: False)
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == 999992)
    monkeypatch.setattr(temp_layout, "_legacy_root_reaped", False)
    return dead, live, stale, fresh, disk


def test_first_use_reaps_dead_owner_and_stale_cache_in_the_old_root(tmp_path, monkeypatch) -> None:
    dead, live, stale, fresh, disk = _legacy_tree(tmp_path, monkeypatch)
    with temp_layout.managed_temp_dir("y", "/w/r") as path:
        assert path.is_relative_to(disk)
    assert not dead.exists() and not stale.exists()
    assert live.exists() and fresh.exists()


def test_old_root_reap_runs_once_per_process(tmp_path, monkeypatch) -> None:
    dead, *_ = _legacy_tree(tmp_path, monkeypatch)
    with temp_layout.managed_temp_dir("y", "/w/r"):
        pass
    dead.mkdir()
    with temp_layout.managed_temp_dir("y", "/w/r"):
        pass
    assert dead.exists()


def test_old_root_reap_is_a_noop_when_the_root_did_not_move(tmp_path, monkeypatch) -> None:
    _, _, stale, _, _ = _legacy_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(temp_layout, "_is_tmpfs", lambda p: False)
    with temp_layout.managed_temp_dir("y", "/w/r"):
        pass
    assert stale.exists()


def test_old_root_bug_blitz_cache_ages_out_at_seven_days(tmp_path, monkeypatch) -> None:
    *_, disk = _legacy_tree(tmp_path, monkeypatch)
    blitz = tmp_path / "oldtmp" / "coordinator" / "_fleet" / "coordinator-bug-blitz"
    old, mid, new = blitz / "old", blitz / "mid", blitz / "new"
    for d in (old, mid, new):
        d.mkdir(parents=True)
    now = __import__("time").time()
    os.utime(old, (now - 8 * 86400,) * 2)
    os.utime(mid, (now - 3 * 86400,) * 2)
    with temp_layout.managed_temp_dir("y", "/w/r"):
        pass
    assert not old.exists() and mid.exists() and new.exists()


def test_old_root_reap_skips_a_leased_commit_clone_and_prunes_the_rest(tmp_path, monkeypatch) -> None:
    *_, disk = _legacy_tree(tmp_path, monkeypatch)
    clones = tmp_path / "oldtmp" / "coordinator" / "_fleet" / "claude-klabauter-commit-clone"
    leased, dead_lease, bare = clones / "leased", clones / "deadlease", clones / "bare"
    (leased / ".git" / "in-use").mkdir(parents=True)
    (leased / ".git" / "in-use" / "999992").write_text("")
    (dead_lease / ".git" / "in-use").mkdir(parents=True)
    (dead_lease / ".git" / "in-use" / "999991").write_text("")
    bare.mkdir(parents=True)
    for d in (leased, dead_lease, bare):
        os.utime(d, (1, 1))
    with temp_layout.managed_temp_dir("y", "/w/r"):
        pass
    assert leased.exists()
    assert not dead_lease.exists() and not bare.exists()


def test_reap_dead_owner_dirs_cap_bounds_removals_per_call(tmp_path: Path) -> None:
    run_temp = tmp_path / "run-temp"
    dead = [run_temp / f"2000000000-{i:06x}-x" for i in range(5)]
    for d in dead:
        d.mkdir(parents=True)
    temp_layout._reap_dead_owner_dirs(run_temp, cap=2)
    assert sum(d.exists() for d in dead) == 3
    temp_layout._reap_dead_owner_dirs(run_temp, cap=64)
    assert not any(d.exists() for d in dead)


def test_scratch_root_is_the_machine_local_key_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(tmp_path) if key == "paths.scratch_root" else None)
    assert temp_layout.scratch_root() == tmp_path
    assert temp_layout.scratch_root("/work/r") == tmp_path


def test_scratch_root_falls_back_to_the_managed_temp_root_when_unset(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert temp_layout.scratch_root() == temp_layout.fleet_temp_root()
    assert temp_layout.scratch_root("/work/r") == temp_layout.coordinator_temp_root("/work/r")
    assert not temp_layout.scratch_root("/work/r").exists()


def _registry(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: values.get(key))


def test_build_root_is_the_machine_local_key_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recwarn) -> None:
    _registry(monkeypatch, {"paths.build_root": str(tmp_path / "b"), "paths.scratch_root": str(tmp_path / "s")})
    assert temp_layout.build_root() == tmp_path / "b"
    assert not [w for w in recwarn.list if "paths.build_root" in str(w.message)]


def test_build_root_falls_back_to_scratch_root_with_a_warning_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, {"paths.scratch_root": str(tmp_path / "s")})
    with pytest.warns(UserWarning, match=r"paths\.build_root"):
        assert temp_layout.build_root() == tmp_path / "s"


def test_scratch_and_build_root_delegate_to_run_scratch_with_the_managed_fallback(fake_temp, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import run_scratch

    _registry(monkeypatch, {})
    seen: list = []
    monkeypatch.setattr(run_scratch, "scratch_root", lambda fallback=None: seen.append(("scratch", fallback)) or Path("S"))
    monkeypatch.setattr(run_scratch, "build_root", lambda fallback=None: seen.append(("build", fallback)) or Path("B"))
    assert temp_layout.scratch_root("/work/r") == Path("S")
    with pytest.warns(UserWarning):
        assert temp_layout.build_root() == Path("B")
    assert seen[0] == ("scratch", temp_layout.coordinator_temp_root("/work/r"))
    assert seen[-1] == ("build", Path("S"))


def test_scratch_root_resolves_through_the_registry_dir_when_machine_resolver_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core import run_scratch

    registry = tmp_path / "machine-local"
    registry.mkdir()
    (registry / "registry.local.toml").write_text('[paths]\nscratch_root = "/vendored/s"\n', encoding="utf-8")
    monkeypatch.setattr(run_scratch, "_machine_resolver", lambda: None)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.delenv("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", raising=False)
    assert temp_layout.scratch_root("/work/r") == Path("/vendored/s")


def test_build_output_dir_is_build_root_repo_output_and_leaves_a_prior_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, {"paths.build_root": str(tmp_path)})
    out = temp_layout.build_output_dir("/work/game", "win64")
    assert out == tmp_path / "game" / "win64"
    assert not out.exists()
    out.mkdir(parents=True)
    (out / "prior.bin").write_text("x")
    assert temp_layout.build_output_dir("/work/game", "win64") == out
    assert (out / "prior.bin").read_text() == "x"


@pytest.mark.parametrize("output", ["", ".", "..", "a/b", "a\\b"])
def test_build_output_dir_refuses_a_non_segment_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    _registry(monkeypatch, {"paths.build_root": str(tmp_path)})
    with pytest.raises(ValueError):
        temp_layout.build_output_dir("/work/game", output)


def test_root_writers_name_every_child_shape_with_owner_and_bound() -> None:
    writers = {w.name: w for w in temp_layout.COORDINATOR_ROOT_WRITERS}
    assert len(writers) == len(temp_layout.COORDINATOR_ROOT_WRITERS)
    assert {"pytest", "run-temp", "clean-mirror"} <= {w.name for w in writers.values() if w.scope == "repo"}
    assert {"fleet-session-files", "fleet-caches", "claude-klabauter-commit-clone", "claude-klabauter-publish-engine"} <= {
        w.name for w in writers.values() if w.scope == "fleet"
    }
    for w in writers.values():
        assert w.owner in {"pid", "lease", "none"}
        assert w.max_age_s >= 0
    assert writers["claude-klabauter-commit-clone"].owner == "lease"
    assert writers["pytest"].owner == "pid"


def test_unowned_outputs_keep_latest_two() -> None:
    for w in temp_layout.COORDINATOR_ROOT_WRITERS:
        if w.name in {"clean-mirror", "fleet-caches", "claude-klabauter-commit-clone", "claude-klabauter-publish-engine"}:
            assert w.keep_latest == 2


def test_root_writer_matching_is_pinned_to_the_producer_shape() -> None:
    writers = {w.name: w for w in temp_layout.COORDINATOR_ROOT_WRITERS}
    assert writers["fleet-session-files"].matches("workflow-stopped-abc")
    assert not writers["fleet-session-files"].matches("someone-elses-file")
    assert writers["fleet-caches"].matches("tmpab12_cd9")
    assert writers["fleet-caches"].matches("mk-git-seed-1")
    assert not writers["fleet-caches"].matches("tmpshort")
    assert writers["pytest"].matches("123-abcdef")
