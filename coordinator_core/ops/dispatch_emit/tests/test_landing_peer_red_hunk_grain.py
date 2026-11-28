"""The landing test gate judges a red per peer hunk: with one split and one unsplit peer path, a red the
green overlay ran and whose reach excludes every unsplit path is advisory; one reaching an unsplit path
refuses; an unreadable reach falls back to refusing."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_reach
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

SPLIT, UNSPLIT, TEST = "pkg/a.py", "pkg/b.py", "t/test_a.py"
PEERS = {
    SPLIT: {"owner": "peer-1", "lines": "1-2", "split": True},
    UNSPLIT: {"owner": "peer-2", "lines": "3-4", "split": False},
}


def _record(root, peers=PEERS, *, verdict="green", seeds=(TEST,)):
    record = {"sha": "x", "base_sha": "b", "blobs": {}, "peer_hunks": peers}
    path = measured_rev_record_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    cov.write_verdict(root, "r1", record, cov.OverlayVerdict(verdict, "tail", tuple(seeds), False))


def _red_on(monkeypatch, test_file=TEST):
    monkeypatch.setattr(
        li, "_landing_test_gate_body",
        lambda *a, **k: ([ltt.Finding(test_file, ltt.TARGETED_TESTS_FAILED, f"failing: {test_file}::test_x")], "failed"),
    )


def _reach(monkeypatch, mapping):
    monkeypatch.setattr(landing_reach, "reach_paths", lambda root, seeds, depth=2: set(mapping[next(iter(seeds))]))


def test_a_red_reaching_only_the_split_peer_path_is_advisory(tmp_path, monkeypatch):
    _red_on(monkeypatch)
    _record(tmp_path)
    _reach(monkeypatch, {TEST: {TEST, SPLIT}})

    findings, check = li._landing_test_gate(tmp_path, {}, "r1")

    assert list(findings) == [] and check == ltt.PEER_WAIVED_CHECK
    (advised,) = findings.peer_advisory
    assert advised.reason == ltt.TARGETED_TESTS_FAILED and "peer-1" in advised.detail


def test_a_red_reaching_the_unsplit_peer_path_still_refuses(tmp_path, monkeypatch):
    _red_on(monkeypatch)
    _record(tmp_path)
    _reach(monkeypatch, {TEST: {TEST, SPLIT, UNSPLIT}})

    findings, check = li._landing_test_gate(tmp_path, {}, "r1")

    assert check == "failed" and [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]
    assert not getattr(findings, "peer_advisory", ())


def test_an_unreadable_reach_falls_back_to_all_or_nothing_and_refuses(tmp_path, monkeypatch):
    _red_on(monkeypatch)
    _record(tmp_path)

    def boom(root, seeds, depth=2):
        raise OSError("index unreadable")

    monkeypatch.setattr(landing_reach, "reach_paths", boom)

    findings, check = li._landing_test_gate(tmp_path, {}, "r1")

    assert check == "failed" and [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]


@pytest.mark.parametrize("case", ["red-verdict", "unran-file", "no-split-path"])
def test_the_per_hunk_waiver_still_needs_a_green_overlay_that_ran_the_file_and_a_split_peer(tmp_path, monkeypatch, case):
    _red_on(monkeypatch, "t/test_other.py" if case == "unran-file" else TEST)
    peers = {UNSPLIT: PEERS[UNSPLIT]} if case == "no-split-path" else PEERS
    _record(tmp_path, peers, verdict="red" if case == "red-verdict" else "green")
    _reach(monkeypatch, {TEST: {TEST, SPLIT}, "t/test_other.py": {SPLIT}})

    findings, check = li._landing_test_gate(tmp_path, {}, "r1")

    assert check == "failed" and [f.reason for f in findings] == [ltt.TARGETED_TESTS_FAILED]


def test_every_peer_split_waives_without_reading_reach(tmp_path, monkeypatch):
    _red_on(monkeypatch)
    _record(tmp_path, {SPLIT: PEERS[SPLIT]})
    monkeypatch.setattr(landing_reach, "reach_paths", lambda *a, **k: pytest.fail("reach read in the all-split case"))

    findings, check = li._landing_test_gate(tmp_path, {}, "r1")

    assert list(findings) == [] and check == ltt.PEER_WAIVED_CHECK


def test_the_pure_split_judges_each_finding_by_its_own_reach():
    clean = ltt.Finding("t/test_a.py", ltt.TARGETED_TESTS_FAILED, "failing: t/test_a.py::test_x")
    reaching = ltt.Finding("t/test_c.py", ltt.TARGETED_TESTS_FAILED, "failing: t/test_c.py::test_y")
    reach = {"t/test_a.py": {"t/test_a.py", SPLIT}, "t/test_c.py": {"t/test_c.py", UNSPLIT}}

    refusing, advisory = ltt.waive_peer_reds_by_reach(
        [clean, reaching], {SPLIT: "peer-1"}, ["t/test_a.py", "t/test_c.py"], {UNSPLIT}, lambda f: reach[f],
    )

    assert [f.path for f in refusing] == ["t/test_c.py"] and [f.path for f in advisory] == ["t/test_a.py"]


def test_split_peer_state_reads_the_flags():
    assert ltt.split_peer_state({"peer_hunks": PEERS}) == (True, frozenset({UNSPLIT}))
    assert ltt.split_peer_state({"peer_hunks": {SPLIT: PEERS[SPLIT]}}) == (True, frozenset())
    assert ltt.split_peer_state({"peer_hunks": {UNSPLIT: PEERS[UNSPLIT]}}) == (False, frozenset({UNSPLIT}))
    assert ltt.split_peer_state(None) == (False, frozenset())


def test_the_per_finding_reach_adds_no_git_spawn(tmp_path, monkeypatch):
    _red_on(monkeypatch)
    _record(tmp_path)

    spawned: list[str] = []

    def spawn(*a, **k):
        spawned.append(repr(a))
        raise OSError("no spawn")

    monkeypatch.setattr("coordinator_core.git.run.run_git", spawn)
    monkeypatch.setattr(subprocess, "run", spawn)
    monkeypatch.setattr(subprocess, "Popen", spawn)

    li._landing_test_gate(tmp_path, {}, "r1")

    assert spawned == []
