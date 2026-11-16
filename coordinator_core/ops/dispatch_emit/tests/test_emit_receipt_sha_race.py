"""Two overlapping emits of one run id leave a receipt whose sha256 is the script's, and a genuine
mismatch verdict names both digests and the remedy."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from coordinator_core import locked_write
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.ops.dispatch_emit.tests.test_chain_emit_minted_run_id import _TORN, _verify_sequence
from pathlib import Path

pytestmark = pytest.mark.complete

_SCRIPT_A = "// emitter A\n" + "a\n" * 2000
_SCRIPT_B = "// emitter B\n" + "b\n" * 3000


def _emit(path, script):
    return op_module._emit_script_file(
        path,
        script,
        receipt_plan_path=None,
        params={"force": True},
        receipt_extras=None,
        plan_findings=[],
        session_id="s",
        agent_type_host=None,
        before_write=lambda: None,
    )


def test_overlapping_emits_leave_a_receipt_that_names_the_script_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locked_write, "_machine_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr(op_module, "check_agent_types_resolve", lambda *a, **k: None)
    monkeypatch.setattr(op_module, "run_checks", lambda script: [])
    path = tmp_path / "scratch" / "warp" / "ask-r1.workflow.mjs"

    real_receipt = op_module._write_emission_receipt
    first_in_stall = threading.Event()
    stalled = []

    def stalling_receipt(*args, **kwargs):
        # The stall sits between the script write and the receipt write, once, in the first emitter.
        if not stalled:
            stalled.append(True)
            first_in_stall.set()
            time.sleep(0.4)
        return real_receipt(*args, **kwargs)

    monkeypatch.setattr(op_module, "_write_emission_receipt", stalling_receipt)

    seen_partial = []
    stop = threading.Event()
    whole = {_SCRIPT_A.encode("utf-8"), _SCRIPT_B.encode("utf-8")}

    def reader() -> None:
        while not stop.is_set():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if data not in whole:
                seen_partial.append(len(data))

    errors = []

    def emitter(script) -> None:
        try:
            _emit(path, script)
        except Exception as exc:  # noqa: BLE001 -- surfaced by the assert below
            errors.append(exc)

    path.parent.mkdir(parents=True)
    watcher = threading.Thread(target=reader)
    first = threading.Thread(target=emitter, args=(_SCRIPT_A,))
    second = threading.Thread(target=emitter, args=(_SCRIPT_B,))
    watcher.start()
    first.start()
    assert first_in_stall.wait(10)
    second.start()
    first.join(30)
    second.join(30)
    stop.set()
    watcher.join(10)

    assert errors == []
    on_disk = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = json.loads(emission_receipt_path(path).read_text(encoding="utf-8"))
    assert receipt["sha256"] == on_disk
    assert seen_partial == []


def test_a_genuine_mismatch_verdict_names_both_digests_and_the_remedy() -> None:
    out = _verify_sequence([_TORN])
    error = out["v"]["error"]
    assert "emit receipt sha mismatch" in error
    assert "ab" * 32 in error and "cd" * 32 in error
    assert "re-emit" in error
