"""Tests for `coordinator_core.orientation.budget_breach_signal` -- the
"wire the existing breach instrument to a bell" fix for `ops/op_budget_breaches.py`
(correctly wired, fully tested, and called by nothing outside its own test file,
while `ceremony.commit` timed out on 28 of 31 calls for 28 hours unnoticed).

Same silent-failure class as `test_warm_health_signal.py`'s BLINDFOLD #2, one
instrument over."""

from __future__ import annotations

import pytest

from coordinator_core.orientation import budget_breach_signal as bbs


def _summary(*ops, breaching=None, stolen_ms=1000.0, attempts=100):
    """A `breach_report`-shaped dict. Only the fields the emitter reads."""
    rows = [
        {
            "op": name,
            "attempts": n,
            "breaches": int(round(n * rate)),
            "breach_rate": rate,
            "stolen_ms": 5000.0,
            "trend": "window_limited",
        }
        for name, n, rate in ops
    ]
    return {
        "bar_ms": 500.0,
        "ops": rows,
        "totals": {
            "attempts": attempts,
            "breaching_ops": len(rows) if breaching is None else breaching,
            "stolen_ms": stolen_ms,
            "vanished": 0,
            "in_flight": 0,
        },
    }


@pytest.fixture
def report(monkeypatch):
    """Patch the report the emitter reads; returns a setter."""
    def _set(summary):
        monkeypatch.setattr(
            "coordinator_core.ops.op_budget_breaches.breach_report",
            lambda **kw: summary,
        )
    return _set


def test_healthy_box_renders_nothing(report):
    """The module's whole posture: no op over the bar means no section, every
    session, forever. A standing line would train the eye to skip it."""
    report(_summary(breaching=0))
    assert bbs.emit_budget_breaches(bbs.Path(".")) == ""


def test_thin_evidence_is_not_a_signal(report):
    """A cold clone's first dispatches land over the bar for reasons that say
    nothing about the op -- below MIN_ATTEMPTS, even a 100% rate stays silent.
    This is the `memo.send` 6/6 case observed live alongside the real ones."""
    report(_summary(("memo.send", bbs.MIN_ATTEMPTS - 1, 1.0)))
    assert bbs.emit_budget_breaches(bbs.Path(".")) == ""


def test_occasional_breach_below_rate_stays_silent(report):
    """Plenty of attempts but an occasional breach is not yet a boot message."""
    report(_summary(("ops.thing", 500, bbs.BREACH_RATE / 2)))
    assert bbs.emit_budget_breaches(bbs.Path(".")) == ""


def test_the_live_ceremony_commit_case_renders(report):
    """The case this module was built from, at its measured shape: 28 of 31
    calls over the bar. If this ever stops rendering, the 28-hour blindfold is
    back."""
    report(_summary(("ceremony.commit", 31, 28 / 31)))
    line = bbs.emit_budget_breaches(bbs.Path("."))
    assert line.startswith("- ⚠ ")
    assert "500ms bar" in line


def test_one_qualifying_op_is_enough_among_thin_ones(report):
    """A real breach must not be masked by thin-evidence rows ranked above it."""
    report(_summary(
        ("memo.send", 2, 1.0),
        ("ceremony.commit", 31, 0.97),
    ))
    assert bbs.emit_budget_breaches(bbs.Path(".")) != ""


def test_never_suggests_waiting(report):
    """`headline_for`'s register rule, asserted at this surface too: an op is
    not made correct by the caller waiting longer, and the boot line must never
    teach that habit."""
    report(_summary(("ceremony.commit", 31, 0.97)))
    line = bbs.emit_budget_breaches(bbs.Path(".")).lower()
    for banned in ("timeout", "wait", "retry", "be patient"):
        assert banned not in line, f"breach line must not suggest {banned!r}: {line}"


def test_unreadable_sink_is_omitted_not_raised(monkeypatch):
    """Fail-open: orientation regen must never break on a telemetry problem."""
    def _boom(**kw):
        raise OSError("sink is gone")
    monkeypatch.setattr(
        "coordinator_core.ops.op_budget_breaches.breach_report", _boom
    )
    assert bbs.emit_budget_breaches(bbs.Path(".")) == ""


def test_section_is_protected_from_the_byte_budget():
    """Elastic would reproduce the bug on a timer: the byte budget bites when
    the cache is crowded, which is the same condition under which ops breach.
    A line trimmed exactly on the sessions where it is truest is never read."""
    from coordinator_core.orientation import regenerate_cache as rc

    assert "Budget breaches" in rc._CACHE_PROTECTED_SECTIONS
    assert "Budget breaches" not in rc._CACHE_ELASTIC_SECTIONS


def test_regen_calls_the_emitter():
    """The whole point. `op_budget_breaches` was already correct and already
    tested; what was missing was a caller. Assert one exists."""
    import inspect

    from coordinator_core.orientation import regenerate_cache as rc

    assert hasattr(rc, "emit_budget_breaches")
    assert "emit_budget_breaches(" in inspect.getsource(rc.build_cache)
