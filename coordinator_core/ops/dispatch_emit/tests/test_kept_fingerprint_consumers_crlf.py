"""Every consumer of a raw kept or baseline fingerprint reads a CRLF file under core.autocrlf as it reads LF.

One case per consumer, driven through its module-level entry on a real ``core.autocrlf=true`` repo
(CRLF worktree, LF in HEAD and the index, keep and baseline recorded raw). The test id names the
consumer; ``same`` is the answer the unchanged file must get, ``different`` the negative control.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.dispatch_emit import (
    ask_contract,
    halted_hold_release,
    halted_hunk_attribution,
    halted_record_commit,
    kept_work,
    land_run_halt,
    sibling_claims,
    terminal_commit,
    write_baseline,
)
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import crlf_repo, git, hold_kept

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER = b"gamma\r\ndelta\r\n"
HALTED = "ask-20261024T000000-aaaaaa"
LANDING = "ask-20261024T020000-bbbbbb"
HALT_AT = "2026-10-24T01:00:00Z"
START_AT = "2026-10-24T02:00:00Z"
RUN = "ask-20261024T000000-c8c8c8"


def _drift(root, rel, same: bool) -> None:
    if not same:
        (root / rel).write_bytes(OTHER)


def _stage(root, run_id, *, baseline, stage="staged", last=START_AT, started=START_AT) -> None:
    rdir = ask_contract.record_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(json.dumps({"write_baseline": baseline}), encoding="utf-8")
    (rdir / "run-stage.json").write_text(
        json.dumps({"stage": stage, "started_at": started, "last_activity_at": last}), encoding="utf-8"
    )


def _still_kept(tmp_path, same):
    root, rel, _, raw = crlf_repo(tmp_path)
    _drift(root, rel, same)
    return kept_work.still_kept(root, {"paths": {rel: raw}}, [], "C1")


def _post_checkpoint(tmp_path, same):
    root, rel, _, _ = crlf_repo(tmp_path)
    record = hold_kept(root, "run-a", [], post=[rel])
    _drift(root, rel, same)
    holding, drifted = kept_work.kept_post_checkpoint(root, record)
    return rel in holding and not drifted


def _persist_blobs(tmp_path, same):
    root, rel, _, raw = crlf_repo(tmp_path)
    _drift(root, rel, same)
    written = halted_record_commit._persist_blobs(root, resolve_git_common_dir(root), {rel: raw})
    return rel in written


def _hunk_kept_blobs_branch(tmp_path, same):
    """The kept_blobs branch holds the path; with the keep gone the landing baseline would release it."""
    root, rel, _, raw = crlf_repo(tmp_path)
    git(root, "add", rel)
    hold_kept(root, HALTED, [rel])
    _drift(root, rel, same)
    other = kept_work.blob_sha(b"old\r\n")
    _stage(root, HALTED, baseline={rel: other}, stage="halted", last=HALT_AT, started="2026-10-24T00:00:00Z")
    _stage(root, LANDING, baseline={rel: other})
    return halted_hunk_attribution.reader(root, LANDING)(HALTED, [rel]) == {rel}


def _hunk_landing_baseline_branch(tmp_path, same):
    """No keep; the index holds the LF form of the landing baseline, so the path releases."""
    root, rel, committed, raw = crlf_repo(tmp_path)
    assert git(root, "rev-parse", f":{rel}") == committed
    _drift(root, rel, same)
    landing_baseline = raw if same else write_object(resolve_git_common_dir(root), b"blob", b"zeta\r\n")
    _stage(root, HALTED, baseline={rel: kept_work.blob_sha(b"old\r\n")}, stage="halted", last=HALT_AT,
           started="2026-10-24T00:00:00Z")
    _stage(root, LANDING, baseline={rel: landing_baseline})
    return halted_hunk_attribution.reader(root, LANDING)(HALTED, [rel]) == set()


def _own_dirt_kept_branch(tmp_path, same):
    root, rel, _, _ = crlf_repo(tmp_path)
    hold_kept(root, RUN, [rel])
    _drift(root, rel, same)
    return sibling_claims._own_dirt(root, RUN, rel, {rel: None})


def _own_dirt_baseline_branch(tmp_path, same):
    """HEAD holds the LF form of the baseline; the run then edited the file, so the dirt is its own."""
    root, rel, committed, raw = crlf_repo(tmp_path)
    _stage(root, RUN, baseline={rel: raw})
    (root / rel).write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    return sibling_claims._own_dirt(root, RUN, rel, {rel: committed if same else "1" * 40})


def _unwritten(tmp_path, same):
    root, rel, _, raw = crlf_repo(tmp_path)
    baseline = write_baseline.capture(root, [rel])
    assert baseline == {rel: raw}
    _drift(root, rel, same)
    return write_baseline.unwritten(root, baseline, [rel]) == [rel]


def _terminal_commit_no_change(tmp_path, same):
    """The unchanged CRLF path whose index holds its LF form routes 'revise the ask', not 'predates the run'."""
    root, rel, _, raw = crlf_repo(tmp_path)
    if not same:
        (root / rel).write_bytes(OTHER)
        raw = kept_work.fingerprint(root, rel)
    row = ManifestRow(id="C1", agent_type="x", model="m", brief_path="b", writes=(rel,), wave=0,
                      register_rows=("R1",))
    manifest = StageManifest(run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
                             write_baseline={rel: raw})
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(rel,)),))
    out = terminal_commit._no_change_refusal(root, manifest, request, None)
    return "revise the ask" in out["error"]


def _hold_release(tmp_path, same):
    root, rel, _, raw = crlf_repo(tmp_path)
    if not same:
        raw = write_object(resolve_git_common_dir(root), b"blob", OTHER)
    record = hold_kept(root, RUN, [rel])
    kept_work.write(record, {"c1": {"paths": {rel: raw}}}, None)
    marker = json.loads((record / "halted.json").read_text(encoding="utf-8"))
    marker["run_artifacts"] = {"withheld": {rel: "halt-record-only"}}
    (record / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return halted_hold_release.kept_at_head_paths(root, record, marker) == {rel}


def _land_run_halt_differs(tmp_path, same):
    root, rel, committed, _ = crlf_repo(tmp_path)
    entries = {rel: ("100644", committed if same else "1" * 40)}
    differ, gone = land_run_halt._differs(root, entries, [rel])
    assert gone == set()
    return differ == set()


CASES = {
    "kept_work.still_kept": _still_kept,
    "kept_work.kept_post_checkpoint": _post_checkpoint,
    "halted_record_commit._persist_blobs": _persist_blobs,
    "halted_hunk_attribution.kept_blobs": _hunk_kept_blobs_branch,
    "halted_hunk_attribution.landing_baseline": _hunk_landing_baseline_branch,
    "sibling_claims._own_dirt.kept": _own_dirt_kept_branch,
    "sibling_claims._own_dirt.baseline": _own_dirt_baseline_branch,
    "write_baseline.unwritten": _unwritten,
    "terminal_commit._no_change_refusal": _terminal_commit_no_change,
    "halted_hold_release.kept_at_head_paths": _hold_release,
    "land_run_halt._differs": _land_run_halt_differs,
}


@pytest.mark.parametrize("consumer", CASES)
def test_crlf_file_reads_as_an_lf_file_does(consumer, tmp_path):
    assert CASES[consumer](tmp_path, True) is True


@pytest.mark.parametrize("consumer", CASES)
def test_different_content_still_reads_as_different(consumer, tmp_path):
    assert CASES[consumer](tmp_path, False) is False


def test_committed_sha_comes_from_the_real_filter_and_differs_from_the_raw_keep(tmp_path):
    root, rel, committed, raw = crlf_repo(tmp_path)
    assert committed == git(root, "rev-parse", f"HEAD:{rel}") and committed != raw
    assert kept_work.fingerprint(root, rel) == raw
    assert kept_work.matches_committed(root, rel, raw, committed)
