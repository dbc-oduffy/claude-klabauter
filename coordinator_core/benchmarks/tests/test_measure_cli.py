
from __future__ import annotations

import re
import sys

import pytest

from coordinator_core.benchmarks import measure
from coordinator_core.telemetry import op_latency


@pytest.fixture(autouse=True)
def _restore_benchmark_origin_env(monkeypatch):
    # measure.main declares benchmark origin into the process-global env;
    # monkeypatch restores the pre-test value on teardown.
    # setenv first: delenv(raising=False) on an absent key records no undo, so the
    # declaration written during the test would survive teardown.
    monkeypatch.setenv(op_latency.ORIGIN_ENV, "")
    monkeypatch.delenv(op_latency.ORIGIN_ENV)


_STAMPED_RE = re.compile(
    r"^process_time_ms=\S+ procs_per_call=\S+ instrument=\S+ k=\d+ "
    r"platform=\S+ warmth=cold head=\S+$"
)


def test_default_run_prints_one_stamped_line(capsys):
    rc = measure.main(["--k", "1", "--", sys.executable, "-c", "pass"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    lines = out.splitlines()
    assert len(lines) == 1
    assert _STAMPED_RE.match(lines[0]), lines[0]
    assert "instrument=batched_process_time_ms" in lines[0]
    assert "k=1" in lines[0]


def test_once_on_linux_exits_2_and_names_not_implemented_error(capsys):
    if not sys.platform.startswith("linux"):
        return
    rc = measure.main(["--once", "--", sys.executable, "-c", "pass"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "NotImplementedError" in err


def test_empty_argv_exits_2_with_usage(capsys):
    rc = measure.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_json_flag_emits_parseable_json(capsys):
    import json

    rc = measure.main(["--k", "1", "--json", "--", sys.executable, "-c", "pass"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["instrument"] == "batched_process_time_ms"
    assert payload["k"] == 1
    assert payload["platform"] == sys.platform


_EXIT3 = [sys.executable, "-c", "import sys; sys.exit(3)"]


@pytest.mark.parametrize("flags", [["--k", "1"], ["--k", "1", "--json"]])
def test_nonzero_rc_batched_prints_no_figure(capsys, flags):
    rc = measure.main([*flags, "--", *_EXIT3])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "rc=3" in captured.err


def test_nonzero_rc_once_prints_no_figure(capsys):
    try:
        rc = measure.main(["--once", "--json", "--", *_EXIT3])
    except NotImplementedError:
        pytest.skip("--once not implemented on this platform")
    captured = capsys.readouterr()
    if rc == 2 and "NotImplementedError" in captured.err:
        pytest.skip("--once not implemented on this platform")
    assert rc == 1
    assert captured.out == ""
    assert "rc=3" in captured.err


def test_suite_shaped_argv_is_refused_before_spawn(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(measure, "_run", lambda *a, **k: called.append(a))
    rc = measure.main(["--", sys.executable, "-m", "pytest", "coordinator_core"])
    assert rc == 2
    assert not called
    assert "measure: refused:" in capsys.readouterr().err


def test_scoped_file_and_python_c_argv_reach_run(monkeypatch):
    seen = []

    def fake_run(argv, k, once):
        seen.append(argv)
        return {"process_time_ms": 1, "procs_per_call": 1, "instrument": "x", "k": k, "rc": 0}

    monkeypatch.setattr(measure, "_run", fake_run)
    for cmd in (
        [sys.executable, "-m", "pytest", "coordinator_core/benchmarks/tests/test_measure_cli.py"],
        [sys.executable, "-c", "pass"],
    ):
        assert measure.main(["--k", "1", "--", *cmd]) == 0
    assert len(seen) == 2


@pytest.fixture
def tiny_repo(tmp_path):
    from coordinator_core.git.run import run_git

    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "a.txt"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        assert run_git(args, cwd=str(repo)).ok
    return repo


def _clones(repo):
    scratch = repo / "scratch" / "benchmark-clones"
    return list(scratch.iterdir()) if scratch.exists() else []


@pytest.mark.spawns_process
def test_clone_mode_without_token_is_refused_before_mint(tiny_repo, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(measure, "single_invocation_tree_process_time", lambda *a, **k: called.append(a))
    rc = measure.main(
        ["--clone-per-run", "--", sys.executable, "-c", "pass"], source_root=tiny_repo
    )
    assert rc == 2
    assert not called
    assert not _clones(tiny_repo)
    assert "measure: refused:" in capsys.readouterr().err


@pytest.mark.spawns_process
def test_clone_mode_refuses_working_repo_target(tiny_repo, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(measure, "single_invocation_tree_process_time", lambda *a, **k: called.append(a))
    rc = measure.main(
        ["--clone-per-run", "--", sys.executable, "-c", "pass", "{clone}", str(tiny_repo)],
        source_root=tiny_repo,
    )
    assert rc == 2
    assert not called
    assert not _clones(tiny_repo)
    assert "measure: refused:" in capsys.readouterr().err


@pytest.mark.spawns_process
def test_clone_mode_mints_distinct_clones_and_removes_them(tiny_repo, capsys):
    import json
    from pathlib import Path

    rc = measure.main(
        [
            "--clone-per-run", "--k", "2", "--json", "--",
            sys.executable, "-c", "import os; print(os.getcwd())", "{clone}",
        ],
        source_root=tiny_repo,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clone_per_run"] is True
    assert payload["instrument"] == "single_invocation_tree_process_time"
    cwds = [Path(r["stdout"].strip()) for r in payload["runs"]]
    assert len(cwds) == 2 and cwds[0] != cwds[1]
    scratch = (tiny_repo / "scratch" / "benchmark-clones").resolve()
    for cwd in cwds:
        assert scratch in cwd.resolve().parents
        assert not cwd.exists()
    assert not _clones(tiny_repo)


@pytest.mark.spawns_process
def test_clone_mode_head_moving_prints_no_figure(tiny_repo, monkeypatch, capsys):
    shas = iter(["a" * 40, "b" * 40, "b" * 40])
    monkeypatch.setattr(measure, "head_sha", lambda *a, **k: next(shas))
    rc = measure.main(
        ["--clone-per-run", "--k", "1", "--", sys.executable, "-c", "pass", "{clone}"],
        source_root=tiny_repo,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "a" * 40 in captured.err and "b" * 40 in captured.err


def test_clone_mode_with_once_is_refused(capsys):
    rc = measure.main(["--clone-per-run", "--once", "--", sys.executable, "-c", "pass", "{clone}"])
    assert rc == 2
    assert "measure: refused:" in capsys.readouterr().err
