"""write_deferred_script leaves `<script>.emitted.json` beside the script."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des


def test_deferred_script_has_receipt_beside_it(tmp_path):
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    receipt = script.with_name(script.name + ".emitted.json")
    assert receipt.is_file()
    assert json.loads(receipt.read_text(encoding="utf-8"))["sha256"]


def test_receipt_failure_never_fails_the_emit(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emission_receipt

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(emission_receipt, "_write_emission_receipt", boom)
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    assert script.is_file()
    assert not script.with_name(script.name + ".emitted.json").exists()
