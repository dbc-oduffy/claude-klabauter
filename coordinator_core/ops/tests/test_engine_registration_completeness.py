"""Tests for engine.registration_completeness (AC4-AC8)."""

from __future__ import annotations

import asyncio
import json
import subprocess

import coordinator_core.ops as ops_pkg
from coordinator_core.authz.registration_quad import QuadViolation, _discover_all_ops
from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.ops import engine_registration_completeness as mod

OP = "engine.registration_completeness"


def _v(op_key: str) -> QuadViolation:
    return QuadViolation(
        op_key=op_key,
        surfaces_present=("OP_CLASSIFICATION",),
        surfaces_missing=("_OP_KEY_SCOPE",),
        missing_surface_files=(("_OP_KEY_SCOPE", "coordinator_core/op_scopes.py"),),
    )


def test_build_report_partitions_live_and_suspended():
    report = mod.build_report(
        [_v("zz.live"), _v("zz.suspended")],
        suspended=frozenset({"zz.suspended"}),
        generation={"engine_root": "x"},
        registered_op_count=2,
    )
    assert report["complete"] is False
    assert [r["op_key"] for r in report["violations"]] == ["zz.live"]
    assert [r["op_key"] for r in report["suspended_by_design"]] == ["zz.suspended"]
    assert report["registered_op_count"] == 2
    json.dumps(report)


def test_build_report_complete_when_only_suspended_remain():
    report = mod.build_report(
        [_v("zz.suspended")],
        suspended=frozenset({"zz.suspended"}),
        generation={},
        registered_op_count=1,
    )
    assert report["complete"] is True
    assert report["violations"] == []


def test_eager_registry_equals_full_walk():
    ops_pkg._eager_import_all()
    eager = set(_REGISTRY)
    _discover_all_ops()
    assert set(_REGISTRY) == eager


def test_dispatch_returns_complete_and_self_clean():
    msg = {"jsonrpc": "2.0", "id": 1, "method": OP, "params": {}}
    d = asyncio.run(dispatch_message(msg))
    result = d["result"]
    assert result["complete"] is True
    assert OP not in {r["op_key"] for r in result["violations"]}
    assert OP not in {r["op_key"] for r in result["suspended_by_design"]}
    assert result["registered_op_count"] > 0
    json.dumps(result)


def test_handler_spawns_no_process(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert mod._engine_registration_completeness({})["complete"] is True


def test_generation_reads_stamp_files(monkeypatch, tmp_path):
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "_engine_stamp").write_text("sha:" + "ab" * 20 + "\n", encoding="utf-8")
    (pkg / "_engine_published_at").write_text("2026-09-30T21:57:53Z\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_engine_root", lambda: tmp_path)
    gen = mod._generation()
    assert gen["stamp_sha"] == "ab" * 20
    assert gen["published_at"].startswith("2026-09-30T21:57:53")
    assert gen["engine_root"] == tmp_path.as_posix()


def test_generation_none_without_stamp_files(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_engine_root", lambda: tmp_path)
    gen = mod._generation()
    assert gen["stamp_sha"] is None
    assert gen["published_at"] is None
