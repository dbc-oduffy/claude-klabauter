
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
        return {"process_time_ms": 1, "procs_per_call": 1, "instrument": "x", "k": k}

    monkeypatch.setattr(measure, "_run", fake_run)
    for cmd in (
        [sys.executable, "-m", "pytest", "coordinator_core/benchmarks/tests/test_measure_cli.py"],
        [sys.executable, "-c", "pass"],
    ):
        assert measure.main(["--k", "1", "--", *cmd]) == 0
    assert len(seen) == 2
