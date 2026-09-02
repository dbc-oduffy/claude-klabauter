"""
coordinator_core.ops.tests.test_group_em_crown_instrument_ops -- JSON-RPC
veneer tests for "groupem.stamp", "groupem.resolve_addressee", and
"groupem.idle_report".

Spec backlink: state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-themselves/C7.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_idle_report as gei
from coordinator_core.ops import group_em_resolve_addressee as gera
from coordinator_core.ops import group_em_stamp as ges
from coordinator_core.op_scopes import OP_KEY_SCOPE


# ---------------------------------------------------------------------------
# groupem.stamp
# ---------------------------------------------------------------------------


def test_stamp_resolves_through_registry():
    assert "groupem.stamp" in _registry_map.OP_MODULE_MAP
    assert _registry_map.OP_MODULE_MAP["groupem.stamp"] == "coordinator_core.ops.group_em_stamp"
    assert OP_KEY_SCOPE["groupem.stamp"] == "none"
    assert ipc._REGISTRY.get("groupem.stamp") is not None


def test_stamp_mutating_classification_registered():
    assert OP_CLASSIFICATION["groupem.stamp"] is OpClass.MUTATING


def test_stamp_returns_same_answer_as_underlying_function(tmp_path, monkeypatch):
    calls = {}

    def _fake_stamp(repo_root, holder_session_id, declinations, interval_seconds, **kwargs):
        calls["args"] = (repo_root, holder_session_id, declinations, interval_seconds)
        calls["kwargs"] = kwargs
        return True

    monkeypatch.setattr(ges.watch_heartbeat, "stamp", _fake_stamp)
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: "caller-sid-1")

    result = ges._groupem_stamp(
        {
            "repo_root": str(tmp_path),
            "declinations": [{"session_id": "sid-x"}],
            "interval_seconds": 60,
        }
    )

    assert result == {"stamped": True}
    assert calls["args"] == (
        str(tmp_path),
        "caller-sid-1",
        [{"session_id": "sid-x"}],
        60,
    )
    assert calls["kwargs"]["writer_session_id"] == "caller-sid-1"


def test_stamp_false_on_decline_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr(ges.watch_heartbeat, "stamp", lambda *a, **k: False)
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: "caller-sid-1")

    result = ges._groupem_stamp({"repo_root": str(tmp_path), "interval_seconds": 60})

    assert result == {"stamped": False}


# ---------------------------------------------------------------------------
# groupem.resolve_addressee
# ---------------------------------------------------------------------------


def test_resolve_addressee_resolves_through_registry():
    assert "groupem.resolve_addressee" in _registry_map.OP_MODULE_MAP
    assert (
        _registry_map.OP_MODULE_MAP["groupem.resolve_addressee"]
        == "coordinator_core.ops.group_em_resolve_addressee"
    )
    assert OP_KEY_SCOPE["groupem.resolve_addressee"] == "none"
    assert ipc._REGISTRY.get("groupem.resolve_addressee") is not None


def test_resolve_addressee_compute_only_classification_registered():
    assert OP_CLASSIFICATION["groupem.resolve_addressee"] is OpClass.COMPUTE_ONLY


def test_resolve_addressee_returns_same_answer_as_underlying_function(tmp_path, monkeypatch):
    calls = {}

    def _fake_resolve(repo_root, peer_session_id):
        calls["args"] = (repo_root, peer_session_id)
        return "peer-name-42"

    monkeypatch.setattr(gera.group_em_send_pass, "resolve_addressee", _fake_resolve)

    result = gera._groupem_resolve_addressee(
        {"repo_root": str(tmp_path), "peer_session_id": "sid-peer"}
    )

    assert result == {"name": "peer-name-42"}
    assert calls["args"] == (str(tmp_path), "sid-peer")


def test_resolve_addressee_none_is_a_refusal_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(gera.group_em_send_pass, "resolve_addressee", lambda *a, **k: None)

    result = gera._groupem_resolve_addressee(
        {"repo_root": str(tmp_path), "peer_session_id": "sid-peer"}
    )

    assert result == {"name": None}


# ---------------------------------------------------------------------------
# groupem.idle_report
# ---------------------------------------------------------------------------


def test_idle_report_resolves_through_registry():
    assert "groupem.idle_report" in _registry_map.OP_MODULE_MAP
    assert (
        _registry_map.OP_MODULE_MAP["groupem.idle_report"]
        == "coordinator_core.ops.group_em_idle_report"
    )
    assert OP_KEY_SCOPE["groupem.idle_report"] == "none"
    assert ipc._REGISTRY.get("groupem.idle_report") is not None


def test_idle_report_compute_only_classification_registered():
    assert OP_CLASSIFICATION["groupem.idle_report"] is OpClass.COMPUTE_ONLY


def test_idle_report_returns_same_answer_as_underlying_function(tmp_path, monkeypatch):
    fake_report = {"repo-root": str(tmp_path), "peers": [], "counts": {"peers": 0}}
    calls = {}

    def _fake_build_report(repo_root, **kwargs):
        calls["repo_root"] = repo_root
        calls["kwargs"] = kwargs
        return fake_report

    monkeypatch.setattr(gei.group_em_idle_report, "build_report", _fake_build_report)

    result = gei._groupem_idle_report(
        {"repo_root": str(tmp_path), "group_em_session_id": "gem-sid", "peer": "sid-"}
    )

    assert result is fake_report
    assert calls["repo_root"] == str(tmp_path)
    assert calls["kwargs"]["group_em_session_id"] == "gem-sid"
    assert calls["kwargs"]["peer"] == "sid-"


def test_idle_report_observed_exits_unhashable_raises_named_value_error(tmp_path, monkeypatch):
    """P2 pin: an accepted shape (a list) with unhashable contents (dicts) must
    raise a named `ValueError`, not propagate the bare `TypeError` frozenset()
    itself raises."""

    def _fake_build_report(repo_root, **kwargs):
        raise AssertionError("must not reach build_report on a mis-shaped observed_exits")

    monkeypatch.setattr(gei.group_em_idle_report, "build_report", _fake_build_report)

    with pytest.raises(ValueError) as excinfo:
        gei._groupem_idle_report(
            {"repo_root": str(tmp_path), "observed_exits": [{"session_id": "x"}]}
        )

    assert "observed_exits must contain hashable" in str(excinfo.value)


def test_stamp_refuses_when_no_holder_can_be_resolved(tmp_path, monkeypatch):
    """A crown row that names no crown is worse than no row.

    `caller_session_id` returns Optional[str], and `watch_heartbeat.stamp`
    validates only `writer_session_id`. A call supplying an explicit writer
    while the environment carries no session id therefore reached the writer
    with `holder_session_id=None` and wrote it verbatim.
    """
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: None)

    with pytest.raises(ValueError) as excinfo:
        ges._groupem_stamp(
            {
                "repo_root": str(tmp_path),
                "declinations": [],
                "interval_seconds": 30.0,
                "writer_session_id": "writer-with-no-holder",
            }
        )

    assert "holder_session_id is unresolvable" in str(excinfo.value)
    assert not (tmp_path / "state" / "group-em-watch.json").exists()


def test_stamp_refuses_a_writer_naming_itself_as_someone_else(tmp_path, monkeypatch):
    """A guard authenticated by the party it guards is not a guard.

    `writer_session_id` is what `is_fresh_and_foreign` compares to decide
    whether to decline, and what `_writer_identity` compares to decide
    whether to persist a `prior_*` trace. Accepting it as a free wire param
    let one caller supply another instrument's identity and thereby bypass
    the decline AND suppress the trace in a single call -- destroying that
    instrument's declination rows with no record at all.
    """
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: "real-caller-1111")

    with pytest.raises(ValueError) as excinfo:
        ges._groupem_stamp(
            {
                "repo_root": str(tmp_path),
                "declinations": [],
                "interval_seconds": 30.0,
                "writer_session_id": "some-other-crown-2222",
            }
        )

    assert "disagrees with this caller's resolved identity" in str(excinfo.value)
    assert not (tmp_path / "state" / "group-em-watch.json").exists()


def test_stamp_refuses_an_unverifiable_writer_claim_with_no_resolved_caller(tmp_path, monkeypatch):
    """P1 fail-closed pin: an unresolvable caller must not disarm the guard.

    Supplying `holder_session_id` explicitly sidesteps the earlier
    unresolvable-holder raise, so this reaches the writer-identity branch
    with `resolved_caller` falsy -- previously the `elif resolved_caller and
    ...` guard short-circuited to False here and let ANY writer_session_id
    through unverified. It must now raise instead.
    """
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: None)

    with pytest.raises(ValueError) as excinfo:
        ges._groupem_stamp(
            {
                "repo_root": str(tmp_path),
                "holder_session_id": "explicit-holder-1234",
                "declinations": [],
                "interval_seconds": 30.0,
                "writer_session_id": "unverifiable-writer-5678",
            }
        )

    assert "unresolvable" in str(excinfo.value)
    assert not (tmp_path / "state" / "group-em-watch.json").exists()


def test_stamp_accepts_a_writer_that_agrees_with_the_caller(tmp_path, monkeypatch):
    """The refusal is on DISAGREEMENT, never on the param being supplied."""
    monkeypatch.setattr(ges.group_em_read_pass, "caller_session_id", lambda: "real-caller-1111")

    assert ges._groupem_stamp(
        {
            "repo_root": str(tmp_path),
            "declinations": [],
            "interval_seconds": 30.0,
            "writer_session_id": "real-caller-1111",
        }
    )["stamped"] is True
