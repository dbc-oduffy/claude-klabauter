"""Pins the plan_chain contract against the vendored DoE wake-digest schema."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops.plan_chain import contract

SCHEMA = Path(contract.__file__).resolve().parents[2] / "contract" / "doe-wake-digest.schema.json"
SCHEMA_SHA256 = "9a294b16d18c2e3c43b34a7930da664608521527b4ab8dc2b9db095eb4dd7a57"


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_stages_equal_schema_chain_stage_enum():
    assert list(contract.STAGES) == _schema()["$defs"]["chain_stage"]["enum"]


def test_halts_values_are_stages_or_sentinel():
    for halt_id, stage in contract.HALTS.items():
        assert stage in contract.STAGES or stage == contract.STAGE_RELATIVE, halt_id


def test_halts_cover_c0_table():
    expected = {
        "plan-no-digest", "ready-gate-not-ready", "stamp-stale-substantive",
        "roadmap-gate-shut", "peer-claim", "spine-check-failed",
        "external-gate-uncleared", "falsifier-owed", "dirty-write-set",
        "cross-repo-write-approval", "executor-block", "review-fail",
        "falsifier-not-met", "usage-limit", "fire-cap-reached", "child-no-digest",
        "terminal-commit-refused",
    }
    assert set(contract.HALTS) == expected


def test_vendored_schema_sha256_pinned():
    actual = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    assert actual == SCHEMA_SHA256, (
        "doe-wake-digest.schema.json drifted from coordinator-content-repo@7a00c6681 "
        "(1.3.1); re-vendor, do not edit"
    )


def test_halt_truncates_reason_and_rejects_unknown_stage():
    assert len(contract.Halt("plan", "x" * 500).reason) == contract.HALT_REASON_MAX
    with pytest.raises(ValueError):
        contract.Halt("bogus", "r")


def test_stage_relative_halt_takes_running_stage():
    assert contract.halt("usage-limit", "r", running_stage="execute").halted_at == "execute"
    with pytest.raises(ValueError):
        contract.halt("usage-limit", "r")
    assert contract.halt("plan-no-digest", "r").halted_at == "plan"


def test_manifest_round_trips_through_json():
    m = contract.ChainManifest(
        sizing_object="state/sizings/s.yaml", baton="b", deliverable_id=None,
        interaction_mode="pm", repo_root="r", trail_dir="t",
        wave_args={"a": [1, 2], "b": {"c": "d"}}, script_source="plan-blitz.mjs",
    )
    assert contract.ChainManifest.from_json(m.to_json()) == m


def test_manifest_path_and_constants():
    assert contract.manifest_path("trail", 3, "b.d") == Path("trail") / "chain-3-b.d.json"
    assert contract.CHAIN_BIN == "plan-chain-run"
    assert contract.CHAIN_FLAG == "--chain"


def _manifest(sizing, baton="b", deliverable=None):
    return contract.ChainManifest(
        sizing_object=sizing, baton=baton, deliverable_id=deliverable, interaction_mode="pm",
        repo_root="r", trail_dir="t", wave_args={}, script_source="s.mjs",
    )


def test_chain_key_keys_on_baton_and_deliverable():
    assert contract.chain_key("hnd-1") == "hnd-1"
    assert contract.chain_key("hnd-1", "D 2") == "hnd-1.D_2"


def test_write_manifest_is_idempotent_for_one_chain_and_refuses_another(tmp_path):
    p = tmp_path / "chain-0-b.json"
    contract.write_manifest(p, _manifest("s1.yaml"))
    contract.write_manifest(p, _manifest("s1.yaml"))
    with pytest.raises(contract.ChainManifestCollision, match="s1.yaml.*s2.yaml"):
        contract.write_manifest(p, _manifest("s2.yaml"))
    assert contract.ChainManifest.from_json(p.read_text(encoding="utf-8")).sizing_object == "s1.yaml"
