"""A halt records `{base, run}` blob shas for every path the run wrote, evicting nothing, and names
dirty paths no run manifest declares under `ownerless`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit, halted_record_eviction, stranded_sibling
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = f"state/warp/{RUN}"
HEAD_BYTES = b"x = 1\n"
RUN_BYTES = b"x = 2\n"


def test_record_holds_base_and_run_blobs_for_every_written_path(repo):
    _write_record(repo, RUN)
    (repo / "product.py").write_bytes(RUN_BYTES)
    (repo / "fresh.py").write_bytes(b"new = 1\n")
    paths: list = []

    halted_record_eviction.record_touched_blobs(repo, REL, ["product.py", "fresh.py", "gone.py"], paths)

    rec = stranded_sibling.read(repo / REL)
    assert rec["product.py"] == {"base": blob_sha(HEAD_BYTES), "run": blob_sha(RUN_BYTES)}
    assert rec["fresh.py"] == {"base": None, "run": blob_sha(b"new = 1\n")}
    assert rec["gone.py"] == {"base": None, "run": None}
    assert paths == [f"{REL}/{stranded_sibling.TOUCHED_FILE}"]


def test_manifest_write_baseline_outranks_head_for_base(repo):
    rec_dir = _write_record(repo, RUN)
    (rec_dir / "manifest.json").write_text(
        json.dumps({"write_baseline": {"product.py": "a" * 40, "fresh.py": None}}), encoding="utf-8"
    )
    (repo / "product.py").write_bytes(RUN_BYTES)

    halted_record_eviction.record_touched_blobs(repo, REL, ["product.py", "fresh.py"], [])

    rec = stranded_sibling.read(repo / REL)
    assert rec["product.py"]["base"] == "a" * 40
    assert rec["fresh.py"]["base"] is None


def test_no_record_dir_writes_nothing(repo):
    (repo / "product.py").write_bytes(RUN_BYTES)
    paths: list = []

    halted_record_eviction.record_touched_blobs(repo, REL, ["product.py"], paths)

    assert paths == []
    assert not (repo / REL).exists()


def test_a_halt_that_evicts_nothing_still_writes_the_record_and_names_ownerless(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "product.py").write_bytes(RUN_BYTES)
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *a, **k: ["product.py"])

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert "evicted" not in out
    rec = stranded_sibling.read(repo / REL)
    assert rec["product.py"] == {"base": blob_sha(HEAD_BYTES), "run": blob_sha(RUN_BYTES)}
    assert out["ownerless"] == ["product.py"]


def test_a_manifest_declared_path_is_not_ownerless(repo, monkeypatch):
    rec_dir = _write_record(repo, RUN)
    (rec_dir / "manifest.json").write_text(
        json.dumps({"write_baseline": {"product.py": blob_sha(HEAD_BYTES)}}), encoding="utf-8"
    )
    (repo / "product.py").write_bytes(RUN_BYTES)
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *a, **k: ["product.py"])

    out = _call(repo)

    assert "ownerless" not in out, out
    assert "product.py" in stranded_sibling.read(repo / REL)
