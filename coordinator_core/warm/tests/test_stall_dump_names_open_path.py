"""The stall dump (full record and stub) names the oldest in-flight bounded read."""

from __future__ import annotations

import time

from coordinator_core import bounded_io
from coordinator_core.warm import server


def _header(path, prefix="open-path:"):
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1
    return lines[0]


def _register(monkeypatch, entries):
    monkeypatch.setattr(bounded_io, "inflight_open_paths", lambda: entries)


def test_full_record_names_oldest_open_path(tmp_path, monkeypatch):
    now = time.monotonic()
    _register(
        monkeypatch,
        [bounded_io.OpenPath("/hot/oldest.md", now - 5.0), bounded_io.OpenPath("/hot/newer.md", now - 1.0)],
    )
    out = server._write_stall_record(tmp_path, None, key="k", full=True, sample_box=False)
    line = _header(out)
    assert line.startswith("open-path: /hot/oldest.md ")
    assert int(line.rsplit(" ", 1)[1][:-2]) >= 5000


def test_full_record_names_none_when_empty(tmp_path, monkeypatch):
    _register(monkeypatch, [])
    out = server._write_stall_record(tmp_path, None, key="k", full=True, sample_box=False)
    assert _header(out) == "open-path: none"


def test_stub_names_open_path_and_none(tmp_path, monkeypatch):
    _register(monkeypatch, [bounded_io.OpenPath("/hot/a.md", time.monotonic() - 2.0)])
    stub = server._write_stall_record(tmp_path, None, key="s1", full=False)
    assert _header(stub).startswith("open-path: /hot/a.md ")
    _register(monkeypatch, [])
    stub = server._write_stall_record(tmp_path, None, key="s2", full=False)
    assert _header(stub) == "open-path: none"


def test_open_path_line_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(bounded_io, "inflight_open_paths", boom)
    assert server._open_path_line() == "open-path: none\n"
