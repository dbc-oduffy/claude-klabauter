"""Tests for the advisory-fire counter's write-guard leg (C21,
docs/plans/2026-08-06-apply-guard-class-census.md) and for the C6/C7
opt-in aggregation contract
(docs/plans/2026-08-06-windows-hot-path-less-work-per-interpreter.md).

Exercises `engine.evaluate`'s advisory-phase `return out` seam end-to-end,
using a fake `_Guard` (via `engine._discover_guards` monkeypatched) so the
firing is deterministic rather than depending on a real guard's trigger
shape. Covers AC-C21 in both directions:

  (a) a flipped guard firing produces exactly one appended
      `{"guard", "at"}` record in
      `state/subagent-share/<session_id>/advisory-fire-counts.jsonl`.
  (b) an induced write failure (an unwritable per-session directory) leaves
      the guard's own returned envelope unchanged.

C7 (this plan's chunk) extends the same fake-guard harness to pin the
`aggregate=True` opt-in C6 added, chosen as the natural home over a new
parallel file because it already owns the `_discover_guards` monkeypatch +
`_record_advisory_fire`/counts-file assertion machinery every one of these
new tests needs:

  (a) `TestAggregateReturnsAllFiringAdvisoriesInPriorityOrder` — the
      silent-drop regression this whole plan exists to prevent (AC6).
      Demonstrated to genuinely fail against the pre-C6 `evaluate()`
      (commit 71065167f, the parent of C6's landing commit f0994eabc) by
      temporarily swapping `coordinator_core/write_guards/engine.py` for
      the pre-C6 copy at that commit and re-running an equivalent probe: it
      raised `TypeError: evaluate() got an unexpected keyword argument
      'aggregate'` — the legacy signature has no aggregation opt-in at all,
      so there is no way to ask it for more than one advisory. That
      temporary swap was local-only (git-diff-clean afterwards) and is not
      part of this diff.

      This "would fail against pre-fix code" guarantee is PROSE-ONLY —
      nothing in CI re-runs that swap-and-probe. The claim rests entirely on
      `aggregate` being an unrecognized kwarg on the pre-C6 signature (any
      call passing it there TypeErrors, unconditionally); it is not
      independently verified by this test module on every run. Treat the
      manual verification above as a one-time provenance note, not an
      automated regression guarantee.
  (b) `TestLegacyCallerUnchanged` — omitting `aggregate` still returns
      exactly one dict, unchanged (AC6).
  (c) `TestHardDenyShortCircuitsAggregate` — a hard-deny still short-
      circuits before any advisory runs, in both shapes, with no advisory
      text reaching the deny envelope (AC7).
  (d) `TestRecordAdvisoryFireCountMatchesReturnedAdvisories` — under
      `aggregate=True`, `_record_advisory_fire` runs once per RETURNED
      advisory, not once per `evaluate()` call (AC8) — and, per the C6
      docstring's AC8/C21 cross-plan note, a single `evaluate()` call can
      now legitimately append MORE THAN ONE record to
      `advisory-fire-counts.jsonl`, an arity change in the counter's
      calling convention (not a behavioural change in what counts as a
      firing) that a reader of the counts file must not mistake for a step
      change in guard behaviour.
  (e) `TestDiscoveryStableAcrossRepeatedCalls` — `_discover_guards()`
      (still uncached, C5 `wont_do`) returns an identical guard set and
      order on repeated calls within one process, and `skipped_out` is
      populated correctly on the SECOND and later calls, not only the
      first (AC5).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from coordinator_core.write_guards import engine

_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "permissionDecisionReason": "advisory note",
    }
}


def _payload(session_id="sess-c21", cwd=None):
    p = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/repo/some_file.py"},
        "session_id": session_id,
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


def _fake_guard(name="fake-advisory-guard"):
    return engine._Guard(name, "advisory", ["Write"], 100, lambda payload: dict(_ENVELOPE))


def _counts_path(git_root, session_id):
    return git_root / "state" / "subagent-share" / session_id / "advisory-fire-counts.jsonl"


class TestFlippedGuardProducesOneRecord:
    def test_one_record_appended(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_guard()], []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="sess-c21", cwd=str(tmp_path)))

        assert out == _ENVELOPE
        path = _counts_path(tmp_path, "sess-c21")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record.keys()) == {"guard", "at"}
        assert record["guard"] == "fake-advisory-guard"

    def test_no_record_when_session_id_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_guard()], []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="", cwd=str(tmp_path)))

        assert out == _ENVELOPE
        assert not (tmp_path / "state" / "subagent-share").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
class TestWriteFailureLeavesEnvelopeUnchanged:
    def test_unwritable_session_dir_does_not_alter_returned_envelope(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "state" / "subagent-share" / "sess-c21"
        session_dir.mkdir(parents=True)
        original_mode = session_dir.stat().st_mode
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_guard()], []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )
        try:
            os.chmod(session_dir, 0o000)
            out = engine.evaluate(_payload(session_id="sess-c21", cwd=str(tmp_path)))
            # The guard's own envelope must be returned unchanged -- the
            # write failure inside the counter's own try/except must never
            # turn an advisory into a deny or raise into the caller.
            assert out == _ENVELOPE
        finally:
            os.chmod(session_dir, original_mode)


def _fake_advisory(name, priority, *, calls=None):
    """A fake advisory `_Guard`; if `calls` is given, appends `name` to it
    on every invocation so a test can assert how many times (and in what
    order) each guard's `check()` actually ran."""

    def _check(payload):
        if calls is not None:
            calls.append(name)
        return {"hookSpecificOutput": {"additionalContext": "advisory:%s" % name}}

    return engine._Guard(name, "advisory", ["Write"], priority, _check)


