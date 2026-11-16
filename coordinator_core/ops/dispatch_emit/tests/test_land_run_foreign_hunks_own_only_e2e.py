"""Falsifier: a run whose declared shared file also carries another run's uncommitted hunks lands only its own
hunks and is not refused for the red those foreign hunks cause; driven through the real producers
(`measured_rev`, the clean-overlay verdict writer) and `land_run._handler` at default arguments."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
    measured_rev_record_path,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    DLV,
    PLAN,
    REPORT,
    RUN,
    SELF,
    SIZING,
    VERDICT,
    _author,
    _git,
    _write,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHARED = "pkg/shared.py"
SHARED_TEST = "pkg/test_shared.py"
LANDING = [SHARED, SHARED_TEST]
RED_NODE = f"{SHARED_TEST}::test_the_base_value_is_untouched"

SHARED_HEAD = "VALUE = 1\nKEEP_A = 'a'\nKEEP_B = 'b'\nKEEP_C = 'c'\nKEEP_D = 'd'\nTAIL = 'z'\n"
PEER_TEXT = SHARED_HEAD.replace("VALUE = 1\n", "VALUE = 99\n")
OWN_ONLY = SHARED_HEAD.replace("TAIL = 'z'\n", "TAIL = 'own'\n")
BOTH = PEER_TEXT.replace("TAIL = 'z'\n", "TAIL = 'own'\n")
TEST_TEXT = (
    "import shared\n\n\n"
    "def test_the_base_value_is_untouched():\n    assert shared.VALUE == 1\n\n\n"
    "def test_the_own_hunk_landed():\n    assert shared.TAIL == 'own'\n"
)


def _stage(root) -> str:
    """Run A's baseline taken with run B's hunk already in the worktree; A then writes its own hunk and test."""
    _write(root, SHARED, SHARED_HEAD)
    _git(root, "add", SHARED)
    _git(root, "commit", "-q", "-m", "shared")
    _write(root, SHARED, PEER_TEXT)
    baseline = wb.capture(root, [SHARED])
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    _author(root, SHARED, BOTH)
    _author(root, SHARED_TEST, TEST_TEXT)
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(LANDING), report=REPORT),), deliverable_id=DLV, plan_path=PLAN
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


def _red_over_the_live_tree() -> dict:
    """The run's test verdict as measured over the live worktree: the peer's hunk turns a test red, counted caused."""
    return {
        "status": "fail", "tests_run": 2, "tests_failed": 1, "failed_tests": [RED_NODE],
        "baseline": {"caused": [RED_NODE]},
    }


def _land(root, token: str, **extra) -> dict:
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": LANDING, "foreign_claims": [], "slice_files": LANDING},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review, "session_id": SELF,
            "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT, **extra,
        },
        repo_root=root / ".git",
    )


def _measure(root) -> dict:
    """The workflow's measure then verify stages: `measured_rev`, then the clean-overlay verdict writer."""
    record = mr.measured_rev(RUN, LANDING + [REPORT], repo_root=root)
    assert cov.main(["--run-id", RUN, "--repo-root", str(root)]) == 0
    return record


def test_the_peer_hunk_makes_the_run_red_over_the_live_tree_and_green_without_it(repo, tmp_path) -> None:  # noqa: F811
    """Control: the foreign hunk is the only cause of the red, so a green overlay on HEAD plus own is real."""
    import subprocess
    import sys

    from coordinator_core.win_portability import no_console_creationflags

    _stage(repo)

    def run(root) -> int:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root), SHARED_TEST],
            cwd=str(root), capture_output=True, text=True, **no_console_creationflags(),
        ).returncode

    assert run(repo) == 1
    _write(repo, SHARED, OWN_ONLY)
    assert run(repo) == 0


def test_a_run_sharing_a_file_with_a_peers_hunks_lands_only_its_own_and_is_not_refused(repo) -> None:  # noqa: F811
    token = _stage(repo)
    record = _measure(repo)
    own_blob = _git(repo, "rev-parse", f"{record['overlay_sha']}:{SHARED}").strip()
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, targeted_tests=_red_over_the_live_tree())

    assert out.get("committed") is True and not out.get("refused"), out
    assert _git(repo, "rev-parse", "HEAD").strip() != head
    assert _git(repo, "show", f"{out['sha']}:{SHARED}") == OWN_ONLY
    assert _git(repo, "rev-parse", f"{out['sha']}:{SHARED}").strip() == own_blob
    assert _git(repo, "show", f"{out['sha']}:{SHARED_TEST}") == TEST_TEXT
    assert (repo / SHARED).read_text(encoding="utf-8") == BOTH
    assert SHARED in {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}


def test_the_overlay_verdict_the_land_relied_on_is_green_for_the_peer_free_commit(repo) -> None:  # noqa: F811
    token = _stage(repo)
    record = _measure(repo)

    out = _land(repo, token, targeted_tests=_red_over_the_live_tree())

    assert out.get("committed") is True, out
    assert record["peer_hunks"][SHARED]["split"] is True
    assert record["own_blobs"][SHARED] == _git(repo, "rev-parse", f"{record['overlay_sha']}:{SHARED}").strip()
    verdict = json.loads(cov.verdict_path(repo, RUN).read_text(encoding="utf-8"))
    assert verdict["verdict"] == "green" and not verdict["timed_out"]
    assert verdict["key"] == cov.landing_key(json.loads(measured_rev_record_path(repo, RUN).read_text(encoding="utf-8")))
    assert SHARED_TEST in {s.replace("\\", "/") for s in verdict["seeds"]}


def test_the_same_red_still_refuses_when_no_overlay_verdict_vouches_for_the_peer_free_commit(repo) -> None:  # noqa: F811
    """The other side of the decision: with no verdict on disk the red is the run's to answer for."""
    token = _stage(repo)
    mr.measured_rev(RUN, LANDING + [REPORT], repo_root=repo)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, targeted_tests=_red_over_the_live_tree())

    assert out.get("committed") is False, out
    assert "targeted-tests-failed" in json.dumps(out), out
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert (repo / SHARED).read_text(encoding="utf-8") == BOTH
