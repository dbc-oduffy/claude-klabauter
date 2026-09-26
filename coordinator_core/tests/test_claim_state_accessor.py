
from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at:
        (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path: Path, *, claimed_by: str = "", consumed_by: str = "", claimed_at: str = "", status: str = "open") -> None:
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    if consumed_by:
        lines.append(f"consumed_by: {consumed_by}")
    if claimed_at:
        lines.append(f"claimed_at: {claimed_at}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    common_dir = tmp_path / "gitdir"
    common_dir.mkdir()
    handoff = tmp_path / "state" / "handoffs" / "2026-08-07-example.md"
    handoff.parent.mkdir(parents=True)
    return common_dir, handoff


def test_both_sources_present_agree(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-a"
    assert state.source == "ledger"
    assert state.disagreement is False
    assert state.ledger_holder == "sess-a"
    assert state.mirror_holder == "sess-a"


def test_ledger_only_holder_live(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-b", "2026-08-07T11:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-b"
    assert state.claimed_at == "2026-08-07T11:00:00Z"
    assert state.source == "ledger"
    assert state.mirror_holder is None


def test_mirror_only_no_ledger(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-c", claimed_at="2026-08-07T12:00:00Z", status="claimed")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-c"
    assert state.source == "mirror"
    assert state.disagreement is False
    assert state.ledger_holder is None


def test_disagreement_ledger_claims_mirror_does_not(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-d", "2026-08-07T13:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.disagreement is True
    assert state.holder == "sess-d"
    assert state.source == "ledger"
    assert state.mirror_holder is None


def test_ledger_holder_dead_degrades_to_mirror(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-e", "2026-08-07T14:00:00Z")
    _write_handoff(handoff, claimed_by="sess-e", claimed_at="2026-08-07T14:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder == "sess-e"
    assert state.source == "mirror"
    assert state.disagreement is False


def test_legacy_consumed_by_shape(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, consumed_by="sess-f", claimed_at="2026-08-07T15:00:00Z", status="claimed")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder == "sess-f"
    assert state.source == "mirror"


def test_neither_source_present(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.holder is None
    assert state.source == "none"
    assert state.disagreement is False


def test_ledger_liveness_check_raises_degrades_to_mirror(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-g", "2026-08-07T16:00:00Z")
    _write_handoff(handoff, claimed_by="sess-g", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", side_effect=RuntimeError("boom")):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder == "sess-g"
    assert state.source == "mirror"


def test_handoff_claim_dir_reexported_from_fleet_common():
    from coordinator_core.ops.fleet import _common

    assert _common.handoff_claim_dir is claim_state.handoff_claim_dir
    assert _common._sessions_dir is claim_state._sessions_dir


def test_handoff_claim_dir_path_shape(tmp_path):
    common_dir = tmp_path / "gitdir"
    handoff = Path("state/handoffs/example.md")
    got = claim_state.handoff_claim_dir(common_dir, handoff)
    assert got == common_dir / "coordinator-sessions" / "handoff-claims" / "example.md"


def test_no_subprocess_re_shelled_out_per_call(workspace, monkeypatch):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "git_common_dir") as mocked_git_common_dir:
        claim_state.resolve_claim_state(handoff, common_dir=common_dir)
        mocked_git_common_dir.assert_not_called()


def test_git_common_dir_is_lru_cached_when_common_dir_omitted(workspace):
    from coordinator_core.lifecycle import git_common_dir

    assert hasattr(git_common_dir, "cache_info"), (
        "lifecycle.git_common_dir must stay lru_cache'd — claim_state.py's "
        "no-common_dir fallback path relies on this to avoid a subprocess "
        "shell-out per call."
    )


def test_malformed_session_id_degrades_not_raises(workspace):
    common_dir, handoff = workspace
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff.name
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_bytes(b"\xff\xfe\x00bad")
    _write_handoff(handoff, status="open")

    state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder is None
    assert state.holder is None
    assert state.source == "none"


def test_malformed_claimed_at_degrades_not_raises(workspace):
    common_dir, handoff = workspace
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff.name
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text("sess-h", encoding="utf-8")
    (claim_dir / "claimed_at").write_bytes(b"\xff\xfe\x00bad")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.ledger_holder == "sess-h"
    assert state.claimed_at is None


def test_disagreement_flag_narrower_than_full_holder_mismatch(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-x", "2026-08-07T17:00:00Z")
    _write_handoff(handoff, claimed_by="sess-y", claimed_at="2026-08-07T16:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)

    assert state.disagreement is False
    assert state.ledger_holder == "sess-x"
    assert state.mirror_holder == "sess-y"
    assert state.ledger_holder != state.mirror_holder


def test_historical_claim_ignores_holder_liveness(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-dead", "2026-08-07T14:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        assert claim_state.resolve_claim_state(handoff, common_dir=common_dir).ledger_holder is None
        record = claim_state.resolve_historical_claim(handoff, common_dir=common_dir)

    assert record == ("sess-dead", "2026-08-07T14:00:00Z")


def test_historical_claim_absent_ledger_is_none(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-mirror", status="claimed")

    assert claim_state.resolve_historical_claim(handoff, common_dir=common_dir) is None


def test_historical_claim_never_consults_the_mirror(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-mirror", claimed_at="2026-08-07T09:00:00Z", status="claimed")

    assert claim_state.resolve_historical_claim(handoff, common_dir=common_dir) is None


def test_historical_claim_missing_claimed_at_still_names_the_holder(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-noat")
    _write_handoff(handoff, status="open")

    assert claim_state.resolve_historical_claim(handoff, common_dir=common_dir) == ("sess-noat", None)


def test_comparator_agree(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.verdict == "agree"
    assert report.ledger_holder == "sess-a"
    assert report.mirror_holder == "sess-a"
    assert report.age is None
    assert report.ledger_resolver_source == "not-recorded"
    assert report.bound_exceeded is False


def test_comparator_ledger_only_reports_age_off_ledger_claimed_at(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(
            handoff, common_dir=common_dir, now=1786096800.0 + 60.0
        )

    assert report.verdict == "ledger-only"
    assert report.mirror_holder is None
    assert isinstance(report.age, float)
    assert report.age == pytest.approx(60.0, abs=1.0)
    assert report.bound_exceeded is False


def test_comparator_mirror_only_age_not_measurable(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, claimed_by="sess-mirror", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.verdict == "mirror-only"
    assert report.age == claim_state.AGE_NOT_MEASURABLE
    assert report.bound_exceeded is False


def test_comparator_holder_mismatch_ages_off_ledger_side(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-x", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-y", claimed_at="2026-08-07T09:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(
            handoff, common_dir=common_dir, now=1786096800.0 + 10.0
        )

    assert report.verdict == "holder-mismatch"
    assert report.ledger_holder == "sess-x"
    assert report.mirror_holder == "sess-y"
    assert isinstance(report.age, float)
    assert report.age == pytest.approx(10.0, abs=1.0)


def test_comparator_neither_no_age(workspace):
    common_dir, handoff = workspace
    _write_handoff(handoff, status="open")

    report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.verdict == "neither"
    assert report.age is None
    assert report.ledger_holder is None
    assert report.mirror_holder is None


def test_comparator_malformed_claimed_at_degrades_to_not_measurable(workspace):
    common_dir, handoff = workspace
    claim_dir = _write_claim_dir(common_dir, handoff.name, "sess-a")
    (claim_dir / "claimed_at").write_text("not-a-timestamp", encoding="utf-8")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.verdict == "ledger-only"
    assert report.age == claim_state.AGE_NOT_MEASURABLE
    assert report.bound_exceeded is False


def test_comparator_resolver_source_not_recorded_by_default(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.ledger_resolver_source == "not-recorded"


def test_comparator_resolver_source_read_when_present(workspace):
    common_dir, handoff = workspace
    claim_dir = _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    (claim_dir / "resolver_source").write_text("attributable_session_id:warm", encoding="utf-8")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert report.ledger_resolver_source == "attributable_session_id:warm"


def test_comparator_bound_exceeded_is_a_named_reported_field(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, status="open")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        under_bound = claim_state.compare_claim_state(
            handoff, common_dir=common_dir, now=1786096800.0 + 10.0
        )
        over_bound = claim_state.compare_claim_state(
            handoff,
            common_dir=common_dir,
            now=1786096800.0 + claim_state.DISAGREEMENT_AGE_BOUND_SECONDS_300 + 1.0,
        )

    assert under_bound.bound_exceeded is False
    assert over_bound.bound_exceeded is True
    assert over_bound.bound_seconds == claim_state.DISAGREEMENT_AGE_BOUND_SECONDS_300


def test_comparator_composes_resolve_claim_state_not_a_second_read(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-a", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-a", claimed_at="2026-08-07T10:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True) as live:
        claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert live.call_count == 1


def test_comparator_never_touches_disagreement_flag(workspace):
    common_dir, handoff = workspace
    _write_claim_dir(common_dir, handoff.name, "sess-x", "2026-08-07T10:00:00Z")
    _write_handoff(handoff, claimed_by="sess-y", claimed_at="2026-08-07T09:00:00Z", status="claimed")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        state = claim_state.resolve_claim_state(handoff, common_dir=common_dir)
        report = claim_state.compare_claim_state(handoff, common_dir=common_dir)

    assert state.disagreement is False
    assert report.verdict == "holder-mismatch"


def test_import_cycle_stays_broken():
    import coordinator_core.claim_state  # noqa: F401
    import coordinator_core.ops  # noqa: F401
    import coordinator_core.ops.fleet._common  # noqa: F401
    import coordinator_core.session.claims  # noqa: F401
    import coordinator_core.baton_assemble.apply  # noqa: F401
