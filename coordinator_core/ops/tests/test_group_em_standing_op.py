"""
coordinator_core.ops.tests.test_group_em_standing_op -- veneer tests for
"groupem.standing" (registration, read-only behaviour, degrade-never-raise).

Spec: docs/plans/2026-10-01-groupem-standing-op.md (C1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import nomination, watch_heartbeat
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_standing as ges


def test_registration_quad():
    assert ipc._REGISTRY.get("groupem.standing") is not None
    assert _registry_map.OP_MODULE_MAP["groupem.standing"] == "coordinator_core.ops.group_em_standing"
    assert OP_KEY_SCOPE["groupem.standing"] == "none"
    assert OP_CLASSIFICATION["groupem.standing"] is OpClass.COMPUTE_ONLY


def test_read_only_over_fixture_record_and_heartbeat(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setattr(
        nomination, "is_live", lambda record: nomination.LivenessResult(True, "live")
    )
    monkeypatch.setattr(
        nomination, "entry_status", lambda record, *a, **k: {"status": "verified"}
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_root = str(repo.resolve())

    record_path = nomination._record_path(repo_root)
    nomination._write_json_atomic(
        record_path, {"repo_root": repo_root, "session_id": "sid-holder"}
    )
    hb_path = Path(watch_heartbeat.watch_path(repo_root))
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps({"holder_session_id": "sid-holder"}), encoding="utf-8")

    before = {
        p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (record_path, hb_path)
    }
    result = ges._groupem_standing({"repo_root": repo_root, "peer": "sid-holder"})

    assert result["nomination"]["standing"] == "live"
    assert result["nomination"]["live"] is True
    assert "watch_liveness" in result
    after = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (record_path, hb_path)}
    assert after == before


def test_no_peer_uses_who(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(ges.nomination, "who", lambda root: seen.setdefault("who", root) and None)
    monkeypatch.setattr(ges.watch_heartbeat, "read_liveness", lambda root, now: {"verdict": "absent"})
    result = ges._groupem_standing({"repo_root": str(tmp_path)})
    assert seen["who"] == str(tmp_path)
    assert result == {"nomination": None, "watch_liveness": {"verdict": "absent"}}


def test_raising_legs_degrade_without_raising(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ges.nomination, "who", _boom)
    monkeypatch.setattr(ges.watch_heartbeat, "read_liveness", _boom)
    result = ges._groupem_standing({"repo_root": str(tmp_path)})
    assert result["nomination"] is None
    assert result["watch_liveness"] is None
    assert result["nomination_error"] == "RuntimeError: boom"
    assert result["watch_liveness_error"] == "RuntimeError: boom"
