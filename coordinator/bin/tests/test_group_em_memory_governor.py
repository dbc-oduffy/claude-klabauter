"""group-em-memory-governor: victim choice, protection, and the pause/resume loop."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "group-em-memory-governor.py"
_spec = importlib.util.spec_from_file_location("group_em_memory_governor", _SRC)
assert _spec is not None and _spec.loader is not None
gov = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gov  # dataclass resolution needs the module registered
_spec.loader.exec_module(gov)

P = gov.Proc


@pytest.fixture(autouse=True)
def _fake_start_times(monkeypatch):
    # Fake pids have no real start time; without this every freeze is skipped and `ps` runs per pid.
    monkeypatch.setattr(gov, "process_start_time", lambda pid: 1.0)
    monkeypatch.setattr(gov, "process_start_times", lambda pids: {p: 1.0 for p in pids})


@pytest.fixture(autouse=True)
def _non_windows_host(monkeypatch):
    # The win32-only cache-trim hint must not leak into the pause/resume line assertions;
    # the hint tests set their own platform.
    monkeypatch.setattr(gov.sys, "platform", "linux")


@pytest.fixture(autouse=True)
def _inline_marker(monkeypatch):
    # pytest runs under a Claude Code session; the marker keeps in-process main() calls inline.
    monkeypatch.setenv("COORDINATOR_GUARD_DETACHED", "1")


def _args(**kw):
    base = dict(interval=0, max_pauses=3, resume_after=2, psi_full=10.0, avail_pct=5.0,
                runaway_mb=0, runaway_every=1)
    base.update(kw)
    return argparse.Namespace(**base)


PROCS = [
    P(10, 1, 900, 3500, "node /x/typescript/lib/tsserver.js --useInferredProjectPerProjectRoot"),
    P(11, 1, 5000, 400, "/x/.local/bin/claude --plugin-dir /x"),
    P(20, 1, 300, 2000, "node /x/next/dist/bin/next-server"),
    P(30, 1, 60, 900, "node /x/vitest/vitest.mjs run src --maxWorkers=4"),
    P(31, 30, 50, 500, "node /x/vitest/dist/workers/forks.js"),
    P(40, 1, 10, 1200, "node /x/firebase-tools/lib/bin/firebase.js deploy --only hosting"),
    P(41, 1, 5, 800, "node /x/scratch/deploy-905588/node_modules/.bin/next build"),
    P(50, 1, 1, 100, "node /x/vitest/vitest.mjs run tiny"),
    P(60, 1, 20, 600, "node /x/X/coordinator-content-repo/node_modules/.bin/vitest run"),
]


def test_youngest_heavy_unprotected_is_chosen():
    # 41 and 40 are younger but sit inside a deploy; 50 is under the RSS floor.
    assert gov.choose_victim(PROCS, set(), self_pid=999).pid == 60


def test_repo_name_containing_claude_is_not_protected():
    assert gov.is_candidate(PROCS[-1], self_pid=999)


def test_harness_language_server_and_deploy_are_protected():
    for pid in (10, 11, 40, 41):
        assert not gov.is_candidate(next(p for p in PROCS if p.pid == pid), self_pid=999)


def test_already_paused_is_skipped():
    assert gov.choose_victim(PROCS, {60}, self_pid=999).pid == 30


def test_descendants_include_workers():
    assert sorted(gov.descendants(30, PROCS)) == [30, 31]


def test_pause_on_critical_then_resume_after_calm():
    verdicts = iter([True, False, False, False])
    lines, calls = [], []
    gov.run(_args(), emit=lines.append, sample=lambda: next(verdicts), procs_fn=lambda: PROCS,
            signal_fn=lambda pids, stop: calls.append((tuple(pids), stop)),
            sleep=lambda s: None, max_ticks=4)
    assert lines[0].startswith("GOVERNOR PAUSED pid=60")
    assert lines[1] == "GOVERNOR RESUMED pid=60"
    assert calls == [((60,), True), ((60,), False)]
    assert len(lines) == 2


def test_quiet_when_never_critical():
    lines = []
    gov.run(_args(), emit=lines.append, sample=lambda: False, procs_fn=lambda: PROCS,
            signal_fn=lambda *a: None, sleep=lambda s: None, max_ticks=10)
    assert lines == []


def test_escalates_then_gives_up_once():
    lines = []
    gov.run(_args(max_pauses=2), emit=lines.append, sample=lambda: True, procs_fn=lambda: PROCS,
            signal_fn=lambda *a: None, sleep=lambda s: None, max_ticks=12)
    assert [line.split()[1] for line in lines] == [
        "PAUSED", "PAUSED", "STILL-CRITICAL", "RESUMED-ON-EXIT"]
    assert "pid=60" in lines[0] and "pid=30" in lines[1]


def test_exit_resumes_anything_still_paused():
    verdicts = iter([True])
    calls = []

    def sample():
        try:
            return next(verdicts)
        except StopIteration:
            raise SystemExit(0)  # stands in for SIGTERM at Monitor expiry

    try:
        gov.run(_args(), emit=lambda line: None, sample=sample, procs_fn=lambda: PROCS,
                signal_fn=lambda pids, stop: calls.append((tuple(pids), stop)),
                sleep=lambda s: None)
    except SystemExit:
        pass
    assert calls == [((60,), True), ((60,), False)]


def test_runaway_flagged_once_per_pid_without_signalling():
    lines, calls = [], []
    gov.run(_args(runaway_mb=12288), emit=lines.append, sample=lambda: False,
            procs_fn=lambda: PROCS, signal_fn=lambda *a: calls.append(a), sleep=lambda s: None,
            max_ticks=3, footprint_fn=lambda: {20: 66560.0, 30: 900.0})
    assert lines == ["GOVERNOR RUNAWAY pid=20 footprint=66560MB cmd=node /x/next/dist/bin/next-server"]
    assert calls == []


RAG_TREE = [
    P(70, 1, 30, 200, "python /x/project_rag_scripts/structural_index_refresh.py --run-and-record"),
    P(71, 70, 29, 150, "python /x/project_rag_scripts/structural_index_refresh.py ensure"),
    P(72, 1, 5, 2000, "python /x/project_rag_scripts/server.py"),
]


def _channel(tmp_path, *lines):
    path = tmp_path / "box" / "admission.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _crit(procs, path, ticks=1, notice_fn=None):
    lines, calls = [], []
    kw = {"notice_fn": notice_fn} if notice_fn else {}
    gov.run(_args(), emit=lines.append, sample=lambda: True, procs_fn=lambda: procs,
            signal_fn=lambda pids, stop: calls.append((tuple(pids), stop)),
            sleep=lambda s: None, max_ticks=ticks, notice_path=path, **kw)
    return lines, calls


def test_noticed_example_retrieval_repo_ensure_tree_is_paused_at_critical(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[70],"note":"declared_peak_mb=6144"}')
    lines, calls = _crit(RAG_TREE, path)
    assert lines[0].startswith("GOVERNOR PAUSED pid=70")
    assert calls[0] == ((70, 71), True)


def test_unnoticed_example_retrieval_repo_server_is_never_paused(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[70]}')
    lines, calls = _crit([RAG_TREE[2]], path)
    assert lines == [] and calls == []


def test_absent_or_malformed_channel_changes_nothing(tmp_path):
    for path in (tmp_path / "missing.jsonl", None):
        lines, _ = _crit(RAG_TREE, path)
        assert lines == []
    path = _channel(tmp_path, "not json", '{"phase":"START","pids":"x"}', "[1]")
    assert _crit(RAG_TREE, path)[0] == []


def test_calm_sample_never_reads_the_channel(tmp_path):
    reads = []
    gov.run(_args(), emit=lambda line: None, sample=lambda: False, procs_fn=lambda: RAG_TREE,
            signal_fn=lambda *a: None, sleep=lambda s: None, max_ticks=5,
            notice_path=tmp_path / "x", notice_fn=lambda p, procs: reads.append(1) or frozenset())
    assert reads == []


def test_dead_noticed_pid_is_ignored(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[999]}', '{"phase":"END","pids":[70]}')
    assert gov.read_noticed_pids(path, RAG_TREE) == frozenset()


CONHOSTS = [
    P(80, 1, 5, 8, r"C:\Windows\System32\conhost.exe 0x4"),
    P(81, 1, 4, 7, "conhost.exe"),
]


def test_noticed_conhost_is_never_paused_and_only_hint_is_emitted(tmp_path, monkeypatch):
    monkeypatch.setattr(gov.sys, "platform", "win32")
    path = _channel(tmp_path, '{"phase":"START","pids":[80,81]}')
    lines, calls = _crit(CONHOSTS, path, ticks=3)
    assert calls == []
    assert len(lines) == 1 and lines[0].startswith("GOVERNOR CACHE-TRIM-HINT")


def test_noticed_process_under_rss_floor_is_not_paused(tmp_path):
    small = [P(90, 1, 5, 50, "python /x/job.py"), P(91, 90, 4, 40, "python /x/worker.py")]
    path = _channel(tmp_path, '{"phase":"START","pids":[90]}')
    lines, calls = _crit(small, path)
    assert calls == [] and all("PAUSED" not in line for line in lines)


def test_noticed_tree_over_floor_is_still_paused(tmp_path):
    big = [P(90, 1, 5, 50, "python /x/job.py"), P(91, 90, 4, 400, "python /x/worker.py")]
    path = _channel(tmp_path, '{"phase":"START","pids":[90]}')
    lines, calls = _crit(big, path)
    assert calls[0] == ((90, 91), True)


def test_noticed_harness_process_stays_protected(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[11]}')
    assert _crit(PROCS, path)[1][0][0] == (60,)


# Freezing a store owner mid-write stalls or half-applies every client's write (fifa-stats pid 70608).
STORE_OWNERS = [
    r"C:\Program Files\nodejs\node.exe --import tsx E:\fifa-stats\packages\ddct\adapters\master\owner.ts E:\fifa-stats\.ddct\pglite",  # abs-path-ok: the exact command line the governor froze
    "postgres -D /var/lib/postgresql/data",
    "/usr/sbin/mysqld --basedir=/usr",
    "node --import tsx ./pglite-server.ts",
    "sqlite3 ./app.db",
]


@pytest.mark.parametrize("cmd", STORE_OWNERS)
def test_store_and_database_owners_are_never_candidates(cmd):
    assert not gov.is_candidate(P(70608, 1, 5, 302, cmd), self_pid=999)


def test_tail_read_is_bounded_and_drops_the_cut_line(tmp_path):
    old = '{"phase":"START","pids":[70]}'
    pad = '{"phase":"END","pids":[],"note":"' + "x" * 200 + '"}'
    path = _channel(tmp_path, old, *([pad] * 400))
    assert gov.read_noticed_pids(path, RAG_TREE) == frozenset()


def test_ended_launch_pid_is_not_noticed(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[70]}', '{"phase":"END","pids":[70]}')
    assert gov.read_noticed_pids(path, RAG_TREE) == frozenset()


def test_runaway_reads_footprint_only_every_n_ticks():
    reads = []
    gov.run(_args(runaway_mb=12288, runaway_every=6), emit=lambda line: None, sample=lambda: False,
            procs_fn=lambda: PROCS, signal_fn=lambda *a: None, sleep=lambda s: None,
            max_ticks=12, footprint_fn=lambda: reads.append(1) or {})
    assert len(reads) == 2


def test_help_exits_zero():
    import pytest

    with pytest.raises(SystemExit) as exc:
        gov.main(["--help"])
    assert exc.value.code == 0


def _stream(verdicts, tmp_path, plat="win32", sibling=True, monkeypatch=None):
    if sibling:
        (tmp_path / "trim-file-cache.py").write_text("")
    monkeypatch.setattr(gov.sys, "platform", plat)
    it = iter(verdicts)
    lines = []
    gov.run(_args(resume_after=1), emit=lines.append, sample=lambda: next(it), procs_fn=lambda: PROCS,
            signal_fn=lambda *a: None, sleep=lambda s: None, max_ticks=len(verdicts),
            bin_dir=tmp_path)
    return lines


def _hints(lines):
    return [x for x in lines if "CACHE-TRIM-HINT" in x]


def test_hint_prints_once_across_critical_streak(tmp_path, monkeypatch):
    lines = _stream([True, True, True], tmp_path, monkeypatch=monkeypatch)
    assert _hints(lines) == [
        f'GOVERNOR CACHE-TRIM-HINT run elevated: python "{tmp_path.as_posix()}/trim-file-cache.py" --apply']


def test_hint_rearms_after_calm(tmp_path, monkeypatch):
    lines = _stream([True, False, True], tmp_path, monkeypatch=monkeypatch)
    assert len(_hints(lines)) == 2


def test_hint_silent_on_linux(tmp_path, monkeypatch):
    assert _hints(_stream([True, True], tmp_path, plat="linux", monkeypatch=monkeypatch)) == []


def test_hint_unavailable_when_sibling_absent(tmp_path, monkeypatch):
    lines = _stream([True], tmp_path, sibling=False, monkeypatch=monkeypatch)
    assert _hints(lines) == [
        "GOVERNOR CACHE-TRIM-HINT unavailable: trim-file-cache.py not beside the governor"]


def test_hint_spawns_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(gov.subprocess, "run", boom)
    monkeypatch.setattr(gov.subprocess, "Popen", boom)
    assert gov.cache_trim_hint(tmp_path).startswith("GOVERNOR CACHE-TRIM-HINT")
    assert _hints(_stream([True, True], tmp_path, monkeypatch=monkeypatch))


def test_verdicts_identical_with_and_without_sibling(tmp_path, monkeypatch):
    seq = [True, True, False, False]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _stream(seq, tmp_path / "a", sibling=True, monkeypatch=monkeypatch)
    b = _stream(seq, tmp_path / "b", sibling=False, monkeypatch=monkeypatch)
    strip = lambda ls: [x for x in ls if "CACHE-TRIM-HINT" not in x]
    assert strip(a) == strip(b) and any("PAUSED" in x for x in a) and any("RESUMED" in x for x in a)


def test_help_exits_zero_on_linux():
    with pytest.raises(SystemExit) as exc:
        gov.main(["--help"])
    assert exc.value.code == 0


def test_pids_are_in_pidfile_before_freeze_and_cleared_on_resume(tmp_path):
    pidfile = tmp_path / "frozen.json"
    seen = []
    verdicts = iter([True, False, False])

    def signal_fn(pids, stop):
        seen.append((stop, pidfile.exists() and sorted(
            e["pid"] for e in json.loads(pidfile.read_text())["frozen"])))

    gov.run(_args(), emit=lambda line: None, sample=lambda: next(verdicts), procs_fn=lambda: PROCS,
            signal_fn=signal_fn, sleep=lambda s: None, max_ticks=3, pidfile=pidfile)
    assert seen[0] == (True, [60])  # recorded before the freeze signal
    assert seen[1][0] is False
    assert not pidfile.exists()


def test_finally_thaw_clears_pidfile(tmp_path):
    pidfile = tmp_path / "frozen.json"

    def sample():
        if pidfile.exists():
            raise SystemExit(0)
        return True

    try:
        gov.run(_args(), emit=lambda line: None, sample=sample, procs_fn=lambda: PROCS,
                signal_fn=lambda pids, stop: None, sleep=lambda s: None, pidfile=pidfile)
    except SystemExit:
        pass
    assert not pidfile.exists()


def test_start_thaws_leftover_pids_before_first_sample(tmp_path):
    pidfile = tmp_path / "frozen.json"
    _record(pidfile, 999999, 5.0, [(70, 7.0), (71, 8.0)])  # a governor hard-killed mid-freeze
    calls = []
    _run_start(pidfile, calls, {70: 7.0, 71: 8.0}.get)
    assert calls == [((70, 71), False), "sample"]
    assert not pidfile.exists()


def _record(pidfile, owner, owner_start, frozen):
    pidfile.write_text(json.dumps({
        "governor_pid": owner, "governor_start_time": owner_start,
        "frozen": [{"pid": p, "start_time": s} for p, s in frozen]}))


def _run_start(pidfile, calls, start_time_fn):

    def sample():
        calls.append("sample")
        return False

    gov.run(_args(), emit=lambda line: None, sample=sample, procs_fn=lambda: PROCS,
            signal_fn=lambda pids, stop: calls.append((tuple(pids), stop)),
            sleep=lambda s: None, max_ticks=1, pidfile=pidfile, start_time_fn=start_time_fn)


def test_alive_owner_thaws_nothing_and_keeps_file(tmp_path):
    pidfile = tmp_path / "frozen.json"
    _record(pidfile, 10, 100.0, [(70, 7.0)])
    times = {10: 100.5, 70: 7.0}
    calls = []
    assert gov.thaw_leftovers(pidfile, lambda p, s: calls.append(p), times.get) == []
    assert calls == [] and pidfile.exists()


def test_reused_pid_is_skipped(tmp_path):
    pidfile = tmp_path / "frozen.json"
    _record(pidfile, 10, 100.0, [(70, 7.0), (71, 8.0)])
    times = {70: 999.0, 71: 8.0}  # owner dead; pid 70 now a different process
    calls = []
    assert gov.thaw_leftovers(pidfile, lambda p, s: calls.append((p, s)), times.get) == [71]
    assert calls == [([71], False)]


def test_dead_owner_matching_pids_thawed_and_file_cleared(tmp_path):
    pidfile = tmp_path / "frozen.json"
    _record(pidfile, 10, 100.0, [(70, 7.0)])
    times = {70: 7.0, 10: 555.0}  # owner pid reused by another process
    calls = []
    assert gov.thaw_leftovers(pidfile, lambda p, s: calls.append(p), times.get) == [70]
    assert calls == [[70]] and not pidfile.exists()


def test_thaw_leftovers_ignores_missing_and_malformed(tmp_path):
    calls = []
    assert gov.thaw_leftovers(tmp_path / "none.json", lambda p, s: calls.append(p)) == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert gov.thaw_leftovers(bad, lambda p, s: calls.append(p)) == []
    assert calls == [] and not bad.exists()


def test_footprint_mb_from_rusage_reads_phys_footprint_field():
    import struct
    buf = bytearray(256)
    struct.pack_into("<Q", buf, 72, 3 * 1048576)
    struct.pack_into("<Q", buf, 64, 999 * 1048576)  # ri_resident_size is a different field
    assert gov.footprint_mb_from_rusage(bytes(buf)) == 3.0


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is darwin-only")
def test_darwin_footprints_spawn_nothing_and_include_self(monkeypatch):
    import os

    def boom(*a, **k):
        raise AssertionError("footprints spawned a process")

    monkeypatch.setattr(gov.subprocess, "run", boom)
    monkeypatch.setattr(gov.subprocess, "Popen", boom)
    out = gov._darwin_footprints(top_n=100000)
    assert os.getpid() in out and out[os.getpid()] > 0
    assert len(gov._darwin_footprints(top_n=3)) == 3


def test_default_pidfile_thaw_also_resumes_the_legacy_pidfile(tmp_path, monkeypatch):
    managed, legacy = tmp_path / "managed.json", tmp_path / "legacy.json"
    monkeypatch.setattr(gov, "default_pidfile", lambda: managed)
    monkeypatch.setattr(gov, "legacy_pidfile", lambda: legacy)
    _record(legacy, 999999, 5.0, [(70, 7.0)])  # frozen by a pre-upgrade governor
    calls = []
    assert gov.thaw_leftovers(managed, lambda p, s: calls.append(p), {70: 7.0}.get) == [70]
    assert calls == [[70]] and not legacy.exists()


def test_injected_pidfile_does_not_read_the_legacy_pidfile(tmp_path, monkeypatch):
    injected, legacy = tmp_path / "injected.json", tmp_path / "legacy.json"
    monkeypatch.setattr(gov, "default_pidfile", lambda: tmp_path / "managed.json")
    monkeypatch.setattr(gov, "legacy_pidfile", lambda: legacy)
    _record(legacy, 999999, 5.0, [(70, 7.0)])
    calls = []
    assert gov.thaw_leftovers(injected, lambda p, s: calls.append(p), {70: 7.0}.get) == []
    assert calls == [] and legacy.exists()


def test_pidfile_resolution_survives_an_unresolvable_engine(monkeypatch):
    monkeypatch.setattr(gov, "_temp_layout", lambda: None)
    assert gov.default_pidfile().name == "group-em-memory-governor.frozen.json"
    assert gov.legacy_pidfile().name == gov.default_pidfile().name
    assert gov.legacy_pidfile().parent != gov.default_pidfile().parent


def _klabauter_mirror(tmp_path: Path) -> Path:
    """A published-mirror shaped tree: engine marker, bin + lib, and no hooks/scripts/_engine_root.py."""
    import shutil

    bin_src = _SRC.parent
    root = tmp_path / "mirror"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    dest = root / "coordinator" / "bin"
    dest.mkdir(parents=True)
    shutil.copy2(_SRC, dest / _SRC.name)
    shutil.copytree(bin_src / "lib", dest / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    assert not (root / "coordinator" / "hooks" / "scripts" / "_engine_root.py").exists()
    return root


def test_place_engine_on_path_resolves_from_a_klabauter_shaped_tree(tmp_path, monkeypatch):
    root = _klabauter_mirror(tmp_path)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("cc_invoke", "engine_bootstrap", "lib"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "group_em_memory_governor_mirror", root / "coordinator" / "bin" / _SRC.name)
    mirror = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mirror
    try:
        spec.loader.exec_module(mirror)
        assert mirror._place_engine_on_path() is True
        assert sys.path[0] == str(root.resolve())
    finally:
        sys.modules.pop(spec.name, None)
        for name in ("cc_invoke", "engine_bootstrap", "lib"):
            sys.modules.pop(name, None)


class _FakePopen:
    calls: list = []
    fail = False

    def __init__(self, cmd, **kw):
        if _FakePopen.fail:
            raise OSError("breakaway refused")
        _FakePopen.calls.append((cmd, kw))
        self.pid = 4242


@pytest.fixture
def relaunch(monkeypatch, tmp_path):
    """A session-parented start with the spawn faked: no marker, injected ancestry, fake popen."""
    import subprocess

    monkeypatch.delenv("COORDINATOR_GUARD_DETACHED", raising=False)
    monkeypatch.setenv("COORDINATOR_GUARD_FAKE_SESSION", "1")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    _FakePopen.calls, _FakePopen.fail = [], False
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(gov, "run", lambda *a, **k: pytest.fail("run reached on a relaunch"))
    return tmp_path


def test_session_start_relaunches_detached_and_exits_zero(relaunch, capsys):
    log = relaunch / "g.log"
    with pytest.raises(SystemExit) as exc:
        gov.main(["--interval", "3", "--log", str(log)])
    assert exc.value.code == 0
    assert f"DETACHED group-em-memory-governor pid=4242 log={log}" in capsys.readouterr().out
    (cmd, kw), = _FakePopen.calls
    assert kw["env"]["COORDINATOR_GUARD_DETACHED"] == "1"
    assert cmd[-2:] == ["--log", str(log)] and "--interval" in cmd


def test_default_log_is_settings_home_scratch_governor_log(relaunch):
    with pytest.raises(SystemExit):
        gov.main([])
    (cmd, _), = _FakePopen.calls
    assert cmd[-2:] == ["--log", str(relaunch / "home" / "scratch" / "governor.log")]


def test_strict_spawn_failure_exits_five_with_one_line_remedy(relaunch, capsys):
    _FakePopen.fail = True
    with pytest.raises(SystemExit) as exc:
        gov.main(["--log", str(relaunch / "g.log")])
    assert exc.value.code == 5
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("group-em-memory-governor: cannot leave")


def test_marker_set_runs_inline(monkeypatch):
    reached = []
    monkeypatch.setattr(gov, "run", lambda *a, **k: reached.append(1))
    monkeypatch.setattr(gov, "resolve_notice_path", lambda: None)
    monkeypatch.setattr(gov, "default_pidfile", lambda: None)
    gov.main([])
    assert reached == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-time gate")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_relaunching_parent_costs_under_budget(tmp_path, monkeypatch):
    import os
    import re
    import subprocess

    monkeypatch.undo()  # drop the module fixtures: this test spawns for real
    tests_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "box_memory_cli_process_time", tests_dir / "test_box_memory_cli_process_time.py")
    pt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pt)
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_GUARD_DETACHED"}
    env.update(COORDINATOR_GUARD_FAKE_SESSION="1", COORDINATOR_SETTINGS_HOME=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, str(_SRC), "--log", str(tmp_path / "g.log")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out, err = proc.communicate(timeout=30)
    m = re.search(rb"pid=(\d+)", out)
    try:
        assert proc.returncode in (0, 5), err
        assert pt._cpu_seconds(proc) < 0.5
    finally:
        if m:
            subprocess.run(["taskkill", "/F", "/T", "/PID", m.group(1).decode()],
                           capture_output=True, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _headroom(avails, verdicts=None):
    a, v = iter(avails), iter(verdicts or [False] * len(avails))
    lines = []
    gov.run(_args(), emit=lines.append, sample=lambda: next(v), procs_fn=lambda: PROCS,
            signal_fn=lambda *x: None, sleep=lambda s: None, max_ticks=len(avails),
            avail_fn=lambda: next(a))
    return [x for x in lines if "HEADROOM" in x]


def test_headroom_prints_exactly_one_line_low_to_high():
    assert _headroom([5.0, 5.5, 6.0, 58.0, 58.5, 59.0]) == ["GOVERNOR HEADROOM available=58.0"]


def test_headroom_silent_under_threshold_and_without_avail_fn():
    assert _headroom([5.0, 8.0, 12.9]) == []
    lines = []
    gov.run(_args(), emit=lines.append, sample=lambda: False, procs_fn=lambda: PROCS,
            signal_fn=lambda *x: None, sleep=lambda s: None, max_ticks=3)
    assert lines == []


def test_headroom_fires_when_calm_returns_after_paused():
    lines = _headroom([2.0, 2.0, 3.0, 3.1], [True, True, False, False])
    assert lines == ["GOVERNOR HEADROOM available=3.1"]


def test_headroom_rearms_from_the_lower_mark_after_a_fall():
    assert len(_headroom([30.0, 10.0, 20.0, 21.0])) == 1


def test_place_engine_on_path_is_false_when_the_engine_is_unresolvable(monkeypatch):
    import types

    fake = types.ModuleType("cc_invoke")
    fake.ensure_engine_on_path = lambda _f: None
    monkeypatch.setitem(sys.modules, "cc_invoke", fake)
    assert gov._place_engine_on_path() is False
