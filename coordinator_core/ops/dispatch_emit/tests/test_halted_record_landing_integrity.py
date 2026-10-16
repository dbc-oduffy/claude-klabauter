"""A ledger-check halt withholds a chunk whose landed .py fails to parse or drops foreign lines."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN,
    _call,
    _git,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DONE = {"C1": "DONE: C1\n", "C2": "DONE: C2\n"}


def _two_chunks(root, bad_text: str) -> None:
    _write_record(root, RUN)
    (root / "bad.py").write_text(bad_text, encoding="utf-8")
    (root / "good.py").write_text("g = 1\n", encoding="utf-8")
    _marker_script(root, [("C1", ["bad.py"]), ("C2", ["good.py"])], DONE)


def test_unparseable_py_chunk_is_withheld_and_record_lands(repo):
    _two_chunks(repo, 'x = "a\nb"\n')

    out = _call(repo, LEDGER)

    assert out["committed"] is True, out
    assert out["withheld"] == {"bad.py": "py-unparseable"}
    assert out["landed_chunks"] == ["C2"]
    assert [f["reason"] for f in out["integrity"]] == ["py-unparseable"]
    assert out["integrity"][0]["path"] == "bad.py"
    names = _landed(repo, out, "--name-only")
    assert "good.py" in names and "bad.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "Lands-Chunks: C2" in _git(["log", "-1", "--format=%B"], repo)


def test_since_base_overlap_withholds_whole_chunk(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    (repo / "good.py").write_text("g = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["a.py", "b.py"]), ("C2", ["good.py"])], DONE)
    seen = {}

    def fake(root, paths, base, *, run_id):
        seen["base"] = base
        return [landing_integrity.Finding("a.py", landing_integrity.SINCE_BASE_OVERLAP, "drops lines of abc1234")]

    monkeypatch.setattr(landing_integrity, "run_base_for", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(landing_integrity, "since_base_findings", fake)

    out = _call(repo, LEDGER)

    assert seen["base"] == "a" * 40
    assert out["withheld"] == {"a.py": "since-base-overlap", "b.py": "since-base-overlap"}
    assert out["landed_chunks"] == ["C2"]
    assert out["integrity"][0]["reason"] == "since-base-overlap"
    names = _landed(repo, out, "--name-only")
    assert "good.py" in names and "a.py" not in names and "b.py" not in names


def test_clean_chunks_land_and_no_base_skips_overlap_arm(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "good.py").write_text("g = 1\n", encoding="utf-8")
    _marker_script(repo, [("C2", ["good.py"])], DONE)
    monkeypatch.setattr(landing_integrity, "run_base_for", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("overlap arm must be skipped without a base")

    monkeypatch.setattr(landing_integrity, "since_base_findings", boom)

    out = _call(repo, LEDGER)

    assert out["landed_chunks"] == ["C2"]
    assert "integrity" not in out and "withheld" not in out
