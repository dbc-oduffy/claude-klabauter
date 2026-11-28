"""dispatch.land_run builds its landing set from the run's own measured-rev record: a worktree that
checks in to the measured blobs lands (also over a peer's hunks the measurement verified), a commit_set
path the record never measured is excluded and reported, and a recorded path a live peer run also
writes is refused naming that run and the path."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
    measured_rev_record_path,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    BOTH,
    DLV,
    PEER,
    PLAN,
    REPORT,
    RUN,
    SEED,
    SELF,
    SIZING,
    TARGET,
    _author,
    _git,
    _land,
    _stage,
    _write,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER = "pkg/other.py"
PEER_RUN = "ask-20261031T095959-ffffff"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000e3"
LANDING_TREE_DIFFERS = "landing-tree-differs-from-measured"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _record(root, paths):
    """The measured-rev record where the producer writes it, over the worktree bytes as they are now."""
    rec = measured_rev_record_path(root, RUN)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": list(paths), "removed": [], "tree": "0" * 40,
            "blobs": {p: commit.checkin_blob_sha(root, p) for p in paths},
        }),
        encoding="utf-8",
    )


def _declare_peer(root, paths):
    """A live peer run of another session declaring *paths*, minted before the landing run."""
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(paths), report="reports/P1.md"),),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{PEER_RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{PEER_RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{PEER_RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{PEER_RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{PEER_RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{PEER_RUN}/manifest.json\n")
    rl.start(root, PEER_RUN, all_of_pm_ask=False)
    rl.bind(root, PEER_RUN, SIZING)
    run_token.mint(root, PEER_RUN, f"{PEER_RUN}.mjs", session_id=PEER_SESSION)
    time.sleep(0.05)


def _land_slice(root, token, slice_files, incomplete=("X1",)):
    """`_land` with a review slice covering *slice_files*."""
    review = {
        **reviewed(),
        "prep": {
            "run_base_sha": None, "product_files": list(slice_files), "foreign_claims": [],
            "slice_files": list(slice_files),
        },
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": list(incomplete), "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _declare_with(root, baseline, paths):
    """`_stage`'s declaration, for a commit request carrying more than TARGET."""
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(paths), report=REPORT),), deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", write_baseline=baseline,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def test_worktree_equal_to_measured_blobs_lands(repo):  # noqa: F811
    own = SEED.replace("line10\n", "own10\n")
    token = _stage(repo, SEED, own)
    _record(repo, [TARGET])

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert out.get("refused") != LANDING_TREE_DIFFERS, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == own


def test_peer_hunks_in_a_measured_path_land_with_the_worktree_blob(repo):  # noqa: F811
    token = _stage(repo, PEER, BOTH)
    _record(repo, [TARGET])
    measured = commit.checkin_blob_sha(repo, TARGET)

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert out.get("refused") != LANDING_TREE_DIFFERS, out
    assert _git(repo, "rev-parse", f"HEAD:{TARGET}").strip() == measured
    assert _git(repo, "show", f"HEAD:{TARGET}") == BOTH


def test_a_commit_set_path_the_record_never_measured_is_excluded_and_reported(repo):  # noqa: F811
    own = SEED.replace("line10\n", "own10\n")
    _write(repo, OTHER, "foreign = 0\n")
    _git(repo, "add", OTHER)
    _git(repo, "commit", "-q", "-m", "other")
    _write(repo, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    _author(repo, TARGET, own)
    _write(repo, OTHER, "foreign = 100\n")
    token = _declare_with(repo, {}, [TARGET, OTHER])
    _record(repo, [TARGET])

    out = _land_slice(repo, token, [TARGET, OTHER])

    assert out.get("committed") is True, out
    assert out.get("refused") != LANDING_TREE_DIFFERS, out
    landed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert TARGET in landed and OTHER not in landed
    assert out.get("excluded_unmeasured") == [OTHER], out
    assert OTHER in {ln[3:] for ln in _git(repo, "status", "--porcelain", "-uall").splitlines()}
    assert (repo / OTHER).read_text(encoding="utf-8") == "foreign = 100\n"


def test_a_recorded_path_a_live_peer_run_also_writes_is_refused_naming_the_run_and_path(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    _declare_peer(repo, [TARGET])
    own = SEED.replace("line10\n", "own10\n")
    token = _stage(repo, SEED, own)
    _record(repo, [TARGET])
    _write(repo, REPORT, "DONE: X1\n")
    _write(repo, TARGET, own + "late = 1\n")  # the worktree moved on after the measurement
    head = _git(repo, "rev-parse", "HEAD")

    out = _land_slice(repo, token, [TARGET], incomplete=())

    assert out.get("committed") is not True, out
    assert PEER_RUN in out["error"] and TARGET in out["error"], out
    assert _git(repo, "rev-parse", "HEAD") == head
