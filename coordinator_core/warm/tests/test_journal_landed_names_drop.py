"""`_journal_landed` names every journal outcome dropped behind a stalled journal, not just the first."""

from __future__ import annotations

import threading

from coordinator_core import bounded_io
from coordinator_core.warm import dispatch_ack, server


def test_every_dropped_journal_outcome_is_named_on_stderr(monkeypatch, tmp_path, capsys):
    journal = str(tmp_path / "journal")
    release = threading.Event()
    monkeypatch.setattr(dispatch_ack, "journal_dir_beside", lambda claim_dir: journal)
    monkeypatch.setattr(server, "_op_may_mutate", lambda method: True)
    monkeypatch.setattr(server, "_effective_method", lambda msg: "x.y")
    monkeypatch.setattr(dispatch_ack, "record_outcome", lambda *a, **k: release.wait(10))
    real = bounded_io.run_bounded
    monkeypatch.setattr(
        bounded_io, "run_bounded", lambda key, fn, deadline=0.05, default=None: real(key, fn, deadline=0.05, default=default)
    )
    try:
        server._journal_landed({}, "claims", "dk-1", {"result": {"ok": 1}})
        server._journal_landed({}, "claims", "dk-2", {"error": {"code": -1, "message": "boom"}})
        err = capsys.readouterr().err
        assert "dispatch_key=dk-1 kind=result" in err
        assert "dispatch_key=dk-2 kind=error" in err
        assert f"journal={journal}" in err
    finally:
        release.set()
        for t in [t for t in threading.enumerate() if t.name.startswith("bounded-io:")]:
            t.join(2.0)


def test_a_landed_outcome_is_not_named_as_dropped(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dispatch_ack, "journal_dir_beside", lambda claim_dir: str(tmp_path / "j"))
    monkeypatch.setattr(server, "_op_may_mutate", lambda method: True)
    monkeypatch.setattr(server, "_effective_method", lambda msg: "x.y")
    monkeypatch.setattr(dispatch_ack, "record_outcome", lambda *a, **k: True)
    server._journal_landed({}, "claims", "dk-ok", {"result": {"ok": 1}})
    assert "dropped" not in capsys.readouterr().err
