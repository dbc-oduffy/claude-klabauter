"""The tracked `state/run-evidence/<run>/outcome.json` writer emits LF bytes on every host: the repo declares
`eol=lf` for the path, and g16 fails a CRLF producer."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import run_artifacts


def test_tracked_outcome_is_written_with_lf_only(tmp_path, monkeypatch):
    real_write_text = type(tmp_path).write_text

    def crlf_host_write_text(self, data, encoding=None, errors=None, newline=None):
        # A Windows text-mode write translates "\n" to "\r\n" unless newline is pinned.
        if newline is None:
            data = data.replace("\n", "\r\n")
            newline = ""
        return real_write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(type(tmp_path), "write_text", crlf_host_write_text)

    rel = run_artifacts.write_tracked_outcome(tmp_path, "run-lf-1", "done", landed=2)

    assert rel == run_artifacts.outcome_rel("run-lf-1")
    raw = (tmp_path / rel).read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")
    assert json.loads(raw)["state"] == "done"
