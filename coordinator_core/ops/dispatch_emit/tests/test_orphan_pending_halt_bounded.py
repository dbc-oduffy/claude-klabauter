"""`_orphan_pending_halt` answers no orphan inside its wall bound when a record read stalls."""

from __future__ import annotations

import threading
import time

from coordinator_core.ops.dispatch_emit import ask_gate, halt_resume_record, orphan_adopt


def test_stalled_binding_scan_answers_none_under_500ms(tmp_path, monkeypatch):
    release = threading.Event()

    def blocked(*_a, **_k):
        release.wait(10)
        return "ask-20261104T110309-625d2d"

    monkeypatch.setattr(ask_gate, "_newest_run_binding", blocked)
    t0 = time.monotonic()
    try:
        got = ask_gate._orphan_pending_halt(tmp_path, "docs/x.md", None)
    finally:
        release.set()
    assert got is None
    assert time.monotonic() - t0 < 0.5


def test_stalled_record_read_answers_none_under_500ms(tmp_path, monkeypatch):
    run = tmp_path / "state" / "warp" / "ask-20261104T110309-625d2d"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"sizing_rel": "docs/x.md"}', encoding="utf-8")
    (run / "run-stage.json").write_text('{"stage": "plan", "session_id": "dead"}', encoding="utf-8")
    release = threading.Event()

    def stalled(path):
        release.wait(10)
        return b"{}"

    monkeypatch.setattr(halt_resume_record, "_read_file", stalled)
    t0 = time.monotonic()
    try:
        got = ask_gate._orphan_pending_halt(tmp_path, "docs/x.md", None)
    finally:
        release.set()
    assert got is None
    assert time.monotonic() - t0 < 0.5


def test_fast_lookup_still_reports_the_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_gate, "_newest_run_binding", lambda *a: "ask-20261104T110309-625d2d")
    monkeypatch.setattr(
        orphan_adopt, "orphan_verdict",
        lambda *a: {"orphaned": True, "recorded_session": "dead", "stage": "plan"},
    )
    monkeypatch.setattr(orphan_adopt, "_handler", lambda *a: {"ok": False, "reason": "stub"})
    got = ask_gate._orphan_pending_halt(tmp_path, "docs/x.md", None)
    assert got is not None and got.halt is not None
