"""Unit + cadence tests for coordinator_core.benchmarks.interleave.

Mocked tests (default tier) cover `run_interleaved`'s API-shape guarantees --
the single-primitive refusal, the round-robin shuffle discipline, and stats
reduction -- without spawning anything. Cadence-tiered tests exercise the
real `default_baseline_primitives()` against this box's actual git/python/
stdlib-walk primitives.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C1.
"""

from __future__ import annotations

import random
from unittest import mock

import pytest

from coordinator_core.benchmarks.interleave import (
    Primitive,
    default_baseline_primitives,
    run_interleaved,
)


def _const_primitive(name: str, value: float) -> Primitive:
    return Primitive(name=name, invoke=lambda: value)


def _counting_primitive(name: str, values: list) -> Primitive:
    it = iter(values)
    return Primitive(name=name, invoke=lambda: next(it))


def test_run_interleaved_refuses_single_primitive():
    with pytest.raises(ValueError, match="need >=2 primitives"):
        run_interleaved([_const_primitive("only_one", 1.0)], n=5)


def test_run_interleaved_refuses_empty_primitives():
    with pytest.raises(ValueError, match="need >=2 primitives"):
        run_interleaved([], n=5)


def test_run_interleaved_refuses_n_less_than_one():
    primitives = [_const_primitive("a", 1.0), _const_primitive("b", 2.0)]
    with pytest.raises(ValueError, match="n must be >= 1"):
        run_interleaved(primitives, n=0)


def test_run_interleaved_refuses_duplicate_names():
    primitives = [_const_primitive("dup", 1.0), _const_primitive("dup", 2.0)]
    with pytest.raises(ValueError, match="duplicate primitive names"):
        run_interleaved(primitives, n=3)


def test_run_interleaved_collects_n_samples_per_primitive():
    primitives = [_const_primitive("a", 10.0), _const_primitive("b", 20.0)]
    stats = run_interleaved(primitives, n=7, rng=random.Random(0))

    assert stats["a"].sample_count == 7
    assert stats["b"].sample_count == 7
    assert stats["a"].median_ms == 10.0
    assert stats["b"].median_ms == 20.0
    assert stats["a"].p90_ms == 10.0
    assert stats["b"].p90_ms == 20.0


def test_run_interleaved_reports_median_and_p90_distinctly():
    # Adversarial spread so median and p90 cannot coincidentally collapse.
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    primitive_a = _counting_primitive("a", list(values))
    primitive_b = _const_primitive("b", 0.0)

    stats = run_interleaved([primitive_a, primitive_b], n=5, rng=random.Random(1))

    assert stats["a"].sample_count == 5
    assert stats["a"].median_ms == 3.0
    assert stats["a"].p90_ms > stats["a"].median_ms
    assert stats["a"].p90_ms <= 100.0


def test_run_interleaved_draw_order_is_not_grouped_by_primitive():
    """Regression guard for the interleaving contract itself: with a fixed
    seed, the underlying draw order must not be a full block of one
    primitive followed by a full block of the other -- that would be
    exactly the block-sampled shape this module exists to make
    inexpressible at the single-primitive level, and to actively avoid at
    the multi-primitive level."""
    call_log = []

    def _logging_invoke(name):
        def _inner():
            call_log.append(name)
            return 1.0

        return _inner

    primitives = [
        Primitive(name="a", invoke=_logging_invoke("a")),
        Primitive(name="b", invoke=_logging_invoke("b")),
    ]
    run_interleaved(primitives, n=10, rng=random.Random(42))

    assert len(call_log) == 20
    # A fully block-sampled order would be 10 of one name followed by 10 of
    # the other, i.e. exactly one transition. Interleaved round-robin with
    # independent per-round shuffles produces many more.
    transitions = sum(1 for i in range(1, len(call_log)) if call_log[i] != call_log[i - 1])
    assert transitions > 1, f"draw order looks block-sampled: {call_log!r}"


# Spawns real child processes (python, git) for the baseline primitives;
# runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_default_baseline_primitives_are_interleavable_on_this_box():
    primitives = default_baseline_primitives()
    names = {p.name for p in primitives}
    assert names == {
        "bare_cpython_start",
        "forwarder_dispatcher_roundtrip",
        "git_rev_parse_show_toplevel",
        "stdlib_parent_walk",
    }

    stats = run_interleaved(primitives, n=3, rng=random.Random(7))

    for name in names:
        assert stats[name].sample_count == 3
        assert stats[name].median_ms >= 0.0
        assert stats[name].p90_ms >= stats[name].median_ms


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_default_baseline_primitives_respects_explicit_repo_root(tmp_path):
    primitives = default_baseline_primitives(repo_root=str(tmp_path.parent))
    names = {p.name for p in primitives}
    assert "git_rev_parse_show_toplevel" in names
