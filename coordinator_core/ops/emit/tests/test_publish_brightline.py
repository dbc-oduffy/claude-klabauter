"""
Brightline pin for the publish path's document-assembly step (C8).

Purpose: DR-344 constraint 1 (500ms end-to-end, process time, no wall-clock re-entry
under a different name) is a STANDING bar, not a one-time authoring-time measurement.
`publish_envelope.splice_publish_envelope` is the document-assembly work on the publish
path -- the byte-splice that avoids the ~281ms parse+re-serialise this module's own
docstring documents as the rejected design (see `publish_envelope.py` module docstring).
This test pins that the byte-splice design stays cheap as the codebase evolves, against a
realistically large (multi-ten-MB) synthetic fixture -- not a toy few-KB body, since the
whole risk this guards against (an accidental `json.loads`/`json.dumps` re-introduction)
is size-dependent and invisible on a small fixture.

Two measured axes (DR-344 § 5): process time (`time.process_time()`, amortised over
K>=20 in-process iterations to defeat Windows's ~15.6ms scheduler-tick quantisation --
see `coordinator_core/benchmarks/process_time.py` module docstring, trap 2) and peak
allocation (via `tracemalloc`, whose baseline is explicit and reset per-block, so the
figure is a property of the splice rather than of whatever the interpreter happened to
have allocated before this test ran).

Never wall clock: DR-344's negative spec forbids a wall-clock budget re-entering under a
different name, and a wall-clock assertion here would measure peer session load (this
machine's design condition is 50-70 concurrent sessions), not this code's cost -- exactly
what made every prior brightline breach arguable. `time.process_time()` only.

Does NOT use `benchmarks.process_time.batched_process_time_ms`: that primitive spawns its
K samples as SUBPROCESSES (a job-object primitive for trampoline/end-to-end invocation
cost), which would (a) put 20 process spawns in a test that trips
`test_no_unbatched_per_item_git_spawn.py`'s amplification gate, forcing markers that push
this test off the fast tier, and (b) raise `NotImplementedError` off Windows. In-process
K-batching (a plain Python loop around `time.process_time()`) gets the same quantisation
defeat at none of those costs, because the work under test never spawns a process.

Does NOT put the network POST (`publish_transport.publish_document`) inside the measured
window -- transport duration is not our process time and not ours to bound; the brief is
explicit that asserting on it would import exactly the load-dependence this test exists
to exclude. Only the assembly step (`splice_publish_envelope`) is timed.

Negative-spec: this test does not read `state/cockpit-emission.json` or any other
on-disk artifact -- pointing at the real artifact would make the test environment-
dependent (absent/small/wrong-shape in a clean clone) and red for reasons unrelated to
the code under test. The fixture is synthesised in `_synthetic_emission_body`.
"""

from __future__ import annotations

import json
import time
import tracemalloc

import pytest

from coordinator_core.ops.emit import publish_envelope as pe

SCHEMA_VERSION = "3.13.0"

# Iteration count for the amortised process-time measurement. >=20 per the brief --
# the assembly path's own measured cost is ~31ms (~2 scheduler ticks), so a single
# sample can read 15.6ms or 46.9ms for identical work; dividing a K>=20 batch by K
# recovers sub-tick resolution honestly. See `process_time.py` module docstring, trap 2.
_ITERATIONS = 25

# Number of representative section records repeated into the synthetic fixture. Tuned
# to land the serialised fixture in the multi-ten-MB range the brief calls for (the
# real risk -- an accidental full parse/re-serialise -- is size-dependent and invisible
# on a small fixture).
_SECTION_RECORD_COUNT = 40_000

# Widened process-time ceiling for the amortised per-iteration figure. The design's own
# docstring measures ~31ms for the byte-splice against ~281ms for the rejected
# parse+re-serialise alternative on a real-sized artifact; this bound sits well below the
# rejected design's cost and well above the current design's, so it catches a REGRESSION
# back toward a parsing implementation without flaking on ordinary jitter. Generous
# headroom under DR-344's 500ms end-to-end brightline, since this is only the assembly
# slice of the full publish path (assembly + transport), not the whole budget.
_PROCESS_TIME_CEILING_MS = 150.0

# Peak-allocation ceiling across the K-iteration loop, as a multiple of one fixture's
# serialised byte size. The design allocates ONE output buffer via `memoryview` slices +
# `bytes.join`, with no intermediate full-size copies, and measures at 1.0x under
# tracemalloc. The ceiling is 2.0x, not a looser figure, because the regression this
# actually has to catch is not hypothetical: the first implementation of this splice
# made four full-size copies (`raw.lstrip()` for a length, `raw.strip()`, an `inner`
# slice built only to be tested for truthiness, then the concatenation) and measured
# 3.0x. A 4x ceiling would have let that ship. Set the bar under the defect that
# happened, not under the one imagined.
_PEAK_MEMORY_CEILING_MULTIPLE = 2.0


def _synthetic_emission_body(record_count: int) -> bytes:
    """Build a structurally-representative frozen-emission-v2 body at realistic scale.

    `schema_version` and `emitted_at` open the object (matching the real writer's
    `envelope.py` dict-literal ordering that `_bounded_head_scan_key` depends on), then a
    `sections` list repeats a representative record shape `record_count` times to reach a
    multi-ten-MB byte count -- synthesised, never read off disk (see module docstring's
    negative-spec).
    """
    record = {
        "id": "handoff-2026-08-21-103635-reaching-the-warm-engine",
        "kind": "handoff",
        "repo": "claude-klabauter",
        "session_id": "4cf4dac9-8899-43d0-85c5-9ecc288863a5",
        "title": "Reaching the warm engine",
        "body": "A representative section body of moderate length, repeated across "
        "many records to approximate a realistically large frozen-emission-v2 "
        "artifact without reading the real one off disk. " * 4,
        "tags": ["brightline", "publish", "assembly"],
        "created_at": "2026-08-21T10:36:35Z",
        "provenance": {"path": "state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md"},
    }
    doc = {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": "2026-08-21T00:00:00Z",
        "sections": [record for _ in range(record_count)],
    }
    return json.dumps(doc, indent=2).encode("utf-8")


