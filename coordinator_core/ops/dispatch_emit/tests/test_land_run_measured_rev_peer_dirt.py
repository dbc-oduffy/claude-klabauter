"""A shared registration file carrying a peer run's hunks at measure time judges by the run's own hunks."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git as _git_out, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_measured_tree_gate import _VERDICT, _record, _stage
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import C3_PATH, C4_PATH, LANE, LANE_TEST
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

REG = "coordinator_core/ops/_registry_map.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
PEER_AT_BASELINE = SEED.replace("line2\n", "peer-two\n")


def _own(text: str) -> str:
    return text.replace("line10\n", "own-ten\n")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags())


def _blob(root, text):
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode())


@pytest.fixture
def reg_repo(tmp_path, monkeypatch):
    for cmd in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "t"),
                ("config", "commit.gpgsign", "false"), ("config", "core.autocrlf", "false")):
        _git(tmp_path, *cmd)
    (tmp_path / REG).parent.mkdir(parents=True)
    (tmp_path / REG).write_text(SEED, encoding="utf-8", newline="\n")
    _git(tmp_path, "add", REG)
    _git(tmp_path, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(land_run, "_head_entries", lambda r, paths: {})
    return tmp_path


def _mismatch(root, measured, landed, settled=None):
    (root / REG).write_text(landed, encoding="utf-8", newline="\n")
    record = {"blobs": {REG: _blob(root, measured)}, "removed": []}
    baseline = {REG: _blob(root, PEER_AT_BASELINE)}
    return land_run._measured_mismatch(root, record, [REG], [], settled or {}, baseline=baseline)


def test_peer_hunks_in_measured_blob_but_not_in_landed_blob_is_no_drift(reg_repo):
    measured = _own(PEER_AT_BASELINE)
    stripped = _own(SEED)
    assert _mismatch(reg_repo, measured, stripped, {REG: _blob(reg_repo, stripped)}) == []


def test_peer_hunk_added_after_measure_leaves_unchanged_own_hunks_no_drift(reg_repo):
    measured = _own(PEER_AT_BASELINE)
    landed = measured.replace("line5\n", "late-peer-five\n")
    assert _mismatch(reg_repo, measured, landed) == []


def test_late_peer_hunk_does_not_mask_a_changed_own_hunk(reg_repo):
    measured = _own(PEER_AT_BASELINE)
    landed = measured.replace("line5\n", "late-peer-five\n").replace("own-ten\n", "own-ten-changed\n")
    assert _mismatch(reg_repo, measured, landed) == [REG]


def test_run_with_no_measured_own_hunks_does_not_absorb_late_hunks(reg_repo):
    landed = PEER_AT_BASELINE.replace("line5\n", "late-peer-five\n")
    assert _mismatch(reg_repo, PEER_AT_BASELINE, landed) == [REG]


def test_own_hunk_changed_after_measure_still_refuses(reg_repo):
    measured = _own(PEER_AT_BASELINE)
    landed = measured.replace("own-ten\n", "own-ten-changed\n")
    assert _mismatch(reg_repo, measured, landed) == [REG]


def test_own_hunk_missing_from_landed_blob_still_refuses(reg_repo):
    assert _mismatch(reg_repo, _own(PEER_AT_BASELINE), PEER_AT_BASELINE) == [REG]


def test_without_a_baseline_the_whole_blob_comparison_stands(reg_repo):
    (reg_repo / REG).write_text(_own(SEED), encoding="utf-8", newline="\n")
    record = {"blobs": {REG: _blob(reg_repo, _own(PEER_AT_BASELINE))}, "removed": []}
    assert land_run._measured_mismatch(reg_repo, record, [REG], [], {}) == [REG]


def test_unmeasured_path_holding_only_peer_dirt_is_not_the_runs(reg_repo):
    baseline = {REG: _blob(reg_repo, PEER_AT_BASELINE)}
    (reg_repo / REG).write_text(PEER_AT_BASELINE, encoding="utf-8", newline="\n")
    assert land_run._peer_dirt_only(reg_repo, REG, baseline)


def test_unmeasured_path_holding_an_own_hunk_is_still_the_runs(reg_repo):
    baseline = {REG: _blob(reg_repo, PEER_AT_BASELINE)}
    (reg_repo / REG).write_text(_own(PEER_AT_BASELINE), encoding="utf-8", newline="\n")
    assert not land_run._peer_dirt_only(reg_repo, REG, baseline)
    assert not land_run._peer_dirt_only(reg_repo, REG, {})


def test_land_inner_drops_unmeasured_paths_that_hold_only_peer_dirt(halted, monkeypatch):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH, C4_PATH])
    monkeypatch.setattr(land_run, "_peer_dirt_only", lambda root, rel, baseline: rel in {LANE, LANE_TEST})

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    landed = set(_git_out(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert LANE_TEST not in landed


def test_land_inner_still_refuses_unmeasured_paths_that_are_not_peer_dirt(halted, monkeypatch):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH, C4_PATH])
    monkeypatch.setattr(land_run, "_peer_dirt_only", lambda root, rel, baseline: rel == LANE)

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [LANE_TEST]
