"""Pins the run-baton.json record: round trip, normalisation and refusals."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from pathlib import Path

BATON = "state/handoffs/2026-10-21-x.md"


def test_round_trip(tmp_path: Path) -> None:
    path = c.write_run_baton(tmp_path, "run-1", BATON)
    assert path.name == c.RUN_BATON_FILE == "run-baton.json"
    assert path.parent == c.record_dir(tmp_path, "run-1")
    assert json.loads(path.read_text(encoding="utf-8")) == {"baton": BATON}
    assert c.read_run_baton(tmp_path, "run-1") == BATON


def test_backslash_in_forward_slash_out(tmp_path: Path) -> None:
    path = c.write_run_baton(tmp_path, "run-1", "state\\handoffs\\x.md")
    assert json.loads(path.read_text(encoding="utf-8")) == {"baton": "state/handoffs/x.md"}
    path.write_text(json.dumps({"baton": "state\\handoffs\\x.md"}), encoding="utf-8")
    assert c.read_run_baton(tmp_path, "run-1") == "state/handoffs/x.md"


@pytest.mark.parametrize("run_id", ["../evil", "a/b", ""])
def test_unsafe_run_id(tmp_path: Path, run_id) -> None:
    with pytest.raises(ValueError):
        c.write_run_baton(tmp_path, run_id, BATON)
    assert c.read_run_baton(tmp_path, run_id) is None


# abs-path-ok: refusal inputs, never touched on disk
@pytest.mark.parametrize("bad", ["", "  ", "/abs/x.md", "C:\\x.md", "C:/x.md", "../x.md", "a/../b.md", "a\\..\\b.md"])
def test_unsafe_baton_refused_on_write_and_read(tmp_path: Path, bad) -> None:
    with pytest.raises(ValueError):
        c.write_run_baton(tmp_path, "run-1", bad)
    assert not (c.record_dir(tmp_path, "run-1") / c.RUN_BATON_FILE).exists()
    d = c.record_dir(tmp_path, "run-1")
    d.mkdir(parents=True)
    (d / c.RUN_BATON_FILE).write_text(json.dumps({"baton": bad}), encoding="utf-8")
    assert c.read_run_baton(tmp_path, "run-1") is None


def test_non_str_baton_reads_none(tmp_path: Path) -> None:
    d = c.record_dir(tmp_path, "run-1")
    d.mkdir(parents=True)
    (d / c.RUN_BATON_FILE).write_text(json.dumps({"baton": 7}), encoding="utf-8")
    assert c.read_run_baton(tmp_path, "run-1") is None


def test_missing_and_malformed_file(tmp_path: Path) -> None:
    assert c.read_run_baton(tmp_path, "run-1") is None
    d = c.record_dir(tmp_path, "run-1")
    d.mkdir(parents=True)
    (d / c.RUN_BATON_FILE).write_text("{not json", encoding="utf-8")
    assert c.read_run_baton(tmp_path, "run-1") is None
