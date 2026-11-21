"""The entry script the launcher runs prints a typed ERROR admit verdict when its helper module cannot load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

SCRIPT = Path(__file__).resolve().parents[1] / "box-memory-snapshot.py"


def test_exception_after_typed_json_does_not_print_a_second_object(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("box_memory_snapshot_dup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def late_failure(argv=None, **kwargs):
        mod._print_admit_error("already refused", 12.0, verdict="HOLD")
        raise RuntimeError("late")

    monkeypatch.setattr(mod, "_main", late_failure)
    assert mod.main(["--json", "--admit-peak", "12"]) == mod.EXIT_ERROR
    out = capsys.readouterr().out
    assert json.loads(out)["admit"]["verdict"] == "HOLD"
