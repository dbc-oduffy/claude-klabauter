"""A ledger-check halt lands the record only: chunk product is withheld halt-record-only, nothing stamped coded."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
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


def test_ledger_check_halt_withholds_every_chunk_and_lands_record(repo):
    _two_chunks(repo, 'x = "a\nb"\n')

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"] == {"bad.py": "halt-record-only", "good.py": "halt-record-only"}
    assert "landed_chunks" not in out and "integrity" not in out
    assert out["lands_chunks_stamp"] is None
    names = _landed(repo, out, "--name-only")
    assert "good.py" not in names and "bad.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "Lands-Chunks" not in _git(["log", "-1", "--format=%B"], repo)


def test_clean_chunk_is_still_withheld_halt_record_only(repo):
    _write_record(repo, RUN)
    (repo / "good.py").write_text("g = 1\n", encoding="utf-8")
    _marker_script(repo, [("C2", ["good.py"])], DONE)

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"good.py": "halt-record-only"}
    assert "landed_chunks" not in out and "integrity" not in out
    assert out["lands_chunks_stamp"] is None