def _fake_hard_deny(name, priority, *, calls=None):
    def _check(payload):
        if calls is not None:
            calls.append(name)
        return {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "hard-deny:%s" % name,
            }
        }

    return engine._Guard(name, "hard-deny", ["Write"], priority, _check)


class TestAggregateReturnsAllFiringAdvisoriesInPriorityOrder:
    """AC6 — the silent-drop regression this plan exists to prevent.

    Pre-C6 `evaluate()` has no `aggregate` parameter at all; a caller could
    never observe more than the first-priority advisory. See the module
    docstring's (a) entry for how this was confirmed to fail against that
    legacy signature."""

    def test_all_n_returned_in_priority_order(self, monkeypatch):
        calls = []
        guards = [
            _fake_advisory("g-high-priority", 10, calls=calls),
            _fake_advisory("g-mid-priority", 20, calls=calls),
            _fake_advisory("g-low-priority", 30, calls=calls),
        ]
        monkeypatch.setattr(engine, "_discover_guards", lambda: (guards, []))
        payload = _payload(session_id="")

        out = engine.evaluate(payload, aggregate=True)

        assert isinstance(out, list)
        assert len(out) == 3, "expected all three firing advisories, not just the first"
        assert calls == ["g-high-priority", "g-mid-priority", "g-low-priority"]
        contexts = [entry["hookSpecificOutput"]["additionalContext"] for entry in out]
        assert contexts == [
            "advisory:g-high-priority",
            "advisory:g-mid-priority",
            "advisory:g-low-priority",
        ], "aggregate result must preserve PRIORITY order"

    def test_no_firing_advisories_returns_empty_list_not_none(self, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([], []))
        payload = _payload(session_id="")

        out = engine.evaluate(payload, aggregate=True)

        assert out == []


class TestLegacyCallerUnchanged:
    """AC6 — a caller that omits `aggregate` (every existing call site)
    keeps today's byte-identical single-envelope contract."""

    def test_legacy_call_returns_exactly_one_unchanged(self, monkeypatch):
        calls = []
        guards = [
            _fake_advisory("g-high-priority", 10, calls=calls),
            _fake_advisory("g-mid-priority", 20, calls=calls),
        ]
        monkeypatch.setattr(engine, "_discover_guards", lambda: (guards, []))
        payload = _payload(session_id="")

        out = engine.evaluate(payload)

        assert isinstance(out, dict)
        assert out["hookSpecificOutput"]["additionalContext"] == "advisory:g-high-priority"
        # First-non-None-wins: evaluation stops at the first firing advisory,
        # so the lower-priority guard's check() never runs.
        assert calls == ["g-high-priority"]


class TestHardDenyShortCircuitsAggregate:
    """AC7 — a hard-deny still short-circuits before any advisory runs, in
    BOTH the legacy and aggregate shapes, and no advisory text leaks into
    the deny envelope."""

    def _guards(self, advisory_calls):
        return [
            _fake_hard_deny("h-deny", 5),
            _fake_advisory("g-should-not-run", 10, calls=advisory_calls),
        ]

    def test_aggregate_zero_advisories_evaluated_no_advisory_text(self, monkeypatch):
        advisory_calls = []
        monkeypatch.setattr(engine, "_discover_guards", lambda: (self._guards(advisory_calls), []))
        payload = _payload(session_id="")

        out = engine.evaluate(payload, aggregate=True)

        assert advisory_calls == [], "advisory guard must never run once a hard-deny fires"
        assert isinstance(out, dict), "a hard-deny result is the single envelope dict, not a list"
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "advisory" not in hso["permissionDecisionReason"]
        assert "g-should-not-run" not in json.dumps(out)

    def test_legacy_zero_advisories_evaluated_no_advisory_text(self, monkeypatch):
        advisory_calls = []
        monkeypatch.setattr(engine, "_discover_guards", lambda: (self._guards(advisory_calls), []))
        payload = _payload(session_id="")

        out = engine.evaluate(payload)

        assert advisory_calls == []
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "g-should-not-run" not in json.dumps(out)


