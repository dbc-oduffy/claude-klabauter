"""Tests for coordinator_core.warm.telemetry.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C26
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import telemetry


@pytest.fixture(autouse=True)
def _short_warm_runtime_base(monkeypatch: pytest.MonkeyPatch):
    """Overrides the suite-wide HOME quarantine's `warm-runtime-base`
    (`coordinator_core/conftest.py::_quarantine_real_home`) with a short,
    real on-disk root under `/tmp`.

    Only `test_try_warm_dispatch_does_not_record_on_a_served_response`
    below drives the real `client.try_warm_dispatch` preamble
    (`election.socket_path`), but the quarantine's own path is already
    90+ bytes deep on macOS before `coordinator/warm/<16-hex-hash>/
    <token>.sock` is appended, tripping `election.SUN_PATH_MAX_BYTES`
    (100) before that test's own assertion runs. Applied autouse for
    uniformity with this dispatch's sibling files -- every other test in
    this module writes telemetry rows directly and never touches
    `election`, so the override is a no-op for them. Same fix as
    `test_election_posix.py::short_runtime_base` (committed b4e300c8f1);
    duplicated here rather than lifted into a shared `conftest.py`
    because this dispatch's scope is this file only.
    """
    from coordinator_core.warm import breadcrumb

    base = Path(tempfile.mkdtemp(prefix="wrb-", dir="/tmp"))
    try:
        monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_invocation_counts_warm_and_cold_separately():
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=True)
    t.record_invocation(warm=False)

    snap = t.snapshot()
    assert snap["served_count"] == 3
    assert snap["warm_count"] == 2
    assert snap["cold_count"] == 1


def test_record_invocation_returns_running_served_count():
    t = telemetry.ServerTelemetry()
    assert t.record_invocation(warm=True) == 1
    assert t.record_invocation(warm=False) == 2
    assert t.record_invocation(warm=True) == 3


def test_served_count_is_zero_arg_and_matches_snapshot():
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=True)

    assert t.served_count() == 2 == t.snapshot()["served_count"]


def test_served_count_binds_directly_into_idle_served_count_fn():
    """`served_count` must satisfy `idle.ServedCountFn` -- a zero-arg
    callable returning the served-invocation count -- with no adapter,
    per this module's own docstring."""
    from coordinator_core.warm import idle

    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)

    served_count: idle.ServedCountFn = t.served_count
    assert served_count() == 1


def test_record_exit_accepts_each_known_reason():
    for reason in telemetry.EXIT_REASONS:
        t = telemetry.ServerTelemetry()
        t.record_exit(reason)
        assert t.snapshot()["exit_reason"] == reason


def test_record_exit_rejects_unknown_reason():
    t = telemetry.ServerTelemetry()
    with pytest.raises(ValueError):
        t.record_exit("some-other-reason")


def test_server_boot_row_carries_both_instants(tmp_path):
    """THE UNCENSORED MEASUREMENT. Every client-side estimate of boot is
    bounded by when callers happened to call; this row is written by the
    booting process itself, with no caller in it. Two instants, because an
    endpoint that is bound will accept a connection while the op registry is
    still importing -- reaching the first is not being answered by the
    second."""
    telemetry.record_server_boot(listener_secs=0.41, ready_secs=1.87, pid=1234, engine_root=tmp_path)

    rows = telemetry.server_boot_samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["listener_secs"] == 0.41
    assert rows[0]["ready_secs"] == 1.87
    assert rows[0]["pid"] == 1234


def test_server_boot_samples_absent_file_reads_empty(tmp_path):
    """No rows is not an error, and must not read as a measurement either --
    an absent file means no stamped spawn has booted yet."""
    assert telemetry.server_boot_samples(tmp_path / "nothing-here") == []


