"""Unattributable provenance on a run-own path is an advisory; a path the run never baselined still refuses."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

OWN = "coordinator_core/own.py"
OTHER = "coordinator_core/other.py"
ME = "ask-20261029T000000-aaaaaa"
COARSE = "baseline delta is too coarse to attribute"


@pytest.fixture
def raising(monkeypatch):
    def boom(root, run_id, path):
        raise ValueError(COARSE)

    monkeypatch.setattr(hp, "_unproduced", boom)
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {OWN: None})
    monkeypatch.setattr(hp, "_is_run_own_artifact", lambda root, run, path: False)


def test_baselined_path_is_advisory_not_finding(raising, tmp_path):
    advisories: list = []
    found = hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None, advisories=advisories)
    assert found == []
    assert len(advisories) == 1
    assert advisories[0]["path"] == OWN and COARSE in advisories[0]["detail"]


def test_path_the_run_never_baselined_still_refuses(raising, tmp_path):
    advisories: list = []
    found = hp.unproduced_hunks(tmp_path, ME, [OTHER], own_plan=None, advisories=advisories)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert found[0].path == OTHER
    assert advisories == []


def test_run_own_artifact_is_advisory(raising, monkeypatch, tmp_path):
    monkeypatch.setattr(hp, "_is_run_own_artifact", lambda root, run, path: path == OTHER)
    advisories: list = []
    assert hp.unproduced_hunks(tmp_path, ME, [OTHER], own_plan=None, advisories=advisories) == []
    assert [a["path"] for a in advisories] == [OTHER]


def test_without_advisory_list_signature_stays_refusing(raising, tmp_path):
    found = hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]


def test_real_coarse_delta_on_baselined_path_is_advisory(monkeypatch, tmp_path):
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / OWN).write_bytes(b"a\nb\n")
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {OWN: None})
    monkeypatch.setattr(rhc, "exceeds_match_bound", lambda old, new: True)
    from coordinator_core.git import git_dir, git_state, git_objects

    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {})
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: None)
    from coordinator_core.ops.dispatch_emit import kept_work

    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data)
    monkeypatch.setattr(kept_work, "kept_fingerprints", lambda d: {})
    advisories: list = []
    assert hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None, advisories=advisories) == []
    assert len(advisories) == 1 and COARSE in advisories[0]["detail"]


def test_unattributable_run_produced_path_a_halted_peer_withholds_settles_to_head_naming_the_peer(
    raising, monkeypatch, tmp_path,
):
    from coordinator_core.ops.dispatch_emit import land_run

    peer = "ask-20261029T000000-bbbbbb"
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, run, plan, paths: {OWN: peer})
    monkeypatch.setattr(hp, "_head_blob_sha", lambda root, path: "a" * 40)
    advisories: list = []
    settled: dict = {}
    found = hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None, advisories=advisories, settled=settled)
    assert found == [] and advisories == []
    assert settled[OWN] == {"blob": "a" * 40, "owner": peer, "lines": hp.UNATTRIBUTABLE_LINES}


def test_unattributable_held_path_without_a_settle_target_is_a_finding_naming_the_peer(
    raising, monkeypatch, tmp_path,
):
    from coordinator_core.ops.dispatch_emit import land_run

    peer = "ask-20261029T000000-bbbbbb"
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, run, plan, paths: {OWN: peer})
    found = hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None, advisories=[])
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED] and peer in found[0].detail


def test_unattributable_path_no_halted_peer_withholds_stays_an_advisory(raising, monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, run, plan, paths: {})
    advisories: list = []
    assert hp.unproduced_hunks(tmp_path, ME, [OWN], own_plan=None, advisories=advisories, settled={}) == []
    assert [a["path"] for a in advisories] == [OWN]
    assert advisories[0]["detail"].startswith("provenance unreadable")
