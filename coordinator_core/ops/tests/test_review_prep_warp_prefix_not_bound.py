"""A run's review.prep binds only its own prep prefix when a sibling warp run's
`manifest-warp-<run>-<base>-prep[-N]` artifacts and base-keyed pointer sit in the same share dir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
from coordinator_core.ops.tests.test_review_prep import _base, _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
ASK = "ask-20261118T180107-7d0882"
WARP = "warp-2026-11-18-1bba15-1-adopt-orphan-stall-dir-import-fence-20261118T174659-417cc6"
SHARED = "shared/tests/test_dispositions.py"


def _prep(repo: Path, run: str) -> dict:
    return rp._handler(
        {"base": _base(repo), "plan_path": "state/warp/x/manifest.json", "worktree": True, "session_id": SESSION, "run_id": run},
        repo,
    )


def _artifacts(reply: dict) -> list:
    out = [reply["whole_diff_path"], reply["sidecar_path"]]
    return out + [p for s in reply["slices"] for p in (s["diff_path"], s["sidecar_path"])]


def _share(repo: Path) -> Path:
    return Path(rp.share_dir(str(repo), SESSION))


@pytest.fixture
def warp_sibling(repo):
    """The warp run's prep landed first over many files plus the shared one; the ask run edits only the shared file."""
    warp_files = [f"pkg{i % 4}/sub/mod{i}.py" for i in range(19)] + [SHARED]
    _write(repo, warp_files, 30)
    _stage(repo, warp_files, run_id=WARP)
    _stage(repo, [SHARED], run_id=ASK)
    warp = _prep(repo, WARP)
    assert warp["error"] is None and len(warp["slices"]) > 1
    return warp


def test_the_ask_run_prep_names_only_its_own_prefix(repo, warp_sibling):
    base = _base(repo)
    own, foreign = run_prep_slice_prefix(ASK, base), run_prep_slice_prefix(WARP, base)
    reply = _prep(repo, ASK)
    assert reply["error"] is None
    paths = _artifacts(reply)
    assert all(own in p for p in paths) and not any(foreign in p for p in paths)
    assert [f for s in reply["slices"] for f in s["files"]] == [SHARED]


def test_a_stale_base_pointer_naming_a_warp_sidecar_is_never_taken(repo, warp_sibling):
    base = _base(repo)
    warp_sidecar = f"{run_prep_slice_prefix(WARP, base)}.review-prep.md"
    pointer = _share(repo) / rp._pointer_name(None, base)
    pointer.write_text(json.dumps({"sidecar": warp_sidecar, "ambiguous": False}), encoding="utf-8")
    assert rp.stored_reply_for_run(repo, SESSION, base) is None
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=ASK) is None
    reply = _prep(repo, ASK)
    assert reply["error"] is None and run_prep_slice_prefix(ASK, base) in reply["sidecar_path"]
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=ASK)["sidecar_path"] == reply["sidecar_path"]


def test_a_warp_prefix_writes_no_base_only_pointer(repo, warp_sibling):
    base = _base(repo)
    assert not (_share(repo) / rp._pointer_name(None, base)).exists()
    assert (_share(repo) / rp._pointer_name(WARP, base)).is_file()
