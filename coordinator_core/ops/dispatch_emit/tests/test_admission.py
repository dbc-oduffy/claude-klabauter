"""
coordinator_core.ops.dispatch_emit.tests.test_admission

Covers admission.py's decision surface (`decide`, `await_admission`,
`should_refuse_warm`) with a fully-injected reader/sleep/monotonic/rng so no
test depends on the real box's load. Config resolution is exercised
separately in test_admission_config.py.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import admission


@pytest.fixture(autouse=True)
def _admission_live(monkeypatch):
    """conftest.py sets DISABLE_ENV=1 at import (AC9) so ordinary test runs
    never hit the real host sampler; this module's tests exercise admission
    itself and need it live unless a test sets it explicitly (e.g. the
    disabled-path tests below)."""
    monkeypatch.delenv(admission.DISABLE_ENV, raising=False)


def _thresholds(**overrides):
    values = {
        "cpu_load_max": 0.9,
        "mem_avail_pct_min": 10.0,
        "max_hold_s": 90.0,
        "recheck_s": 10.0,
    }
    values.update(overrides)
    return admission.AdmissionThresholds(
        cpu_load_max=values["cpu_load_max"],
        mem_avail_pct_min=values["mem_avail_pct_min"],
        max_hold_s=values["max_hold_s"],
        recheck_s=values["recheck_s"],
        source={k: "machine-local" for k in admission.CONFIG_KEYS},
    )


def _reading(cpu_load=0.1, mem_avail_mb=8000, mem_total_mb=16000, platform="linux"):
    return {
        "cpu_load": cpu_load,
        "mem_avail_mb": mem_avail_mb,
        "mem_total_mb": mem_total_mb,
        "platform": platform,
        "read_cost_ms": 1.0,
    }


class _FakeClock:
    """Fake sleep/monotonic pair: sleep(s) advances the clock by s; monotonic()
    reads the accumulated time. Mirrors the plan's "fake monotonic advanced by
    the fake sleep" test contract."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


def _fixed_rng(value):
    return lambda: value


def _fixed_load_thresholds(monkeypatch, thresholds):
    monkeypatch.setattr(
        admission,
        "_load_thresholds_with_missing",
        lambda start_dir: (thresholds, []),
    )


def _unconfigured(monkeypatch, missing_keys):
    monkeypatch.setattr(
        admission,
        "_load_thresholds_with_missing",
        lambda start_dir: (None, missing_keys),
    )


# --- decide() ----------------------------------------------------------------


def test_decide_under_threshold_returns_no_reasons():
    t = _thresholds()
    assert admission.decide(_reading(cpu_load=0.1), t) == []


def test_decide_reasons_name_metric_reading_threshold():
    t = _thresholds(cpu_load_max=0.9, mem_avail_pct_min=10.0)
    reading = _reading(cpu_load=2.0, mem_avail_mb=500, mem_total_mb=16000)
    reasons = admission.decide(reading, t)
    assert any("cpu_load" in r and "2.0" in r and "cpu_load_max" in r and "0.9" in r for r in reasons)
    assert any("mem_avail_pct" in r and "mem_avail_pct_min" in r and "10.0" in r for r in reasons)


def test_decide_one_threshold_trips_on_both_posix_and_windows_shaped_reading():
    """One cpu_load_max value holds for both OS shapes -- AC8."""
    t = _thresholds(cpu_load_max=0.9)
    posix_reading = _reading(cpu_load=2.0)
    windows_reading = _reading(cpu_load=0.95)
    assert admission.decide(posix_reading, t) != []
    assert admission.decide(windows_reading, t) != []


# --- await_admission: idle / disabled / unconfigured / unreadable ------------


