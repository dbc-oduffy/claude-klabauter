"""The ask-spool benchmark meets the falsifier on all three cases and fails a case that does not."""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import ask_spool_write_budget as B

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.skipif(os.name == "nt", reason="the benchmark drives the POSIX door"),
]


@pytest.fixture(scope="module")
def report() -> dict:
    return B.measure(runs=3)


def test_every_case_spools_one_0600_entry_under_the_bar_and_drains_once(report):
    assert set(report) == set(B.CASES)
    for name, res in report.items():
        assert B.case_passes(res), (name, res)


def test_the_second_drain_mints_nothing_and_the_stub_engine_saw_one_mint(report):
    for name, res in report.items():
        assert res["drain"]["second"]["minted"] == 0, name
        assert res["drain"]["stub_mints"] == 1, name
        assert res["drain"]["notice_lines"] == 1, name


def _passing() -> dict:
    return {
        "spool_entries": 1, "mode": "0600", "process_ms": 5.0, "names_spool_id": True,
        "drain": {"first": {"minted": 1, "notified": 1}, "second": {"minted": 0}},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"spool_entries": 0},
        {"mode": "0644"},
        {"process_ms": B.BAR_MS},
        {"names_spool_id": False},
        {"drain": {"first": {"minted": 0, "notified": 1}, "second": {"minted": 0}}},
        {"drain": {"first": {"minted": 1, "notified": 0}, "second": {"minted": 0}}},
        {"drain": {"first": {"minted": 1, "notified": 1}, "second": {"minted": 1}}},
    ],
)
def test_case_passes_refuses_each_way_a_case_can_miss(mutation):
    assert B.case_passes(_passing())
    assert not B.case_passes({**_passing(), **mutation})
