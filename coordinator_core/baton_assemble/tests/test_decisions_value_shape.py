"""baton_assemble decisions readers treat `{"value": v}` as `{"disposition": v}` and refuse unrecognized entries."""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _write_artifact

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOTE = "operator-authorized break-glass excise"


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _artifact(tmp_path):
    predecessor = _write_artifact(
        tmp_path / "state" / "handoffs" / "predecessor.md", ["handoff_id: hnd-predecessor-1a2b3c"]
    )
    return _write_artifact(
        tmp_path / "state" / "handoffs" / "h1.md",
        [
            "deliverable_id: DEL-EXCISE-1",
            "initiative: init-excise-1",
            f"predecessor: {predecessor.relative_to(tmp_path)}",
        ],
    )


def _lineage(tmp_path, entry):
    decisions = {"j-continuation-vs-fork": entry}
    return ba.brief(
        "handoff", str(_artifact(tmp_path)), decisions=decisions, repo_root=tmp_path
    ).decision_object["artifact"]["lineage"]


def test_value_only_excise_resolves_like_disposition(tmp_path):
    by_disposition = _lineage(tmp_path, {"disposition": "excise", "decision_note": _NOTE})
    by_value = _lineage(tmp_path, {"value": "excise", "decision_note": _NOTE})
    assert by_value["predecessor"] is None
    assert by_value["standalone_no_predecessor_reason"] == _NOTE
    assert by_value["predecessor"] == by_disposition["predecessor"]


def test_unrecognized_entry_refuses_naming_key_and_shapes():
    with pytest.raises(ValueError, match=r"j-continuation-vs-fork.*\{\"value\"") as exc:
        ba._decision_entry({"j-continuation-vs-fork": {"decision_note": "x"}}, "j-continuation-vs-fork")
    assert "disposition" in str(exc.value)
    with pytest.raises(ValueError, match="j-continuation-vs-fork"):
        ba._decision_entry({"j-continuation-vs-fork": 3}, "j-continuation-vs-fork")


def test_disposition_entry_is_not_rewritten():
    entry = {"disposition": "override", "value": "payload"}
    assert ba._decision_entry({"k": entry}, "k") == entry
    assert ba._decision_entry({}, "k") is None
