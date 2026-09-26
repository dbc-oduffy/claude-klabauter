"""Tests for `coordinator_core.benchmarks.hook_entry_cost` -- C13 of
`docs/plans/2026-08-22-a-bash-call-stops-costing-a-second-and-a-half.md`.

THIS FILE CHANGES NO GUARD BEHAVIOUR. It proves the three instruments this
chunk adds (stage-cost measurement, guard-registration classification,
filesystem-probe enumeration) are importable, deterministic where they
should be, and produce the shapes the audit doc
(`state/audits/2026-08-22-what-the-preToolUse-chain-actually-costs.md`)
depends on -- it is not a standing regression gate over any numeric
threshold (this chunk's dispatch brief: "Do NOT fix anything in this
chunk"; there is nothing here yet to hold a ceiling against).

Spec backlink: state/dispatch-briefs/2026-08-22-a-bash-call-stops-costing-
a-second-and-a-half/C13.md
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.hook_entry_cost import (
    STAGE_LABELS,
    GuardCallVariance,
    classify_guard_registration,
    enumerate_fs_probes_for_corpus,
    measure_stage_costs,
)
from coordinator_core.benchmarks.process_time import IS_DARWIN, IS_WINDOWS

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _require_supported_platform() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "process-time accounting has no primitive for this platform -- "
            "see coordinator_core.benchmarks.process_time module docstring"
        )


def test_measure_stage_costs_covers_every_stage_label():
    _require_supported_platform()
    costs = measure_stage_costs(n=3)
    assert [c.label for c in costs] == list(STAGE_LABELS)


def test_measure_stage_costs_reports_process_time_and_wall_side_by_side():
    _require_supported_platform()
    costs = measure_stage_costs(n=3)
    for c in costs:
        assert c.process_time_p50_ms >= 0.0
        assert c.process_time_p90_ms >= c.process_time_p50_ms
        assert c.wall_p50_ms >= 0.0
        assert c.wall_p90_ms >= c.wall_p50_ms
        assert c.n == 3


def test_measure_stage_costs_bare_interpreter_cheaper_than_full_chain():
    _require_supported_platform()
    costs = {c.label: c for c in measure_stage_costs(n=5)}
    assert (
        costs["bare_interpreter"].process_time_p50_ms
        <= costs["chain_spawns_nothing"].process_time_p50_ms
    )


def test_measure_stage_costs_raises_off_unsupported_platform(monkeypatch):
    import coordinator_core.benchmarks.hook_entry_cost as hec

    monkeypatch.setattr(hec, "IS_WINDOWS", False)
    monkeypatch.setattr(hec, "IS_DARWIN", False)
    with pytest.raises(NotImplementedError):
        hec.measure_stage_costs(n=1)


def test_classify_guard_registration_returns_every_registered_entry():
    from coordinator_core.bash_guards.roster import guard_roster

    classified = classify_guard_registration()
    classified_names = {c.name for c in classified}
    roster_names = {e.id for e in guard_roster()}
    assert roster_names.issubset(classified_names)


def test_classify_guard_registration_entries_are_named_and_typed():
    for entry in classify_guard_registration():
        assert isinstance(entry, GuardCallVariance)
        assert entry.name
        assert isinstance(entry.reads_cmd, bool)
        assert isinstance(entry.reads_payload, bool)
        assert isinstance(entry.reads_session, bool)
        assert entry.candidate_session_invariant == (
            not (entry.reads_cmd or entry.reads_payload or entry.reads_session)
        )


def test_classify_guard_registration_deterministic():
    first = classify_guard_registration()
    second = classify_guard_registration()
    assert first == second


def test_classify_guard_registration_known_false_positive_pair():
    by_name = {c.name: c for c in classify_guard_registration()}
    for name in ("destructive-git-revert", "destructive-git-revert-advisory"):
        assert by_name[name].candidate_session_invariant is True


def test_enumerate_fs_probes_for_corpus_returns_positive_counts():
    rows = enumerate_fs_probes_for_corpus()
    assert rows
    for row in rows:
        assert row.count > 0
        assert row.guard_name
        assert row.payload_label


def test_enumerate_fs_probes_for_corpus_labels_are_from_the_shared_corpus():
    from coordinator_core.benchmarks.bash_dispatch_probe import CORPUS_PAYLOADS

    rows = enumerate_fs_probes_for_corpus()
    labels = {row.payload_label for row in rows}
    assert labels.issubset(set(CORPUS_PAYLOADS.keys()))
