"""`groupem.standing` -- read-only Group EM who/standing reads and their registrations."""

from __future__ import annotations

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import nomination
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_standing as ops_standing


def test_registered_on_every_surface():
    assert _registry_map.OP_MODULE_MAP["groupem.standing"] == "coordinator_core.ops.group_em_standing"
    assert OP_KEY_SCOPE["groupem.standing"] == "none"
    assert OP_CLASSIFICATION["groupem.standing"] is OpClass.COMPUTE_ONLY
    assert ipc._REGISTRY.get("groupem.standing") is not None


def test_no_record_reports_none(tmp_path, monkeypatch):
    monkeypatch.setattr(nomination, "read_record", lambda root, directory=None: None)
    assert ops_standing._groupem_standing({"repo_root": str(tmp_path)})["nomination"] is None


def _patch_holder(monkeypatch, *, live):
    monkeypatch.setattr(
        nomination, "read_record", lambda root, directory=None: {"session_id": "sid-1", "repo_root": root}
    )
    monkeypatch.setattr(
        nomination,
        "is_live",
        lambda record: nomination.LivenessResult(live, "live" if live else "pid_not_running"),
    )
    row = type("Row", (), {"session_id": "sid-1", "name": "crown"})()
    monkeypatch.setattr(nomination.session_registry, "read_rows", lambda: [row])


def test_who_annotates_liveness_without_peer(tmp_path, monkeypatch):
    _patch_holder(monkeypatch, live=True)
    record = ops_standing._groupem_standing({"repo_root": str(tmp_path)})["nomination"]
    assert record["live"] is True
    assert "standing" not in record


def test_standing_live_by_name_and_by_id(tmp_path, monkeypatch):
    _patch_holder(monkeypatch, live=True)
    for peer in ("crown", "sid-1"):
        result = ops_standing._groupem_standing({"repo_root": str(tmp_path), "peer": peer})
        assert result["nomination"]["standing"] == "live"


def test_standing_not_live_and_no_match(tmp_path, monkeypatch):
    _patch_holder(monkeypatch, live=False)
    for peer, expected in (("crown", "not_live"), ("other", "no_match")):
        result = ops_standing._groupem_standing({"repo_root": str(tmp_path), "peer": peer})
        assert result["nomination"]["standing"] == expected


def test_op_never_writes(tmp_path, monkeypatch):
    _patch_holder(monkeypatch, live=True)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    ops_standing._groupem_standing({"repo_root": str(tmp_path), "peer": "crown"})
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