class TestRecordAdvisoryFireCountMatchesReturnedAdvisories:
    """AC8 — `_record_advisory_fire` runs once per RETURNED advisory under
    `aggregate=True`, not once per `evaluate()` call. Also pins the C6
    docstring's AC8/C21 distinguishability note: a single `evaluate()` call
    can now legitimately append more than one record."""

    def test_aggregate_appends_one_record_per_returned_advisory(self, tmp_path, monkeypatch):
        guards = [
            _fake_advisory("g-first", 10),
            _fake_advisory("g-second", 20),
            _fake_advisory("g-third", 30),
        ]
        monkeypatch.setattr(engine, "_discover_guards", lambda: (guards, []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="sess-c7-agg", cwd=str(tmp_path)), aggregate=True)

        assert len(out) == 3
        path = _counts_path(tmp_path, "sess-c7-agg")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        # Distinguishability note: THIS single evaluate() call produced
        # three records, not the "at most one per call" arity the pre-C6
        # counter guaranteed — a reader of the time series must attribute
        # this to the aggregate=True calling convention, not to three
        # separate triggering events.
        assert len(lines) == 3
        names = [json.loads(line)["guard"] for line in lines]
        assert names == ["g-first", "g-second", "g-third"]

    def test_legacy_call_still_appends_exactly_one_record(self, tmp_path, monkeypatch):
        guards = [
            _fake_advisory("g-first", 10),
            _fake_advisory("g-second", 20),
        ]
        monkeypatch.setattr(engine, "_discover_guards", lambda: (guards, []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        engine.evaluate(_payload(session_id="sess-c7-legacy", cwd=str(tmp_path)))

        path = _counts_path(tmp_path, "sess-c7-legacy")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["guard"] == "g-first"


class TestDiscoveryStableAcrossRepeatedCalls:
    """AC5 — C5 (this plan's memoization chunk) is `wont_do`, so
    `_discover_guards()` genuinely re-runs every call; this pins that the
    re-run is still stable (identical guard set/order) and that
    `skipped_out` is populated correctly on the SECOND and later calls
    within one process, not only the first — the failure mode a naive
    "only wire skipped_out for the first call" memoization attempt would
    reintroduce."""

    def test_identical_guard_set_and_order_across_calls(self, monkeypatch):
        discover_calls = []

        def _discover():
            discover_calls.append(1)
            return (
                [
                    _fake_advisory("g-alpha", 10),
                    _fake_advisory("g-beta", 20),
                ],
                ["broken-module"],
            )

        monkeypatch.setattr(engine, "_discover_guards", _discover)

        first_skipped = []
        second_skipped = []
        out1 = engine.evaluate(_payload(session_id=""), skipped_out=first_skipped, aggregate=True)
        out2 = engine.evaluate(_payload(session_id=""), skipped_out=second_skipped, aggregate=True)

        assert len(discover_calls) == 2, "no memoization landed (C5 wont_do) — discovery must re-run"
        assert [e["hookSpecificOutput"]["additionalContext"] for e in out1] == [
            e["hookSpecificOutput"]["additionalContext"] for e in out2
        ]
        assert first_skipped == ["broken-module"]
        assert second_skipped == ["broken-module"], (
            "skipped_out must be populated on the SECOND call too, not only the first"
        )

    def test_skipped_out_reflects_a_change_between_calls_not_stale_from_first(self, monkeypatch):
        """A stricter probe than mere stability: if the underlying import
        failures actually differ between calls (e.g. a module starts
        importing cleanly mid-process), `skipped_out` on the SECOND call
        must reflect THAT call's own discovery pass, not a value cached
        from the first."""
        responses = [
            ([_fake_advisory("g-alpha", 10)], ["flaky-module"]),
            ([_fake_advisory("g-alpha", 10)], []),
        ]

        def _discover():
            return responses.pop(0)

        monkeypatch.setattr(engine, "_discover_guards", _discover)

        first_skipped = []
        second_skipped = []
        engine.evaluate(_payload(session_id=""), skipped_out=first_skipped)
        engine.evaluate(_payload(session_id=""), skipped_out=second_skipped)

        assert first_skipped == ["flaky-module"]
        assert second_skipped == [], "second call's skipped_out must reflect its OWN pass, not the first's"
