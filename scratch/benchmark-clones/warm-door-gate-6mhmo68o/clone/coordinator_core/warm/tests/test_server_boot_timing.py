"""Boot timing: the server measures its own spawn -> ready interval.

Guards the seam between `warm.client._spawn_once` (which stamps the spawn
instant) and `warm.server._run_guarded` (which reads it). The measurement
exists because every client-side estimate of boot is censored by when callers
happened to call -- see `telemetry.record_server_boot`'s docstring for the two
readings of `client-cold.jsonl` that disagree 9x on the median, which is what
prompted it.
"""

from __future__ import annotations

from coordinator_core.warm import server, telemetry


def test_unstamped_spawn_reads_as_unmeasurable(monkeypatch):
    """A spawn route that does not stamp t0 (SessionStart's warm_start) leaves
    boot unmeasurable, and that must read as absent rather than as zero."""
    monkeypatch.delenv(telemetry.SPAWN_EPOCH_ENV, raising=False)
    assert server._spawn_epoch_from_env() is None


def test_malformed_stamp_reads_as_unmeasurable(monkeypatch):
    """Never raises, never guesses. A stamp nobody can parse means the
    measurement is unavailable, which is what None already says."""
    for bad in ("", "   ", "soon", "not-a-float"):
        monkeypatch.setenv(telemetry.SPAWN_EPOCH_ENV, bad)
        assert server._spawn_epoch_from_env() is None


def test_stamp_round_trips(monkeypatch):
    monkeypatch.setenv(telemetry.SPAWN_EPOCH_ENV, repr(1756230000.5))
    assert server._spawn_epoch_from_env() == 1756230000.5


def test_no_row_is_written_without_a_stamp(tmp_path):
    """AN INVENTED t0 WOULD BE INDISTINGUISHABLE FROM A MEASURED ONE in the
    file that exists to settle how long boot takes, so an unstamped boot
    contributes nothing rather than a fabricated zero."""
    server._record_own_boot(None, 123.0, tmp_path)

    assert telemetry.server_boot_samples(tmp_path) == []


def test_row_is_written_with_both_instants(tmp_path, monkeypatch):
    """listener_secs measures spawn -> connectable; ready_secs measures spawn
    -> will answer promptly. The gap between them is `_preload_op_registry`."""
    monkeypatch.setattr(server.time, "time", lambda: 1000.0)
    server._record_own_boot(990.0, 995.5, tmp_path)

    rows = telemetry.server_boot_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["listener_secs"] == 5.5
    assert rows[0]["ready_secs"] == 10.0


def test_a_failing_instrument_never_stops_a_boot(tmp_path, monkeypatch):
    """Sits beside the breadcrumb write and keeps its contract: a server that
    could not record its own boot still boots."""
    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry, "record_server_boot", _boom)
    server._record_own_boot(1.0, 2.0, tmp_path)  # must not raise