def test_await_admission_idle_admits_with_zero_sleeps(monkeypatch):
    t = _thresholds()
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    record = admission.await_admission(
        Path("/tmp"),
        hold_allowed=True,
        read_load=lambda: _reading(cpu_load=0.1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=_fixed_rng(0.0),
    )
    assert record["verdict"] == "admitted"
    assert record["waited_s"] == 0.0
    assert record["rechecks"] == 0
    assert clock.slept == []


def test_await_admission_disabled_never_reads(monkeypatch):
    monkeypatch.setenv(admission.DISABLE_ENV, "1")

    def _boom():
        raise AssertionError("reader must not be called when disabled")

    record = admission.await_admission(Path("/tmp"), hold_allowed=True, read_load=_boom)
    assert record["verdict"] == "disabled"


def test_await_admission_unconfigured_never_calls_reader(monkeypatch):
    _unconfigured(monkeypatch, ["cpu_load_max", "recheck_s"])

    def _boom():
        raise AssertionError("reader must not be called when unconfigured")

    record = admission.await_admission(Path("/tmp"), hold_allowed=True, read_load=_boom)
    assert record["verdict"] == "admitted_unconfigured"
    assert set(record["missing_keys"]) == {"cpu_load_max", "recheck_s"}
    assert record["thresholds"] is None


def test_await_admission_unreadable_never_holds(monkeypatch):
    t = _thresholds()
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    record = admission.await_admission(
        Path("/tmp"),
        hold_allowed=True,
        read_load=lambda: _reading(cpu_load=None, mem_avail_mb=None, mem_total_mb=None),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=_fixed_rng(0.0),
    )
    assert record["verdict"] == "admitted_unreadable"
    assert record["waited_s"] == 0.0
    assert clock.slept == []


# --- await_admission: hold loop ----------------------------------------------


def test_await_admission_loaded_then_idle_holds_and_admits(monkeypatch):
    t = _thresholds(cpu_load_max=0.9, recheck_s=10.0, max_hold_s=90.0)
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    readings = [
        _reading(cpu_load=2.0),
        _reading(cpu_load=2.0),
        _reading(cpu_load=0.1),
    ]

    def _reader():
        return readings.pop(0)

    record = admission.await_admission(
        Path("/tmp"),
        hold_allowed=True,
        read_load=_reader,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=_fixed_rng(0.5),
    )
    assert record["verdict"] == "admitted_after_hold"
    assert record["waited_s"] == pytest.approx(sum(clock.slept))
    assert record["rechecks"] == 2
    assert readings == []


def test_await_admission_always_loaded_admits_at_max_hold(monkeypatch):
    t = _thresholds(cpu_load_max=0.9, recheck_s=10.0, max_hold_s=25.0)
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    record = admission.await_admission(
        Path("/tmp"),
        hold_allowed=True,
        read_load=lambda: _reading(cpu_load=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=_fixed_rng(1.0),
    )
    assert record["verdict"] == "admitted_at_max_hold"
    assert record["waited_s"] == pytest.approx(25.0)
    assert clock.slept[-1] <= 10.0 * 1.25 or clock.slept[-1] == pytest.approx(
        25.0 - sum(clock.slept[:-1])
    )


def test_await_admission_hold_allowed_false_never_sleeps_while_loaded(monkeypatch):
    t = _thresholds(cpu_load_max=0.9)
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    record = admission.await_admission(
        Path("/tmp"),
        hold_allowed=False,
        read_load=lambda: _reading(cpu_load=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        rng=_fixed_rng(0.0),
    )
    assert record["verdict"] == "admitted_warm_no_hold"
    assert record["waited_s"] == 0.0
    assert clock.slept == []


def test_hold_sleep_interval_within_jitter_band(monkeypatch):
    t = _thresholds(cpu_load_max=0.9, recheck_s=10.0, max_hold_s=90.0)
    _fixed_load_thresholds(monkeypatch, t)
    clock = _FakeClock()
    readings = [_reading(cpu_load=2.0), _reading(cpu_load=0.1)]

    def _reader():
        return readings.pop(0)

    for rng_val in (0.0, 1.0):
        clock2 = _FakeClock()
        readings2 = [_reading(cpu_load=2.0), _reading(cpu_load=0.1)]

        def _reader2():
            return readings2.pop(0)

        admission.await_admission(
            Path("/tmp"),
            hold_allowed=True,
            read_load=_reader2,
            sleep=clock2.sleep,
            monotonic=clock2.monotonic,
            rng=_fixed_rng(rng_val),
        )
        assert 0.75 * 10.0 - 1e-9 <= clock2.slept[0] <= 1.25 * 10.0 + 1e-9


# --- should_refuse_warm -------------------------------------------------------


def test_should_refuse_warm_none_when_idle(monkeypatch):
    t = _thresholds()
    _fixed_load_thresholds(monkeypatch, t)
    result = admission.should_refuse_warm(Path("/tmp"), read_load=lambda: _reading(cpu_load=0.1))
    assert result is None


def test_should_refuse_warm_none_when_unconfigured(monkeypatch):
    _unconfigured(monkeypatch, ["max_hold_s"])
    result = admission.should_refuse_warm(
        Path("/tmp"), read_load=lambda: (_ for _ in ()).throw(AssertionError("must not read"))
    )
    assert result is None


def test_should_refuse_warm_record_when_loaded(monkeypatch):
    t = _thresholds(cpu_load_max=0.9)
    _fixed_load_thresholds(monkeypatch, t)
    result = admission.should_refuse_warm(Path("/tmp"), read_load=lambda: _reading(cpu_load=2.0))
    assert result is not None
    assert result["reasons"]


def test_should_refuse_warm_disabled_returns_none(monkeypatch):
    monkeypatch.setenv(admission.DISABLE_ENV, "1")
    result = admission.should_refuse_warm(
        Path("/tmp"), read_load=lambda: (_ for _ in ()).throw(AssertionError("must not read"))
    )
    assert result is None


# --- AC3: no numeric literal bound to a CONFIG_KEYS name; exactly one sleep --


def test_no_numeric_literal_bound_to_a_config_key_name():
    source = inspect.getsource(admission)
    tree = ast.parse(source)
    key_names = set(admission.CONFIG_KEYS)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.violations = []

        def _is_numeric(self, node):
            return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
                node.value, bool
            )

        def visit_FunctionDef(self, node):
            for arg in node.args.args + node.args.kwonlyargs:
                pass
            defaults = list(node.args.defaults) + list(node.args.kw_defaults)
            arg_names = (
                [a.arg for a in node.args.args[-len(node.args.defaults):]]
                if node.args.defaults
                else []
            )
            arg_names += [a.arg for a in node.args.kwonlyargs]
            for name, default in zip(arg_names, defaults):
                if default is not None and name in key_names and self._is_numeric(default):
                    self.violations.append((name, node.lineno))
            self.generic_visit(node)

        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in key_names and self._is_numeric(node.value):
                    self.violations.append((target.id, node.lineno))
            self.generic_visit(node)

        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2
            ):
                first = node.args[0]
                key_literal = None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    key_literal = first.value
                if key_literal in key_names and self._is_numeric(node.args[1]):
                    self.violations.append((key_literal, node.lineno))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    assert visitor.violations == [], f"numeric default bound to a CONFIG_KEYS name: {visitor.violations}"


def test_exactly_one_sleep_call_site():
    source = inspect.getsource(admission)
    tree = ast.parse(source)
    sleep_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sleep"
    ]
    assert len(sleep_calls) == 1, f"expected exactly one sleep(...) call site, found {len(sleep_calls)}"
