"""group-em-memory-governor: victim choice, protection, and the pause/resume loop."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "group-em-memory-governor.py"
_spec = importlib.util.spec_from_file_location("group_em_memory_governor", _SRC)
assert _spec is not None and _spec.loader is not None
gov = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gov  # dataclass resolution needs the module registered
_spec.loader.exec_module(gov)

P = gov.Proc


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


def test_noticed_harness_process_stays_protected(tmp_path):
    path = _channel(tmp_path, '{"phase":"START","pids":[11]}')
    assert _crit(PROCS, path)[1][0][0] == (60,)


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
    a = _stream(seq, tmp_path / "a", sibling=True, monkeypatch=monkeypatch) if (tmp_path / "a").mkdir() is None else None
    b = _stream(seq, tmp_path / "b", sibling=False, monkeypatch=monkeypatch) if (tmp_path / "b").mkdir() is None else None
    strip = lambda ls: [x for x in ls if "CACHE-TRIM-HINT" not in x]
    assert strip(a) == strip(b) and any("PAUSED" in x for x in a) and any("RESUMED" in x for x in a)


def test_help_exits_zero_on_linux(monkeypatch):
    import pytest

    monkeypatch.setattr(gov.sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc:
        gov.main(["--help"])
    assert exc.value.code == 0
