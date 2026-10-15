"""`--calls N` on the workweek-complete brief driver: N brief() calls after setup and import."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import workweek_complete_brief_full_path as driver
from coordinator_core.workweek_complete import brief as brief_module

_OK = (0, {"judgment_points": [{"id": driver.SENTINEL_ID}]})


@pytest.fixture
def counted(monkeypatch, tmp_path):
    """main() against a stub fixture dir and a counting brief(): no history build."""
    monkeypatch.setattr(driver, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(driver, "ensure_fixture", lambda parent: tmp_path)
    monkeypatch.chdir(tmp_path)
    count = {"n": 0}

    def fake():
        count["n"] += 1
        return _OK

    monkeypatch.setattr(brief_module, "brief", fake)
    return count


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["d"], 1),
        (["d", "--calls", "0"], 0),
        (["d", "--calls", "3"], 3),
        (["d", "--calls=2"], 2),
        (["--calls", "3", "d"], 3),
    ],
)
def test_call_count(counted, argv, expected):
    assert driver.main(argv) == 0
    assert counted["n"] == expected


def test_setup_only_makes_no_call(counted):
    assert driver.main(["d", "--setup-only", "--calls", "3"]) == 0
    assert counted["n"] == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["d", "--calls", "-1"],
        ["d", "--calls", "x"],
        ["d", "--calls"],
        ["d", "--calls="],
        ["d", "--calls", "3", "e"],
        ["d", "--bogus"],
        [],
    ],
)
def test_malformed_exits_2_with_usage(counted, capsys, argv):
    assert driver.main(argv) == 2
    assert "usage:" in capsys.readouterr().err
    assert counted["n"] == 0