def test_cold_row_carries_op_and_pid_when_supplied(tmp_path):
    """A BURST HAS TO BE ATTRIBUTABLE. 1600 rows in 13 seconds (2026-08-25)
    could name no process and no op, so the defect they reported could not be
    chased. Both travel on the row now."""
    telemetry.record_client_cold_fallback(engine_root=tmp_path, op="memo.check_addressee", pid=4321)

    rows = [
        json.loads(line)
        for line in telemetry.client_cold_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["op"] == "memo.check_addressee"
    assert rows[0]["pid"] == 4321
    assert "ts" in rows[0]


def test_cold_row_omits_op_and_pid_when_unknown(tmp_path):
    """Omitted, never invented: six days of rows predate these keys, and a
    caller that cannot name its op must not be made to fabricate one."""
    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    row = json.loads(telemetry.client_cold_path(tmp_path).read_text(encoding="utf-8").strip())
    assert set(row) == {"ts"}


def test_exit_detail_is_absent_unless_recorded():
    """Seven days of rows predate this field. An absent key keeps them and
    every reader of them working unchanged, so the field costs nothing to
    anyone who does not ask for it."""
    t = telemetry.ServerTelemetry()
    t.record_exit(telemetry.EXIT_REASON_IDLE_DEMOTION)

    assert "exit_detail" not in t.snapshot()


def test_exit_detail_carries_the_skew_axis():
    """WHAT THE COLLAPSED COUNT COULD NOT SAY. `skew` was the largest exit
    reason on this box, and it fires from either the source-hash axis or the
    build-stamp axis -- two mechanisms whose remediations point in opposite
    directions (something editing engine source in the serving clone, versus
    the publish cadence stranding servers). The reason alone sends the next
    reader at whichever one they already suspected."""
    t = telemetry.ServerTelemetry()
    t.record_exit(telemetry.EXIT_REASON_SKEW, "source,token")

    snap = t.snapshot()
    assert snap["exit_reason"] == telemetry.EXIT_REASON_SKEW
    assert snap["exit_detail"] == "source,token"


def test_record_exit_first_call_wins():
    t = telemetry.ServerTelemetry()
    t.record_exit(telemetry.EXIT_REASON_SKEW)
    t.record_exit(telemetry.EXIT_REASON_IDLE_DEMOTION)

    assert t.snapshot()["exit_reason"] == telemetry.EXIT_REASON_SKEW


def test_snapshot_before_any_exit_has_none_reason():
    t = telemetry.ServerTelemetry()
    assert t.snapshot()["exit_reason"] is None


def test_snapshot_life_seconds_advances_with_injected_clock():
    ticks = iter([100.0, 137.5])
    t = telemetry.ServerTelemetry(clock=lambda: next(ticks))

    assert t.snapshot()["life_seconds"] == pytest.approx(37.5)


def test_record_invocation_is_thread_safe_under_concurrency():
    t = telemetry.ServerTelemetry()
    threads = [threading.Thread(target=t.record_invocation, kwargs={"warm": True}) for _ in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert t.served_count() == 50


def test_telemetry_path_lives_in_the_clone_s_runtime_dir(tmp_path):
    """Telemetry follows `breadcrumb.svc_dir` wherever it resolves — the two
    are deliberately one seam, so the 2026-08-19 move out of the engine
    clone carried both files rather than splitting them."""
    from coordinator_core.warm.breadcrumb import svc_dir

    path = telemetry.telemetry_path(tmp_path)
    assert path == svc_dir(tmp_path) / telemetry.TELEMETRY_FILENAME
    assert tmp_path not in path.parents


def test_flush_appends_one_json_line_with_snapshot_fields(tmp_path):
    t = telemetry.ServerTelemetry()
    t.record_invocation(warm=True)
    t.record_invocation(warm=False)
    t.record_exit(telemetry.EXIT_REASON_IDLE_DEMOTION)

    t.flush(engine_root=tmp_path)

    path = telemetry.telemetry_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["served_count"] == 2
    assert record["warm_count"] == 1
    assert record["cold_count"] == 1
    assert record["exit_reason"] == telemetry.EXIT_REASON_IDLE_DEMOTION
    assert "flushed_at" in record
    assert "life_seconds" in record


def test_flush_appends_across_multiple_server_lives(tmp_path):
    first = telemetry.ServerTelemetry()
    first.record_invocation(warm=True)
    first.record_exit(telemetry.EXIT_REASON_SKEW)
    first.flush(engine_root=tmp_path)

    second = telemetry.ServerTelemetry()
    second.record_invocation(warm=False)
    second.record_exit(telemetry.EXIT_REASON_OPERATOR_STOP)
    second.flush(engine_root=tmp_path)

    path = telemetry.telemetry_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    reasons = [json.loads(line)["exit_reason"] for line in lines]
    assert reasons == [telemetry.EXIT_REASON_SKEW, telemetry.EXIT_REASON_OPERATOR_STOP]


def test_flush_creates_svc_dir_when_absent(tmp_path):
    t = telemetry.ServerTelemetry()
    t.flush(engine_root=tmp_path)

    assert telemetry.telemetry_path(tmp_path).exists()


def test_flush_never_raises_on_write_failure(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.Path, "mkdir", _boom)

    t = telemetry.ServerTelemetry()
    t.flush(engine_root=tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# C1: client-side cold-fallback counter (AC4 first half).
# ---------------------------------------------------------------------------


def test_client_cold_count_is_zero_with_no_recorded_fallback(tmp_path):
    assert telemetry.client_cold_count(tmp_path) == 0


def test_record_client_cold_fallback_increments_the_counter(tmp_path):
    telemetry.record_client_cold_fallback(engine_root=tmp_path)
    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    assert telemetry.client_cold_count(tmp_path) == 2


def test_client_cold_count_reachable_without_a_server_round_trip(tmp_path):
    """The counter is a plain file read -- no warm pipe, no running
    server, no `ServerTelemetry` instance involved at all."""
    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    path = telemetry.client_cold_path(tmp_path)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ts" in record

    assert telemetry.client_cold_count(tmp_path) == 1


def test_record_client_cold_fallback_never_raises_on_write_failure(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.Path, "mkdir", _boom)

    telemetry.record_client_cold_fallback(engine_root=tmp_path)  # must not raise


def test_try_warm_dispatch_records_cold_fallback(tmp_path, monkeypatch):
    """The actual client-side call site: `try_warm_dispatch` falling cold
    (warmth disabled -- the simplest cold-fallback outcome) increments the
    on-disk counter with no server involved."""
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "is_warm_enabled", lambda: False)
    monkeypatch.setattr(client, "_engine_clone_root", lambda: tmp_path)

    assert telemetry.client_cold_count(tmp_path) == 0
    result = client.try_warm_dispatch({"jsonrpc": "2.0", "method": "ping", "id": 1})

    assert result is None
    assert telemetry.client_cold_count(tmp_path) == 1


def test_try_warm_dispatch_does_not_record_on_a_served_response(tmp_path, monkeypatch):
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: "irrelevant")
    monkeypatch.setattr(client, "engine_token", lambda: "tok")
    monkeypatch.setattr(client, "_caller_session_id", lambda: "")

    response_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n"

    class _FakeFh:
        def write(self, data):
            pass

        def flush(self):
            pass

        def readline(self):
            return response_line.encode("utf-8")

        def close(self):
            pass

    monkeypatch.setattr(client, "_open_pipe", lambda pipe: _FakeFh())

    result = client.try_warm_dispatch({"jsonrpc": "2.0", "method": "ping", "id": 1})

    assert result == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert telemetry.client_cold_count(tmp_path) == 0


# ---------------------------------------------------------------------------
# C2: one-command warm rate (AC4 second half).
# ---------------------------------------------------------------------------


def test_warm_rate_is_none_with_no_recorded_outcomes(tmp_path):
    status = telemetry.warm_rate(tmp_path)
    assert status["warm_count"] == 0
    assert status["cold_count"] == 0
    assert status["total"] == 0
    assert status["warm_rate"] is None


def test_warm_rate_reflects_an_injected_mix_of_warm_and_cold(tmp_path):
    server = telemetry.ServerTelemetry()
    for _ in range(3):
        server.record_invocation(warm=True)
    server.flush(engine_root=tmp_path)

    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    status = telemetry.warm_rate(tmp_path)
    assert status["warm_count"] == 3
    assert status["cold_count"] == 1
    assert status["total"] == 4
    assert status["warm_rate"] == pytest.approx(0.75)


def test_warm_rate_changes_when_the_mix_changes(tmp_path):
    """The reported rate must move with the underlying data, not read a
    constant -- a second, differently-mixed sample must yield a
    different rate."""
    server = telemetry.ServerTelemetry()
    server.record_invocation(warm=True)
    server.flush(engine_root=tmp_path)
    telemetry.record_client_cold_fallback(engine_root=tmp_path)
    telemetry.record_client_cold_fallback(engine_root=tmp_path)
    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    first = telemetry.warm_rate(tmp_path)
    assert first["warm_rate"] == pytest.approx(0.25)

    other_root = tmp_path / "other"
    other_root.mkdir()
    server2 = telemetry.ServerTelemetry()
    for _ in range(9):
        server2.record_invocation(warm=True)
    server2.flush(engine_root=other_root)
    telemetry.record_client_cold_fallback(engine_root=other_root)

    second = telemetry.warm_rate(other_root)
    assert second["warm_rate"] == pytest.approx(0.9)
    assert second["warm_rate"] != first["warm_rate"]


def test_warm_rate_sums_across_multiple_server_lives(tmp_path):
    first = telemetry.ServerTelemetry()
    first.record_invocation(warm=True)
    first.record_invocation(warm=True)
    first.flush(engine_root=tmp_path)

    second = telemetry.ServerTelemetry()
    second.record_invocation(warm=True)
    second.flush(engine_root=tmp_path)

    status = telemetry.warm_rate(tmp_path)
    assert status["warm_count"] == 3
    assert status["total"] == 3


def test_warm_rate_reachable_without_a_server_round_trip(tmp_path):
    telemetry.record_client_cold_fallback(engine_root=tmp_path)

    status = telemetry.warm_rate(tmp_path)
    assert status["cold_count"] == 1
    assert status["warm_rate"] == pytest.approx(0.0)