@pytest.fixture(scope="module")
def _fixture_bytes() -> bytes:
    return _synthetic_emission_body(_SECTION_RECORD_COUNT)


@pytest.fixture(autouse=True)
def _pin_schema_version(monkeypatch):
    monkeypatch.setattr(pe.validate, "read_schema_version", lambda: SCHEMA_VERSION)


@pytest.fixture(autouse=True)
def _pin_stamp(monkeypatch):
    monkeypatch.setattr(pe, "read_engine_stamp_sha", lambda engine_root: "abc1234")


def test_fixture_is_realistically_sized(_fixture_bytes):
    # Sanity guard on the fixture itself -- the whole test is void if this drifts down
    # to a toy size (the brief: "not a toy one, since the whole risk is size-dependent").
    assert len(_fixture_bytes) >= 10_000_000, (
        f"synthetic fixture is only {len(_fixture_bytes)} bytes -- too small to be "
        "representative of the multi-ten-MB artifacts this path serves in production"
    )


def test_splice_stays_within_process_time_brightline(_fixture_bytes):
    """Amortised process-time bound: K>=20 in-process iterations, never wall-clock."""
    t0 = time.process_time()
    for _ in range(_ITERATIONS):
        out = pe.splice_publish_envelope(_fixture_bytes, owner="owner", repo="myrepo")
        assert out  # keep the call live, not optimised away
    elapsed_ms = (time.process_time() - t0) * 1000.0
    per_iteration_ms = elapsed_ms / _ITERATIONS

    assert per_iteration_ms <= _PROCESS_TIME_CEILING_MS, (
        f"splice_publish_envelope amortised process time regressed: "
        f"{per_iteration_ms:.2f}ms/iteration over {_ITERATIONS} iterations "
        f"(total {elapsed_ms:.2f}ms), exceeding the {_PROCESS_TIME_CEILING_MS}ms "
        f"ceiling. DR-344's brightline is 500ms end-to-end for the whole publish path; "
        f"this ceiling is a tighter regression guard on just the assembly slice."
    )


def test_splice_does_not_double_buffer_peak_memory(_fixture_bytes):
    """Peak-allocation bound: no double-buffer at the splice/concatenation step.

    Measured with `tracemalloc`, which reports the peak of THIS block's own allocations
    against a baseline reset here.

    Not `psutil`'s `peak_wset`: that is a monotonic process-lifetime high-water mark, so
    a before/after delta around this loop measures only what exceeds the highest point
    the interpreter has already reached -- including the fixture build and the K splices
    the process-time test above already performed. Measured against the real regression
    this test exists to catch (the pre-fix 3.0x-of-artifact splice), that framing cleared
    a 2x ceiling by under 2% -- it passed, but on a margin set by test ordering rather
    than by the code under test, and it would flip to a false green under a reorder or a
    smaller fixture. tracemalloc's baseline is explicit and per-block, so the number here
    is a property of the splice and nothing else.
    """
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline = tracemalloc.get_traced_memory()[0]

        for _ in range(_ITERATIONS):
            out = pe.splice_publish_envelope(_fixture_bytes, owner="owner", repo="myrepo")
            assert out
            del out

        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    growth = peak - baseline
    ceiling = _PEAK_MEMORY_CEILING_MULTIPLE * len(_fixture_bytes)

    assert growth <= ceiling, (
        f"splice_publish_envelope peak allocation over {_ITERATIONS} iterations "
        f"was {growth} bytes, exceeding {_PEAK_MEMORY_CEILING_MULTIPLE}x one fixture's "
        f"size ({ceiling:.0f} bytes for a {len(_fixture_bytes)}-byte fixture) -- consistent "
        f"with a double-buffering (e.g. parse+re-serialise) regression at the splice step."
    )


def test_splice_never_parses_the_full_emission_body(monkeypatch, _fixture_bytes):
    """No-parse property, asserted directly: `json.loads` is only ever called on the
    small bounded head-scan window, never on anything approaching the full body.

    A future edit that "just parses it quickly to validate" would regress the ~31ms
    design back toward the rejected ~281ms one invisibly (the output stays correct);
    this pins the mechanism, not just the number.
    """
    real_loads = pe.json.loads
    call_arg_lengths = []

    def _tracking_loads(s, *args, **kwargs):
        length = len(s) if hasattr(s, "__len__") else len(str(s))
        call_arg_lengths.append(length)
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(pe.json, "loads", _tracking_loads)

    out = pe.splice_publish_envelope(_fixture_bytes, owner="owner", repo="myrepo")
    assert out

    assert call_arg_lengths, "expected at least the head-scan json.loads calls"
    # The bounded head-scan window (`_HEAD_SCAN_BOUND`) caps how large any legitimate
    # call can be; the full fixture is orders of magnitude larger, so this distinguishes
    # "parsed the tiny schema_version/emitted_at value literal" from "parsed the body".
    for length in call_arg_lengths:
        assert length < pe._HEAD_SCAN_BOUND, (
            f"json.loads was called with a {length}-byte argument -- exceeds the "
            f"{pe._HEAD_SCAN_BOUND}-byte bounded head-scan window, consistent with a "
            f"full-body parse regression (fixture is {len(_fixture_bytes)} bytes)"
        )
