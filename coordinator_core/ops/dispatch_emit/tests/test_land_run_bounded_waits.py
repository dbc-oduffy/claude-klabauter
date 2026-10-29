"""land_run is bounded by one shared deadline: a slow read ends the landing as a named halt.

The slow read advances a fake clock; nothing sleeps. Both sides of the decision are pinned: inside the
budget the landing commits, past it the next leg halts with its name and nothing is committed.
"""

from __future__ import annotations

import pathlib
from contextlib import ExitStack

import pytest

from coordinator_core.benchmarks import land_run_slow_disk_profile as prof
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import land_deadline

SLOW_SECS = 40.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(land_deadline, "_now", fake)
    return fake


def _bind(monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setenv("COORDINATOR_SESSION_ID", prof.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)


@pytest.fixture
def landing(tmp_path, monkeypatch):
    _bind(monkeypatch)
    with ExitStack() as stack:
        root, base = prof.build_land_fixture(tmp_path, mode="clean", tracked=5, chain=2, stack=stack)
        yield root, base


def _head(root) -> str:
    return prof._git(["rev-parse", "HEAD"], root).strip()


class Reads:
    """Counts `Path.read_bytes`/`read_text` calls and charges `slow_secs` of fake time on the product file."""

    def __init__(self, monkeypatch, clock, slow_secs: float) -> None:
        self.count = 0
        real_bytes, real_text = pathlib.Path.read_bytes, pathlib.Path.read_text

        def charge(path) -> None:
            self.count += 1
            if pathlib.Path(path).name == "p.py":
                clock.now += slow_secs

        def read_bytes(path):
            charge(path)
            return real_bytes(path)

        def read_text(path, *a, **k):
            charge(path)
            return real_text(path, *a, **k)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        monkeypatch.setattr(pathlib.Path, "read_text", read_text)


def test_a_landing_inside_the_budget_commits(landing, clock, monkeypatch):
    root, base = landing
    before = _head(root)
    reads = Reads(monkeypatch, clock, slow_secs=1.0)
    reply = prof._call_land(root, base)
    assert reply.get("committed") is True, reply
    assert _head(root) != before
    assert reads.count > 0


def test_a_slow_read_ends_the_landing_as_a_named_halt(landing, clock, monkeypatch):
    root, base = landing
    before = _head(root)
    Reads(monkeypatch, clock, slow_secs=SLOW_SECS)
    reply = prof._call_land(root, base)
    assert reply["committed"] is False
    assert reply["refused"] == land_deadline.REFUSED
    assert reply["leg"] == "land_run.integrity"
    assert reply["leg"] in reply["error"]
    assert _head(root) == before


def test_the_deadline_cuts_the_reads_a_stalled_landing_would_still_make(tmp_path_factory, clock, monkeypatch):
    counts = {}
    for label, budget in (("before", None), ("after", land_deadline.LAND_BUDGET_SECS)):
        with ExitStack() as stack, monkeypatch.context() as m:
            _bind(m)
            root, base = prof.build_land_fixture(
                tmp_path_factory.mktemp(label), mode="clean", tracked=5, chain=2, stack=stack,
            )
            reads = Reads(m, clock, slow_secs=SLOW_SECS)
            m.setattr(land_deadline, "LAND_BUDGET_SECS", budget)
            prof._call_land(root, base)
            counts[label] = reads.count
    print(f"reads per landing under one {SLOW_SECS}s slow read: before={counts['before']} after={counts['after']}")
    assert counts["after"] < counts["before"], counts


def test_check_is_a_noop_without_a_scope_and_raises_past_it(clock):
    land_deadline.check("anywhere")
    with land_deadline.scope(5.0):
        land_deadline.check("inside")
        clock.now += 6.0
        assert land_deadline.expired()
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            land_deadline.check("the.leg")
    assert raised.value.leg == "the.leg"
    assert not land_deadline.expired()


def test_a_fail_open_except_exception_cannot_swallow_the_deadline(clock):
    with land_deadline.scope(1.0):
        clock.now += 2.0
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            try:
                land_deadline.check("leg")
            except Exception:
                pass


def test_an_inner_scope_never_extends_the_outer_deadline(clock):
    with land_deadline.scope(5.0):
        with land_deadline.scope(500.0):
            clock.now += 6.0
            assert land_deadline.expired()


def test_claim_release_past_the_deadline_is_skipped_and_named(clock, monkeypatch, tmp_path):
    released = []
    monkeypatch.setattr(
        commit_v2.session_scope, "release_committed_claims_or_retain", lambda *a, **k: released.append(a)
    )
    with land_deadline.scope(1.0):
        assert commit_v2._release_committed_claims_step(tmp_path, ["a.py"]) is None
        assert len(released) == 1
        clock.now += 2.0
        note = commit_v2._release_committed_claims_step(tmp_path, ["a.py"])
    assert note is not None and "_release_committed_claims_step" in note
    assert "deferred to the next op" in note
    assert len(released) == 1


def test_expired_release_records_paths_and_sha_and_a_record_failure_is_swallowed(clock, monkeypatch, tmp_path):
    from coordinator_core.session import deferred_release

    recorded = []
    monkeypatch.setattr(commit_v2.session_core, "resolve_session_id", lambda _cwd: "sid-1")
    monkeypatch.setattr(deferred_release, "record", lambda *a: recorded.append(a) or True)
    monkeypatch.setattr(
        commit_v2.session_scope, "release_committed_claims_or_retain", lambda *a, **k: recorded.append("inline")
    )
    with land_deadline.scope(1.0):
        assert commit_v2._release_committed_claims_step(tmp_path, ["a.py"], "abc123") is None
        assert recorded == ["inline"]
        clock.now += 2.0
        recorded.clear()
        note = commit_v2._release_committed_claims_step(tmp_path, ["a.py", "b.py"], "abc123")
        assert recorded == [("sid-1", str(tmp_path), "abc123", ["a.py", "b.py"])]

        def boom(*a):
            raise RuntimeError("disk")

        monkeypatch.setattr(deferred_release, "record", boom)
        assert "deferred to the next op" in commit_v2._release_committed_claims_step(tmp_path, ["a.py"], "abc123")
    assert note is not None
