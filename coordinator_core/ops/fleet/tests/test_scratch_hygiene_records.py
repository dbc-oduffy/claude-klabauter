"""Pins the fleet.scratch_hygiene record keys against DoE's JSONL example, the exit mapping, and the shared fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet import scratch_hygiene_records as rec
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import build_two_root_fixture
from coordinator_core.temp_layout import coordinator_temp_root

PURGE_KEYS = ["op", "kind", "repo", "path", "action", "bytes", "files", "age_days", "reason"]
NAG_KEYS = ["op", "kind", "repo", "path", "bytes", "age_days", "readme", "finding"]
SUMMARY_KEYS = ["op", "summary", "entries", "bytes", "applied", "capabilities"]


def test_action_enum_is_the_contracts_seven():
    assert rec.ACTIONS == (
        "deleted", "would-delete", "skipped-live", "skipped-unverified", "skipped-young", "skipped-link",
        "skipped-hold-pending",
    )


def test_purge_record_keys_and_order():
    r = rec.purge_record("repo", "scratch/e", "would-delete", bytes=3, files=1, age_days=2.5)
    assert list(r) == PURGE_KEYS
    assert r["op"] == "fleet.scratch_hygiene" and r["kind"] == "purge" and r["reason"] == ""
    json.dumps(r)


def test_purge_record_rejects_action_outside_enum():
    with pytest.raises(ValueError):
        rec.purge_record("repo", "scratch/e", "open-handle")


def test_hold_nag_record_keys_and_null_readme():
    r = rec.hold_nag_record("repo", "scratch-hold/e", finding="missing-readme")
    assert list(r) == NAG_KEYS
    assert r["readme"] is None and r["kind"] == "hold-nag"
    with pytest.raises(ValueError):
        rec.hold_nag_record("repo", "scratch-hold/e", finding="stale")


def test_summary_record_keys_and_totals():
    records = [
        rec.purge_record("r", "scratch/a", "would-delete", bytes=10),
        rec.purge_record("r", "scratch/b", "skipped-live", bytes=99),
        rec.hold_nag_record("r", "scratch-hold/c", bytes=5),
    ]
    s = rec.summary_record(records, applied=False)
    assert list(s) == SUMMARY_KEYS
    assert (s["entries"], s["bytes"], s["applied"]) == (3, 10, False)


def test_path_is_repo_relative_and_forward_slashed(tmp_path):
    p = tmp_path / "repo" / "scratch" / "a" / "b"
    r = rec.purge_record("repo", p, "deleted", repo_root=tmp_path / "repo")
    assert r["path"] == "scratch/a/b"
    r2 = rec.hold_nag_record("repo", "scratch-hold\\x" if os.sep == "\\" else "scratch-hold/x")
    assert "\\" not in r2["path"]


@pytest.mark.parametrize(
    "records, expected",
    [
        ([], 0),
        ([rec.purge_record("r", "scratch/a", "would-delete"), rec.purge_record("r", "scratch/b", "deleted")], 0),
        ([rec.hold_nag_record("r", "scratch-hold/a", readme="x | y | z")], 0),
        ([rec.purge_record("r", "scratch/a", "skipped-young")], 1),
        ([rec.purge_record("r", "scratch/a", "skipped-link")], 1),
        ([rec.hold_nag_record("r", "scratch-hold/a", finding="missing-readme")], 1),
    ],
)
def test_contract_exit_mapping(records, expected):
    assert rec.contract_exit(records) == expected


def test_contract_exit_ignores_summary_record():
    assert rec.contract_exit([rec.summary_record([], applied=False)]) == 0


def test_two_root_fixture_shape(tmp_path, monkeypatch):
    fx = build_two_root_fixture(tmp_path, monkeypatch)
    try:
        assert fx.temp_root == coordinator_temp_root(fx.repo_root)
        assert fx.temp_root.is_relative_to(tmp_path)
        assert is_junction(fx.top_link) and is_junction(fx.nested_link)
        assert fx.top_link.resolve() == fx.outside.resolve()
        assert not fx.outside.resolve().is_relative_to(fx.scratch.resolve())
        assert set(fx.expected.values()) == {
            "would-delete", "skipped-link", "skipped-young", "skipped-live", "skipped-unverified",
        }
        roots = {p.parent for p in fx.would_delete()}
        assert fx.scratch in roots and fx.temp_root in roots
        assert fx.hold_readme.is_dir() and fx.hold_missing.is_dir()
        assert not any(p.name.startswith("README") for p in fx.hold_missing.iterdir())
        assert sum(1 for _ in fx.registry_dir.glob("*.json")) == 1
        assert not fx._handle.closed
    finally:
        fx.close()
    assert fx._handle is None
