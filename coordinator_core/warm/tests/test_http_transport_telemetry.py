"""The HTTP transport records its own lives.

`supervisor.py`'s `_ServerContext` carried no `ServerTelemetry` at all, so every
death on that transport was invisible in every file on disk and every
exit-reason census over `telemetry.jsonl` was silently a census of the pipe
transport alone (advisory item 6, third bullet). The succession investigation's
own sandbox teardown found two orphaned HTTP listeners from a previous session,
outliving the clone they were spawned from, with nothing recorded anywhere.

The one hazard in fixing it is that both transports append to the SAME file: an
undifferentiated row would change the denominator under every existing reader.
Hence `transport`, present on HTTP rows and absent on pipe rows.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.warm import skew, supervisor, telemetry


def _stamped(root: Path) -> Path:
    skew.write_engine_stamp(root, "sha:http-telemetry-test")
    return root


def _ctx(root: Path) -> "supervisor._ServerContext":
    return supervisor._ServerContext(
        httpd=None, engine_root=root, version_state=skew.ServerVersionState(root)
    )


def test_pipe_rows_keep_their_exact_shape():
    """The default is what protects the ~seven days of rows already on disk:
    absence of `transport` means the pipe server, or a row written before the
    field existed. A reader separating the populations filters on presence."""
    assert "transport" not in telemetry.ServerTelemetry().snapshot()


def test_http_rows_name_their_transport():
    assert telemetry.ServerTelemetry(transport="http").snapshot()["transport"] == "http"


def test_context_flushes_a_row_on_shutdown(tmp_path: Path):
    root = _stamped(tmp_path)
    ctx = _ctx(root)
    ctx.record_invocation(True)
    ctx.record_invocation(True)
    ctx.record_exit(telemetry.EXIT_REASON_SKEW, "token")

    ctx.ctx_shutdown()

    rows = [
        row
        for row in _rows(telemetry.telemetry_path(root))
        if row.get("transport") == "http"
    ]
    assert len(rows) == 1
    assert rows[0]["served_count"] == 2
    assert rows[0]["exit_reason"] == telemetry.EXIT_REASON_SKEW
    assert rows[0]["exit_detail"] == "token"


def test_shutdown_still_unlinks_discovery_when_the_flush_fails(tmp_path: Path, monkeypatch):
    """`flush` sits BEFORE the discovery unlink, so it must not be able to cost
    it. `flush` never raises by contract -- this holds the ordering to that.

    The record names THIS process's pid because `ctx_shutdown`'s unlink is
    ownership-checked (see `unlink_discovery`): a record naming a different pid
    is a live successor's and is correctly left alone. That is a separate
    property, pinned in `test_discovery_unlink_is_ownership_checked.py`; this
    test is about flush-ordering only, so it owns the record it expects to
    remove."""
    root = _stamped(tmp_path)
    ctx = _ctx(root)
    supervisor.write_discovery(
        port=1,
        pid=os.getpid(),
        stable_pid_start_epoch=0,
        engine_sha="x",
        engine_root=root,
    )
    # THE PATCH MUST BE PATH-SCOPED, not blanket. `telemetry.locked_write` and
    # `supervisor.locked_write` are the SAME module object, and since C1 took
    # `held_lock` around `unlink_discovery`'s read-then-unlink (closing that
    # TOCTOU), a blanket patch breaks the very unlink this test asserts still
    # happens -- the test would fail for its own instrumentation rather than
    # for the ordering property it exists to pin. Fail only the telemetry
    # ledger's own lock, and let every other path take the real one.
    _real_held_lock = telemetry.locked_write.held_lock

    def _held_lock(path, *a, **k):
        if Path(path).name == "telemetry.jsonl":
            raise OSError("disk full")
        return _real_held_lock(path, *a, **k)

    monkeypatch.setattr(telemetry.locked_write, "held_lock", _held_lock)

    ctx.ctx_shutdown()

    assert supervisor.read_discovery(root) is None


def _rows(path: Path) -> list:
    import json

    try:
        with path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []
