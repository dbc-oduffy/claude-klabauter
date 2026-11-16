"""Review prep and the review floor classify product files from one source, over one diff."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import review_floor
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
LAND_RUN = "coordinator_core/ops/dispatch_emit/land_run.py"
LAND_TEST = "coordinator_core/ops/dispatch_emit/tests/test_land_run.py"
RUN_DIR = "state/warp/warp-2026-11-16-x-20261116T183359-eb18a6"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _put(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    _put(tmp_path, "docs/plans/p.md", "---\nplan_id: pln-test-1\n---\n\n# p\n")
    _put(tmp_path, LAND_RUN, "v = 1\n")
    _put(tmp_path, LAND_TEST, "t = 1\n")
    for i in range(30):
        _put(tmp_path, f"{RUN_DIR}/rec{i}.json", "{}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _refire_paths(repo: Path) -> list[str]:
    """A refire's declared set: the prior run's records (some committed, some new) plus the
    product files being edited."""
    for i in range(30, 63):
        _put(repo, f"{RUN_DIR}/rec{i}.json", "{}\n")
    _put(repo, LAND_RUN, "v = 2\n")
    _put(repo, LAND_TEST, "t = 2\n")
    return [*(f"{RUN_DIR}/rec{i}.json" for i in range(63)), LAND_RUN, LAND_TEST]


def _prep(repo: Path, paths: list[str]) -> dict:
    return rp.review_prep(
        repo, base=_git(repo, "rev-parse", "HEAD").strip(), paths=paths, slice_prefix="prep-t",
        plan_path="docs/plans/p.md", session_id=SESSION,
    )


def test_a_refire_diff_touching_land_run_gets_a_slice_and_the_floor_agrees(repo) -> None:
    paths = _refire_paths(repo)
    reply = _prep(repo, paths)
    assert reply["error"] is None
    sliced = sorted(f for s in reply["slices"] for f in s["files"])
    assert LAND_RUN in sliced and LAND_TEST in sliced
    assert reply["product_files"] == len(sliced) > 0

    request = SimpleNamespace(chunks=[SimpleNamespace(paths=paths, prefixes=[])])
    floor = review_floor.floor_code_paths(request, {"prep": {"product_files": reply["product_files"]}}, True)
    assert LAND_RUN in floor
    assert set(floor) <= set(sliced)
    assert review_floor.floor_reason(floor, 0) is not None
    assert review_floor.floor_reason(floor, 1) is None


def _nodelta_prep(repo: Path) -> tuple[list[str], dict]:
    paths = [*(f"{RUN_DIR}/rec{i}.json" for i in range(30)), LAND_RUN, LAND_TEST]
    return paths, _prep(repo, paths)


def test_product_rule_is_one_function_for_prep_and_floor() -> None:
    from coordinator_core.ops.review_stamp import is_product_path

    for rel in (LAND_RUN, f"{RUN_DIR}/rec1.json", "docs/plans/x.md", "scratch/a.py", "tasks/t.md"):
        assert is_product_path(rel) == (rel in review_floor.reviewable_paths([rel]) or rel.startswith("scratch/"))


def test_a_nodelta_declared_land_run_matches_the_stored_prep_despite_extra_record_paths(repo) -> None:
    paths, reply = _nodelta_prep(repo)
    assert reply["error"] is None and reply["product_files"] == 0 and reply["slices"] == []
    run_key, base = "run-x", reply["run_base_sha"]
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    reply2 = rp.review_prep(
        repo, base=base, paths=paths, slice_prefix=run_prep_slice_prefix(run_key, base),
        plan_path="docs/plans/p.md", session_id=SESSION,
    )
    assert reply2["error"] is None
    extra = [*paths, "state/warp/other-run/halted.json"]
    hit = rp.stored_reply_for_run(repo, SESSION, base, run_key=run_key, within=extra, covering=True)
    assert hit is not None and hit["product_files"] == 0
    request = SimpleNamespace(chunks=[SimpleNamespace(paths=extra, prefixes=[])])
    assert review_floor.floor_code_paths(request, {"prep": {"product_files": hit["product_files"]}}, True) == []


def test_an_extra_code_path_still_voids_the_stored_prep_match(repo) -> None:
    paths, reply = _nodelta_prep(repo)
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run_key, base = "run-y", reply["run_base_sha"]
    rp.review_prep(
        repo, base=base, paths=paths, slice_prefix=run_prep_slice_prefix(run_key, base),
        plan_path="docs/plans/p.md", session_id=SESSION,
    )
    extra = [*paths, "coordinator_core/ops/unread.py"]
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=run_key, within=extra, covering=True) is None
    request = SimpleNamespace(chunks=[SimpleNamespace(paths=extra, prefixes=[])])
    assert "coordinator_core/ops/unread.py" in review_floor.floor_code_paths(request, {"prep": {"product_files": 0}}, False)
