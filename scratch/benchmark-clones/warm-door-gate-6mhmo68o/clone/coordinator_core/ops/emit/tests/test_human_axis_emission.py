"""C9: activation-gated human_* population in the emit sections.

Purpose: the behavioural half of C9 (b) — with the machine-local registry switch
(``_shared.human_axis_vendored``) OFF (the default), `handoffs.py`/`trackers.py` emit
BYTE-IDENTICAL records to today's shape: no `human_assignee`/`human_claimant`/`human_owner`
key at all, gated or not, present or null. With the switch ON, the sections populate the
keys from frontmatter, same caller-supplied-then-passthrough discipline as every other
frontmatter-sourced field in these sections.

This is also the test `test_human_axis_stays_off_the_wire.py`'s new behavioural leg (module
docstring, C9 body part (c)) relies on — written once, shared by both rows' intent.

Spec backlink: docs/plans/2026-08-19-the-tracker-names-an-owner.md § C9
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section
from coordinator_core.ops.emit.sections import trackers as trackers_section


def _make_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-08-20T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-08-20",
        "status": "open",
        "deployment_state": "ready_to_fire",
    }
    fm.update(overrides)
    return fm


def _collect_handoffs(mock_qr, tmp_path: Path, records: list[dict]):
    ctx = _make_ctx(tmp_path)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return records
        return []

    mock_qr.side_effect = query_records
    return handoffs_section.collect(ctx)


def _collect_trackers(mock_qr, tmp_path: Path, records: list[dict]):
    ctx = _make_ctx(tmp_path)
    mock_qr.return_value = records
    return trackers_section.collect(ctx)


# ---------------------------------------------------------------------------
# handoffs.py
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs.human_axis_vendored", return_value=False)
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_handoffs_switch_off_omits_human_keys_entirely(mock_qr, _mock_flag, tmp_path: Path) -> None:
    records, malformed = _collect_handoffs(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(human_assignee="abc123def", human_claimant="abc123def"),
        }],
    )
    assert malformed == []
    assert len(records) == 1
    assert "human_assignee" not in records[0]
    assert "human_claimant" not in records[0]


@patch("coordinator_core.ops.emit.sections.handoffs.human_axis_vendored", return_value=True)
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_handoffs_switch_on_populates_from_frontmatter(mock_qr, _mock_flag, tmp_path: Path) -> None:
    records, malformed = _collect_handoffs(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(human_assignee="abc123def", human_claimant="def456ghi"),
        }],
    )
    assert malformed == []
    assert len(records) == 1
    assert records[0]["human_assignee"] == "abc123def"
    assert records[0]["human_claimant"] == "def456ghi"


@patch("coordinator_core.ops.emit.sections.handoffs.human_axis_vendored", return_value=True)
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_handoffs_switch_on_null_when_frontmatter_absent(mock_qr, _mock_flag, tmp_path: Path) -> None:
    records, malformed = _collect_handoffs(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm()}],
    )
    assert malformed == []
    assert len(records) == 1
    assert records[0]["human_assignee"] is None
    assert records[0]["human_claimant"] is None


@patch("coordinator_core.ops.emit.sections.handoffs.human_axis_vendored", return_value=False)
@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_handoffs_switch_off_is_byte_identical_to_flag_absent_frontmatter(
    mock_qr, _mock_flag, tmp_path: Path
) -> None:
    """The behavioural leg AC7 depends on: a record whose frontmatter carries NO
    human_* keys at all, emitted with the switch off, produces exactly the same dict
    shape as one whose frontmatter DOES carry them — the switch, not the frontmatter,
    is what gates the key's presence."""
    without_fm, _ = _collect_handoffs(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm()}],
    )
    with_fm, _ = _collect_handoffs(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(human_assignee="abc123def", human_claimant="abc123def"),
        }],
    )
    assert without_fm == with_fm


# ---------------------------------------------------------------------------
# trackers.py
# ---------------------------------------------------------------------------

def _base_tracker_fm(**overrides) -> dict:
    fm = {"title": "Test Tracker", "created": "2026-08-20", "status": "active"}
    fm.update(overrides)
    return fm


@patch("coordinator_core.ops.emit.sections.trackers.human_axis_vendored", return_value=False)
@patch("coordinator_core.ops.emit.sections.trackers._query_tracker_records")
def test_trackers_switch_off_omits_human_owner_entirely(mock_qr, _mock_flag, tmp_path: Path) -> None:
    mock_qr.return_value = [{
        "path": "docs/project-tracker.md",
        "frontmatter": _base_tracker_fm(human_owner="abc123def"),
    }]
    records, malformed = _collect_trackers(mock_qr, tmp_path, mock_qr.return_value)
    assert malformed == []
    assert len(records) == 1
    assert "human_owner" not in records[0]


@patch("coordinator_core.ops.emit.sections.trackers.human_axis_vendored", return_value=True)
@patch("coordinator_core.ops.emit.sections.trackers._query_tracker_records")
def test_trackers_switch_on_populates_from_frontmatter(mock_qr, _mock_flag, tmp_path: Path) -> None:
    mock_qr.return_value = [{
        "path": "docs/project-tracker.md",
        "frontmatter": _base_tracker_fm(human_owner="abc123def"),
    }]
    records, malformed = _collect_trackers(mock_qr, tmp_path, mock_qr.return_value)
    assert malformed == []
    assert len(records) == 1
    assert records[0]["human_owner"] == "abc123def"


@patch("coordinator_core.ops.emit.sections.trackers.human_axis_vendored", return_value=False)
@patch("coordinator_core.ops.emit.sections.trackers._query_tracker_records")
def test_trackers_switch_off_is_byte_identical_to_flag_absent_frontmatter(
    mock_qr, _mock_flag, tmp_path: Path
) -> None:
    mock_qr.return_value = [{"path": "docs/project-tracker.md", "frontmatter": _base_tracker_fm()}]
    without_fm, _ = _collect_trackers(mock_qr, tmp_path, mock_qr.return_value)
    mock_qr.return_value = [{
        "path": "docs/project-tracker.md",
        "frontmatter": _base_tracker_fm(human_owner="abc123def"),
    }]
    with_fm, _ = _collect_trackers(mock_qr, tmp_path, mock_qr.return_value)
    assert without_fm == with_fm


# ---------------------------------------------------------------------------
# _shared.human_axis_vendored — the switch itself
# ---------------------------------------------------------------------------

def test_flag_defaults_off_when_registry_key_unresolved():
    with patch("coordinator_core.ops.emit.sections._shared.registry_get", return_value=None):
        from coordinator_core.ops.emit.sections._shared import human_axis_vendored
        assert human_axis_vendored() is False


def test_flag_on_only_for_truthy_values():
    from coordinator_core.ops.emit.sections._shared import human_axis_vendored

    for value in ("1", "true", "True", "yes", "on", " true ", "  YES  "):
        with patch("coordinator_core.ops.emit.sections._shared.registry_get", return_value=value):
            assert human_axis_vendored() is True, value

    for value in ("0", "false", "no", "off", "garbage", ""):
        with patch("coordinator_core.ops.emit.sections._shared.registry_get", return_value=value):
            assert human_axis_vendored() is False, value
