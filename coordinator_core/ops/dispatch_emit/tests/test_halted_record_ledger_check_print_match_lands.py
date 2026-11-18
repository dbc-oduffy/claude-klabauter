"""A ledger-check halt with no passing test verdict lands a finished chunk whose bytes equal the run's recorded print."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "test_misroute.py"
B = "test_rule_scan.py"
TEXT = {A: "def test_a():\n    assert True\n", B: "def test_b():\n    assert True\n"}
ERRORED = {"targeted_tests": {"status": "error", "tests_failed": 0, "baseline": None}}


def _setup(repo, rows, prints, stopped=()):
    rec = _write_record(repo, RUN)
    (rec / "manifest.json").write_text(
        json.dumps({"rows": [], "write_baseline": {p: None for p in TEXT}}), encoding="utf-8"
    )
    for path, text in TEXT.items():
        (repo / path).write_bytes(text.encode("utf-8"))
    _marker_script(repo, rows, {r: f"DONE: {r}\n" for r, _ in rows})
    (rec / "withheld-prints.json").write_text(json.dumps(prints), encoding="utf-8")
    return {**LEDGER, **({"stopped_rows": list(stopped)} if stopped else {})}


def _prints(**overrides):
    return {p: overrides.get(p, blob_sha(t.encode("utf-8"))) for p, t in TEXT.items()}


def test_print_matching_product_lands_when_the_test_leg_errored(repo):
    halted = _setup(repo, [("C1", [A, B])], _prints())

    out = _call(repo, halted, **ERRORED)

    assert out["committed"] is True, out
    assert not {A, B} & set(out.get("withheld", {}))
    assert not out.get("withheld_causes")
    assert {A, B} <= set(_landed(repo, out, "--name-only"))


def test_print_matching_product_lands_with_no_verdict_at_all(repo):
    halted = _setup(repo, [("C1", [A, B])], _prints())

    out = _call(repo, halted)

    assert {A, B} <= set(_landed(repo, out, "--name-only"))


def test_blob_differing_from_its_print_stays_withheld_and_its_chunk_with_it(repo):
    halted = _setup(repo, [("C1", [A, B])], _prints(**{B: blob_sha(b"edited since the print\n")}))

    out = _call(repo, halted, **ERRORED)

    assert out["withheld"][A] == out["withheld"][B] == "halt-record-only"
    assert out["withheld_causes"] == {A: "split-chunk", B: "print-mismatch"}
    assert not {A, B} & set(_landed(repo, out, "--name-only"))


def test_stopped_row_stays_withheld_though_its_print_matches(repo):
    halted = _setup(repo, [("C1", [A]), ("C2", [B])], _prints(), stopped=["C2"])

    out = _call(repo, halted, **ERRORED)

    assert out["withheld_causes"] == {B: "stopped-row"}
    names = set(_landed(repo, out, "--name-only"))
    assert A in names and B not in names


def test_no_recorded_print_stays_withheld_with_a_named_cause(repo):
    halted = _setup(repo, [("C1", [A, B])], {})

    out = _call(repo, halted, **ERRORED)

    assert out["withheld"][A] == "halt-record-only"
    assert out["withheld_causes"] == {A: "no-recorded-print", B: "no-recorded-print"}


def test_refusal_halt_never_lands_on_a_print_match(repo):
    _setup(repo, [("C1", [A, B])], _prints())

    out = _call(repo, None, **PASSING)

    assert out["withheld_causes"] == {A: "not-ledger-check-halt", B: "not-ledger-check-halt"}
    assert not {A, B} & set(_landed(repo, out, "--name-only"))
