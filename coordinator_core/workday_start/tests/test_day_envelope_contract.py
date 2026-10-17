"""Pins the envelope shape `day_envelope` hands the workday-start driver (`driver._envelope_outcome`).

Stubs only through the public `READERS` tuple, so the pin holds across a rewrite of the producer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ceremony_common.run import envelope as envelope_mod
from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.contract.decision_object.reader_result import build_directive
from coordinator_core.workday_start import day_envelope as de

ALLOWED_KEYS = {"directives", "judgment_points", "reader_errors"}
DIRECTIVE_KEYS = {"id", "cli", "args", "depends_on", "already_satisfied", "detail"}
POINT_KEYS = {"id", "question", "dispositions", "evidence", "recommendation", "revalidate_at_dispatch", "round_trip"}


def _silent(repo_root: Path):
    return [], []


def _directive_reader(repo_root: Path):
    return [build_directive("d-contract-pin", "some-cli", ["--flag"], "detail text")], []


def _point_reader(repo_root: Path):
    point = build_judgment_point(
        None,
        id="j-contract-pin",
        question="Pin this?",
        dispositions=[build_disposition("yes"), build_disposition("no")],
        evidence="evidence text",
        reason="recommendation-forbidden",
        reportable=True,
    )
    return [], [point]


def _raising(repo_root: Path):
    raise RuntimeError("boom")


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, readers) -> dict:
    monkeypatch.setattr(de, "READERS", tuple(readers))
    code, text = de.day_envelope(tmp_path)
    assert code == 0
    assert isinstance(text, str)
    parsed = envelope_mod.parse(text)
    assert parsed == json.loads(text)
    assert set(parsed) <= ALLOWED_KEYS
    for key, value in parsed.items():
        assert value, f"{key} present but empty; a silent member is omitted"
    return parsed


def test_silent_readers_emit_empty_object(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, [("silent", _silent)]) == {}


def test_directive_and_point_round_trip_builder_keys(monkeypatch, tmp_path):
    parsed = _run(monkeypatch, tmp_path, [("d", _directive_reader), ("j", _point_reader), ("s", _silent)])
    assert set(parsed) == {"directives", "judgment_points"}
    (directive,) = parsed["directives"]
    assert DIRECTIVE_KEYS <= set(directive)
    assert directive == _directive_reader(tmp_path)[0][0]
    (point,) = parsed["judgment_points"]
    assert POINT_KEYS <= set(point)
    assert point == _point_reader(tmp_path)[1][0]


def test_directive_only_omits_judgment_points(monkeypatch, tmp_path):
    parsed = _run(monkeypatch, tmp_path, [("d", _directive_reader)])
    assert set(parsed) == {"directives"}


def test_failing_reader_is_named_and_does_not_hide_others(monkeypatch, tmp_path):
    parsed = _run(monkeypatch, tmp_path, [("bad", _raising), ("d", _directive_reader)])
    assert [d["id"] for d in parsed["directives"]] == ["d-contract-pin"]
    (error,) = parsed["reader_errors"]
    assert isinstance(error, str) and error.startswith("bad")


def test_exit_zero_when_every_reader_fails(monkeypatch, tmp_path):
    parsed = _run(monkeypatch, tmp_path, [("bad", _raising)])
    assert set(parsed) == {"reader_errors"}
