
from __future__ import annotations

import re
import sys

from coordinator_core.benchmarks import measure

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
