"""review.prep: one in-process freeze + partition + foreign-claims call returning review-prep-result."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _write(repo: Path, rels: list[str], lines: int) -> None:
    for rel in rels:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v = 1\n" * lines, encoding="utf-8")


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _call(repo: Path, paths: list[str], **kw) -> dict:
    return rp.review_prep(
        repo, base=_base(repo), paths=paths, slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, **kw,
    )


def test_one_product_file_is_one_whole_diff_slice_and_writes_the_sidecar(repo):
    _write(repo, ["a/b/one.py"], 3)
    started = time.process_time()
    result = _call(repo, ["a/b/one.py"])
    cpu_s = time.process_time() - started
    assert result["error"] is None
    assert result["verdict"] == "single-reviewer-ok"
    assert result["plan_id"] == "pln-test-1"
    assert result["basis"] == "worktree"
    assert result["product_files"] == 1
    assert result["foreign_claims"] == []
    assert Path(result["whole_diff_path"]).read_text(encoding="utf-8").startswith("diff --git")
    (slice_,) = result["slices"]
    assert slice_["files"] == ["a/b/one.py"]
    assert slice_["diff_path"] == result["whole_diff_path"]
    assert slice_["sidecar_path"] == result["whole_diff_sidecars"]["personas"][0]
    sidecar = (repo / result["sidecar_path"]).read_text(encoding="utf-8")
    assert sidecar.startswith("---\n") and "product_files: 1" in sidecar and "plan_id: pln-test-1" in sidecar
    assert cpu_s < 0.5


def test_partition_mandatory_returns_one_slice_per_frozen_group(repo):
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    result = _call(repo, rels)
    assert result["error"] is None
    assert result["verdict"] == "PARTITION-MANDATORY"
    assert 1 < len(result["slices"]) <= 6
    assert sorted(f for s in result["slices"] for f in s["files"]) == sorted(rels)
    for s in result["slices"]:
        assert s["id"].startswith("prep-t-")
        assert Path(s["diff_path"]).read_text(encoding="utf-8").startswith("diff --git")
        assert s["diff_path"] != result["whole_diff_path"]


def test_zero_product_files_returns_no_slices(repo):
    _write(repo, ["state/note.md"], 3)
    result = _call(repo, ["state/note.md"])
    assert result["error"] is None
    assert result["product_files"] == 0
    assert result["slices"] == []


def test_a_freeze_refusal_is_returned_as_error_with_no_slices(repo):
    _write(repo, ["a.py"], 3)
    result = rp.review_prep(
        repo, base="deadbeef", paths=["a.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )
    assert result["error"]
    assert result["slices"] == []


def test_range_mode_refusal_passes_through(repo):
    _write(repo, ["a.py"], 3)
    result = _call(repo, ["a.py"], head="no-such-ref-xyz")
    assert result["error"]
    assert result["slices"] == []


def test_handler_requires_exactly_one_of_head_and_worktree(repo):
    common = {"base": _base(repo), "paths": ["seed.py"], "slice_prefix": "p", "plan_path": "docs/plans/p.md"}
    assert "exactly one" in rp._handler({**common, "head": "x", "worktree": True}, repo)["error"]
    assert "exactly one" in rp._handler(dict(common), repo)["error"]
    assert rp._handler(dict(common, worktree=True), None)["error"]


def _landed_commit(repo: Path, rel: str) -> str:
    base = _base(repo)
    _write(repo, [rel], 3)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "peer")
    return base


def test_foreign_claims_keep_only_paths_a_live_other_session_holds(repo, monkeypatch):
    base = _landed_commit(repo, "pkg/held.py")
    _write(repo, ["pkg/own.py"], 3)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "own")
    claims = {"pkg/held.py": ["sess-peer"], "pkg/own.py": [SESSION]}
    monkeypatch.setattr(rp.claim_index, "lookup", lambda paths, **kw: {p: claims.get(p, []) for p in paths})
    found = rp.foreign_claims(repo, f"{base}..HEAD", ["pkg"], SESSION)
    assert found == ["pkg/held.py"]


def test_unanswerable_claim_is_not_foreign_and_empty_paths_spawn_nothing(repo, monkeypatch):
    base = _landed_commit(repo, "pkg/x.py")
    monkeypatch.setattr(
        rp.claim_index, "lookup", lambda paths, **kw: {p: [rp.claim_index.UNANSWERABLE] for p in paths}
    )
    assert rp.foreign_claims(repo, f"{base}..HEAD", ["pkg"], SESSION) == []

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rp, "run_git", boom)
    assert rp.foreign_claims(repo, f"{base}..HEAD", [], SESSION) == []


def _spawn_trap(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rp, "run_git", boom)
    monkeypatch.setattr(rp, "freeze_diff", boom)
    monkeypatch.setattr(rp, "partition_slices", boom)


def test_second_call_with_the_same_prefix_reads_the_store_and_spawns_nothing(repo, monkeypatch):
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    first = _call(repo, rels)
    _spawn_trap(monkeypatch)
    second = _call(repo, rels)
    assert second == first
    assert second["slices"] and second["error"] is None


def test_a_call_for_other_paths_is_not_served_from_the_store(repo, monkeypatch):
    _write(repo, ["a/one.py", "b/two.py"], 3)
    _call(repo, ["a/one.py"])
    _spawn_trap(monkeypatch)
    with pytest.raises(AssertionError, match="spawned"):
        _call(repo, ["a/one.py", "b/two.py"])


def test_a_refusal_is_not_stored(repo):
    _write(repo, ["a.py"], 3)
    bad = rp.review_prep(
        repo, base="deadbeef", paths=["a.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )
    assert bad["error"]
    assert not list((repo / ".coordinator-local").rglob("prep-t.review-prep.md"))
    assert _call(repo, ["a.py"])["error"] is None


def test_concurrent_first_callers_freeze_once(repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    calls = []
    real = rp.freeze_diff

    def counting(*a, **k):
        calls.append(1)
        time.sleep(0.2)
        return real(*a, **k)

    monkeypatch.setattr(rp, "freeze_diff", counting)
    with ThreadPoolExecutor(4) as pool:
        replies = list(pool.map(lambda _: _call(repo, rels), range(4)))
    assert len(calls) == 1
    winners = [r for r in replies if r["error"] is None]
    assert winners and all(r == winners[0] for r in winners)
    assert all(r["error"] is None or r["error"].startswith(rp.REVIEW_PREP_BUSY_PREFIX) for r in replies)
    assert not list((repo / ".coordinator-local").rglob("*.review-prep.lock"))
    assert _call(repo, rels) == winners[0]


def _lock_path(repo: Path) -> Path:
    return Path(rp.share_dir(str(repo), SESSION)) / "prep-t.review-prep.lock"


def _make_lock(repo: Path, age_secs: float = 0.0) -> Path:
    lock = _lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")
    if age_secs:
        then = time.time() - age_secs
        os.utime(lock, (then, then))
    return lock


def _count_freezes(monkeypatch) -> list:
    calls: list = []
    real = rp.freeze_diff

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(rp, "freeze_diff", counting)
    return calls


def test_a_loser_on_a_fresh_lock_returns_busy_without_computing(repo, monkeypatch):
    _write(repo, ["a.py"], 3)
    monkeypatch.setattr(rp, "_LOCK_WAIT_SECS", 0.05)
    lock = _make_lock(repo)
    calls = _count_freezes(monkeypatch)
    reply = _call(repo, ["a.py"])
    assert reply["error"].startswith(rp.REVIEW_PREP_BUSY_PREFIX)
    assert calls == []
    assert lock.exists()


def test_a_stale_lock_is_reclaimed_and_computed_once(repo, monkeypatch):
    _write(repo, ["a.py"], 3)
    _make_lock(repo, age_secs=rp._LOCK_STALE_SECS + 5)
    calls = _count_freezes(monkeypatch)
    reply = _call(repo, ["a.py"])
    assert reply["error"] is None
    assert len(calls) == 1
    assert list((repo / ".coordinator-local").rglob("prep-t.review-prep.md"))
    assert not _lock_path(repo).exists()


def test_a_loser_whose_winner_unlinked_the_lock_reclaims_and_computes_once(repo, monkeypatch):
    _write(repo, ["a.py"], 3)
    lock = _make_lock(repo)
    monkeypatch.setattr(rp, "_LOCK_POLL_SECS", 0.0)
    real_sleep = time.sleep

    def winner_leaves(secs):
        lock.unlink(missing_ok=True)
        real_sleep(0)

    monkeypatch.setattr(rp.time, "sleep", winner_leaves)
    calls = _count_freezes(monkeypatch)
    reply = _call(repo, ["a.py"])
    assert reply["error"] is None
    assert len(calls) == 1
    assert not lock.exists()


def test_group_index_selects_the_balanced_contiguous_group(repo):
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    whole = _call(repo, rels)
    ids = [s["id"] for s in whole["slices"]]
    n = len(ids)
    assert n > 2
    groups = [_call(repo, rels, group_index=i, group_count=2) for i in range(2)]
    assert [s["id"] for s in groups[0]["slices"]] == ids[: (n + 1) // 2]
    assert [s["id"] for s in groups[1]["slices"]] == ids[(n + 1) // 2 :]
    for g in groups:
        assert {k: v for k, v in g.items() if k != "slices"} == {k: v for k, v in whole.items() if k != "slices"}
    assert _call(repo, rels, group_index=0, group_count=1)["slices"] == whole["slices"]
    assert _call(repo, rels, group_index=n + 2, group_count=n + 3)["slices"] == []
    assert len(_call(repo, rels, group_index=n - 1, group_count=99)["slices"]) == 1


def test_group_params_go_together_and_stay_in_range(repo):
    _write(repo, ["a.py"], 3)
    assert "go together" in _call(repo, ["a.py"], group_index=0)["error"]
    assert _call(repo, ["a.py"], group_index=2, group_count=2)["error"]
    assert _call(repo, ["a.py"], group_index=0, group_count=0)["error"]
    common = {"base": _base(repo), "paths": ["a.py"], "slice_prefix": "p", "plan_path": "docs/plans/p.md", "worktree": True}
    assert "integers" in rp._handler(dict(common, group_index="0", group_count=1), repo)["error"]


def test_group_slices_split_matches_the_js_helper():
    items = [{"id": i} for i in range(5)]
    assert [[s["id"] for s in g] for g in rp.group_slices(items, 2)] == [[0, 1, 2], [3, 4]]
    assert [[s["id"] for s in g] for g in rp.group_slices(items, 99)] == [[0], [1], [2], [3], [4]]
    assert rp.group_slices([], 3) == []


def _stored(repo: Path, paths: list[str], run: str = "run1", base: str | None = None) -> dict:
    """Run review.prep for `paths` under the prefix shape execute_review.prep_slice_id_for makes."""
    b = base or _base(repo)
    return rp.review_prep(
        repo, base=b, paths=paths, slice_prefix=f"p-{run}-{b[:12]}-prep",
        plan_path="docs/plans/p.md", session_id=SESSION,
    )


def test_stored_reply_for_run_returns_the_one_matching_reply(repo):
    _write(repo, ["a/b/one.py", "a/b/two.py"], 3)
    made = _stored(repo, ["a/b/one.py", "a/b/two.py"])
    got = rp.stored_reply_for_run(
        repo, SESSION, made["run_base_sha"], run_key="run1", within=["a/b/one.py", "a/b/two.py"]
    )
    assert got is not None
    assert got["product_files"] == 2
    assert sorted(f for s in got["slices"] for f in s["files"]) == ["a/b/one.py", "a/b/two.py"]
    assert got["sidecar_path"] == made["sidecar_path"]
    assert not Path(got["sidecar_path"]).is_absolute() and "\\" not in got["sidecar_path"]


def test_stored_reply_for_run_ignores_another_runs_sidecar(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"], run="other")
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1") is None


def test_stored_reply_for_run_ignores_a_different_base(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"])
    other = made["run_base_sha"][:12] + "0" * 28
    assert rp.stored_reply_for_run(repo, SESSION, other, run_key="run1") is None


def test_stored_reply_for_run_ignores_paths_outside_within(repo):
    _write(repo, ["a/b/one.py", "a/b/two.py"], 3)
    made = _stored(repo, ["a/b/one.py", "a/b/two.py"])
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], within=["a/b/one.py"]) is None
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"]) is not None


def test_stored_reply_for_run_returns_none_on_two_matches(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"], run="run1")
    _stored(repo, ["a/b/one.py"], run="run2")
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"]) is None
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1") is not None


def test_stored_reply_for_run_returns_none_without_a_share_dir(repo):
    assert rp.stored_reply_for_run(repo, "sess-none", _base(repo), run_key="run1") is None


def test_registered_in_every_op_set():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass  # noqa: PLC0415
    from coordinator_core.op_scopes import _OP_KEY_SCOPE  # noqa: PLC0415
    from coordinator_core.ops._registry_map import OP_MODULE_MAP  # noqa: PLC0415

    assert OP_CLASSIFICATION["review.prep"] is OpClass.MUTATING
    assert _OP_KEY_SCOPE["review.prep"] == "show_top"
    assert OP_MODULE_MAP["review.prep"] == "coordinator_core.ops.review_prep"
