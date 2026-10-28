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


def test_empty_paths_return_a_zero_file_reply_and_spawn_nothing(repo, monkeypatch):
    _spawn_trap(monkeypatch)
    result = _call(repo, [])
    assert result["error"] is None
    assert result["slices"] == []
    assert result["product_files"] == 0
    assert result["plan_id"] == "pln-test-1"
    assert result["run_base_sha"] == _base(repo)
    assert not (repo / result["share_dir"] / "prep-t.review-prep.md").exists()
    assert not (repo / result["share_dir"] / "prep-t.review-prep.lock").exists()


def test_cross_repo_path_is_excluded_from_the_freeze_not_fatal(repo):
    _write(repo, ["a/b/one.py"], 3)
    result = _call(repo, ["a/b/one.py", "../coordinator-content-repo/coordinator/workflows/plan-blitz.mjs"])
    assert result["error"] is None
    assert result["product_files"] == 1
    assert [s["files"] for s in result["slices"]] == [["a/b/one.py"]]


def test_only_cross_repo_paths_freeze_nothing_and_still_succeed(repo, monkeypatch):
    _spawn_trap(monkeypatch)
    result = _call(repo, ["../coordinator-content-repo/x.mjs", "a/../../y.py"])
    assert result["error"] is None
    assert result["slices"] == []


def test_dotdot_inside_the_repo_is_kept(repo):
    _write(repo, ["a/b/one.py"], 3)
    result = _call(repo, ["a/c/../b/one.py"])
    assert result["error"] is None
    assert result["product_files"] == 1


def test_empty_paths_with_a_group_return_no_slices(repo):
    result = _call(repo, [], group_index=0, group_count=2)
    assert result["error"] is None
    assert result["slices"] == []


def test_empty_slice_prefix_is_still_refused(repo):
    result = rp.review_prep(
        repo, base=_base(repo), paths=[], slice_prefix="", plan_path="docs/plans/p.md", session_id=SESSION
    )
    assert result["error"] == "base, paths, slice_prefix and plan_path are required"


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


def test_covering_keeps_a_reply_that_names_extra_paths_and_default_drops_it(repo):
    _write(repo, ["a/b/one.py", "a/b/two.py"], 3)
    made = _stored(repo, ["a/b/one.py", "a/b/two.py"])
    base = made["run_base_sha"]
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key="run1", within=["a/b/one.py"]) is None
    got = rp.stored_reply_for_run(repo, SESSION, base, run_key="run1", within=["a/b/one.py"], covering=True)
    assert got is not None and got["sidecar_path"] == made["sidecar_path"]
    assert rp.stored_reply_for_run(
        repo, SESSION, base, run_key="run1", within=["a/b/one.py", "a/b/zzz.py"], covering=True
    ) is None
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key="run1", covering=True) is not None


def test_stored_reply_for_run_returns_none_on_two_matches(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"], run="run1")
    _stored(repo, ["a/b/one.py"], run="run2")
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"]) is None
    assert rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1") is not None


def test_stored_reply_for_run_returns_none_without_a_share_dir(repo):
    assert rp.stored_reply_for_run(repo, "sess-none", _base(repo), run_key="run1") is None


def test_stored_reply_without_base_finds_the_one_ask_route_reply_by_run_key(repo):
    _write(repo, ["a/b/one.py"], 3)
    b = _base(repo)
    made = rp.review_prep(
        repo, base=b, paths=["a/b/one.py"], slice_prefix=f"manifest-run1-{b[:12]}-prep",
        plan_path="manifest.md", session_id=SESSION,
    )
    assert made["plan_id"] == "manifest"
    got = rp.stored_reply_for_run(repo, SESSION, None, run_key="run1")
    assert got is not None
    assert got["run_base_sha"] == b
    assert got["sidecar_path"] == made["sidecar_path"]


def test_stored_reply_without_base_returns_none_on_two_bases_for_one_run(repo):
    _write(repo, ["a/b/one.py"], 3)
    _stored(repo, ["a/b/one.py"], run="run1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    _stored(repo, ["a/b/one.py"], run="run1")
    assert rp.stored_reply_for_run(repo, SESSION, None, run_key="run1") is None


def test_stored_reply_without_base_and_run_key_returns_none(repo):
    _write(repo, ["a/b/one.py"], 3)
    _stored(repo, ["a/b/one.py"])
    assert rp.stored_reply_for_run(repo, SESSION, None) is None


def test_stored_reply_without_base_ignores_a_non_engine_agent_type(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"])
    sidecar = repo / made["sidecar_path"]
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace("engine:prep-freeze", "executor"), encoding="utf-8"
    )
    assert rp.stored_reply_for_run(repo, SESSION, None, run_key="run1") is None


def test_prep_block_unions_slice_files_in_order_and_copies_the_rest():
    reply = {
        "run_base_sha": "abc",
        "product_files": 3,
        "foreign_claims": ["x.py"],
        "slices": [{"files": ["b.py", "a.py"]}, "junk", {"files": ["a.py", "c.py"]}],
    }
    assert rp.prep_block(reply) == {
        "run_base_sha": "abc",
        "product_files": 3,
        "foreign_claims": ["x.py"],
        "slice_files": ["b.py", "a.py", "c.py"],
    }


def _sidecar_fm(repo: Path, reply: dict) -> dict:
    import yaml  # noqa: PLC0415

    text = (repo / reply["sidecar_path"]).read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def test_worktree_prep_records_reviewed_sha256_and_re_freezes_a_moved_file(repo):
    from coordinator_core.git.content_hash import eol_folded_sha256  # noqa: PLC0415

    _write(repo, ["a/one.py", "b/two.py"], 3)
    first = _stored(repo, ["a/one.py", "b/two.py"])
    expected = {
        rel: eol_folded_sha256((repo / rel).read_bytes()) for rel in ("a/one.py", "b/two.py")
    }
    assert first["reviewed_sha256"] == expected
    assert _sidecar_fm(repo, first)["reviewed_sha256"] == expected
    assert _stored(repo, ["a/one.py", "b/two.py"]) == first

    _write(repo, ["a/one.py"], 5)
    second = _stored(repo, ["a/one.py", "b/two.py"])
    assert second["error"] is None
    assert second["reviewed_sha256"]["a/one.py"] == eol_folded_sha256((repo / "a/one.py").read_bytes())
    assert second["reviewed_sha256"]["a/one.py"] != expected["a/one.py"]
    assert second["reviewed_sha256"]["b/two.py"] == expected["b/two.py"]
    assert list(Path(first["whole_diff_path"]).parent.glob("*.attempt1.diff"))


def test_unmoved_declared_paths_are_served_with_no_git_spawn_and_a_directory_entry_is_never_served(repo, monkeypatch):
    _write(repo, ["a/one.py", "b/two.py"], 3)
    first = _call(repo, ["a/one.py", "b/two.py", "never/written.py"])
    assert _sidecar_fm(repo, first)["declared_sha256"]["never/written.py"] is None
    _spawn_trap(monkeypatch)
    assert _call(repo, ["a/one.py", "b/two.py", "never/written.py"]) == first
    monkeypatch.undo()

    pathspecs = ["a/one.py", "b/"]
    made = _stored(repo, pathspecs, run="run2")
    assert _sidecar_fm(repo, made)["declared_sha256"] is None
    assert rp._stored_reply(repo, repo / made["sidecar_path"], _base(repo), pathspecs, None) is None


def test_a_worktree_sidecar_without_declared_sha256_is_recomputed_but_range_is_served(repo):
    import yaml  # noqa: PLC0415

    base = _base(repo)
    _write(repo, ["a/one.py"], 3)
    wt = rp.review_prep(
        repo, base=base, paths=["a/one.py"], slice_prefix="prep-w", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    head = _base(repo)
    rng = rp.review_prep(
        repo, base=base, paths=["a/one.py"], slice_prefix="prep-r", plan_path="docs/plans/p.md",
        session_id=SESSION, head=head,
    )
    for reply, key in ((wt, "declared_sha256"), (rng, "reviewed_sha256")):
        path = repo / reply["sidecar_path"]
        _, fm_text, rest = path.read_text(encoding="utf-8").split("---\n", 2)
        fm = yaml.safe_load(fm_text)
        fm.pop(key)
        path.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n{rest}", encoding="utf-8")
    served = rp._stored_reply(repo, repo / rng["sidecar_path"], base, ["a/one.py"], head)
    assert served is not None and "reviewed_sha256" not in served
    assert rp._stored_reply(repo, repo / wt["sidecar_path"], base, ["a/one.py"], None) is None


def test_stored_reply_for_run_returns_reviewed_sha256_and_recorded_at_and_tolerates_their_absence(repo):
    _write(repo, ["a/b/one.py"], 3)
    made = _stored(repo, ["a/b/one.py"])
    got = rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1")
    assert got["reviewed_sha256"] == made["reviewed_sha256"]
    assert got["recorded_at"].endswith("Z")

    import yaml  # noqa: PLC0415

    path = repo / made["sidecar_path"]
    _, fm_text, rest = path.read_text(encoding="utf-8").split("---\n", 2)
    fm = yaml.safe_load(fm_text)
    fm.pop("reviewed_sha256")
    fm.pop("recorded_at")
    path.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n{rest}", encoding="utf-8")
    old = rp.stored_reply_for_run(repo, SESSION, made["run_base_sha"], run_key="run1")
    assert old is not None and "reviewed_sha256" not in old and "recorded_at" not in old


def test_registered_in_every_op_set():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass  # noqa: PLC0415
    from coordinator_core.op_scopes import _OP_KEY_SCOPE  # noqa: PLC0415
    from coordinator_core.ops._registry_map import OP_MODULE_MAP  # noqa: PLC0415

    assert OP_CLASSIFICATION["review.prep"] is OpClass.MUTATING
    assert _OP_KEY_SCOPE["review.prep"] == "show_top"
    assert OP_MODULE_MAP["review.prep"] == "coordinator_core.ops.review_prep"


PEER = "sess-peer"
DELIVERABLE = "dlv-own"


def _plan_with_deliverable(repo: Path) -> None:
    (repo / "docs" / "plans" / "p.md").write_text(
        f"---\nplan_id: pln-test-1\ndeliverable_id: {DELIVERABLE}\n---\n\n# p\n", encoding="utf-8"
    )


def _commit_as(repo: Path, text: str, message: str, session: str, deliverable: str | None = None) -> str:
    (repo / "a.py").write_text(text, encoding="utf-8")
    trailers = f"Session-Id: {session}\n" + (f"Deliverable-Id: {deliverable}\n" if deliverable else "")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", f"{message}\n\n{trailers}")
    return _git(repo, "rev-parse", "HEAD").strip()


def _peer_scenario(repo: Path, peer_session: str, peer_deliverable: str | None) -> str:
    _plan_with_deliverable(repo)
    (repo / "a.py").write_text("v = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed a")
    base = _base(repo)
    _commit_as(repo, "v = 0\npeer = 1\n", "peer edit", peer_session, peer_deliverable)
    return base


def _prep(repo: Path, base: str, **kw) -> dict:
    return rp.review_prep(
        repo, base=base, paths=["a.py"], slice_prefix=f"p-run-{base[:12]}-prep",
        plan_path="docs/plans/p.md", session_id=SESSION, **kw,
    )


@pytest.mark.parametrize(
    "peer_session,peer_deliverable", [(PEER, None), (SESSION, "dlv-sibling")]
)
def test_worktree_whole_diff_drops_the_peer_hunk_and_keeps_the_run_own(repo, peer_session, peer_deliverable):
    base = _peer_scenario(repo, peer_session, peer_deliverable)
    (repo / "a.py").write_text("v = 0\npeer = 1\nown = 1\n", encoding="utf-8")
    reply = _prep(repo, base)
    assert reply["error"] is None
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert "+own = 1" in diff and "+peer = 1" not in diff
    assert reply["run_base_sha"] == base
    assert set(reply) >= set(rp._STORED_KEYS) - {"sidecar_path"} and "peer_bases" not in reply


def test_range_mode_whole_run_prep_prefix_has_the_snapshot_review_reverify_looks_up(repo):
    from coordinator_core.ops import review_reverify as rr

    base = _peer_scenario(repo, PEER, None)
    head = _commit_as(repo, "v = 0\npeer = 1\nown = 1\n", "own edit", SESSION, DELIVERABLE)
    prefix = f"p-run-{base[:12]}-prep"
    for _ in range(2):
        reply = _prep(repo, base, head=head)
        assert reply["error"] is None
        manifest = repo / reply["pre_review_manifest"]
        assert manifest.parent.name == f"{prefix}.pre-review"
        assert rr._read_manifest(manifest.parent) is not None


def test_range_whole_diff_drops_the_peer_hunk_and_keeps_the_run_own(repo):
    base = _peer_scenario(repo, PEER, None)
    head = _commit_as(repo, "v = 0\npeer = 1\nown = 1\n", "own edit", SESSION, DELIVERABLE)
    reply = _prep(repo, base, head=head)
    assert reply["error"] is None
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert "+own = 1" in diff and "+peer = 1" not in diff


def test_a_declared_directory_takes_the_peer_base_for_its_peer_file_and_keeps_the_rest(repo):
    _plan_with_deliverable(repo)
    (repo / "d").mkdir()
    (repo / "d" / "x.py").write_text("x = 0\n", encoding="utf-8")
    (repo / "d" / "y.py").write_text("y = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed d")
    base = _base(repo)

    def commit(name: str, text: str, session: str, deliverable: str | None = None) -> str:
        (repo / "d" / name).write_text(text, encoding="utf-8")
        trailers = f"Session-Id: {session}\n" + (f"Deliverable-Id: {deliverable}\n" if deliverable else "")
        _git(repo, "add", "d")
        _git(repo, "commit", "-q", "-m", f"edit {name}\n\n{trailers}")
        return _git(repo, "rev-parse", "HEAD").strip()

    commit("x.py", "x = 0\npeer = 1\n", PEER)
    commit("x.py", "x = 0\npeer = 1\nown_x = 1\n", SESSION, DELIVERABLE)
    head = commit("y.py", "y = 0\nown_y = 1\n", SESSION, DELIVERABLE)
    reply = rp.review_prep(
        repo, base=base, paths=["d"], slice_prefix="p-dir-prep",
        plan_path="docs/plans/p.md", session_id=SESSION, head=head,
    )
    assert reply["error"] is None
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert "+own_x = 1" in diff and "+own_y = 1" in diff
    assert "+peer = 1" not in diff


def test_a_peer_commit_after_the_run_own_commit_keeps_the_run_base(repo):
    _plan_with_deliverable(repo)
    (repo / "a.py").write_text("v = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed a")
    base = _base(repo)
    _commit_as(repo, "v = 0\nown = 1\n", "own edit", SESSION, DELIVERABLE)
    head = _commit_as(repo, "v = 0\nown = 1\npeer = 1\n", "peer edit", PEER)
    diff = Path(_prep(repo, base, head=head)["whole_diff_path"]).read_text(encoding="utf-8")
    assert "+own = 1" in diff and "+peer = 1" in diff


def test_partition_receives_the_peer_base_map(repo, monkeypatch):
    base = _peer_scenario(repo, PEER, None)
    (repo / "a.py").write_text("v = 0\npeer = 1\nown = 1\n", encoding="utf-8")
    seen = {}
    real = rp._heavy("partition_slices")

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(rp, "partition_slices", spy)
    _prep(repo, base)
    assert list(seen["path_bases"]) == ["a.py"] and seen["path_bases"]["a.py"] != base


def test_a_stored_reply_is_refused_after_a_peer_commit_that_leaves_the_bytes_unmoved(repo):
    base = _peer_scenario(repo, PEER, None)
    own_text = "v = 0\npeer = 1\nown = 1\n"
    (repo / "a.py").write_text(own_text, encoding="utf-8")
    first = _prep(repo, base)
    assert "+own = 1" in Path(first["whole_diff_path"]).read_text(encoding="utf-8")
    assert _sidecar_fm(repo, first)["peer_bases"] != {}
    peer_sha = _commit_as(repo, own_text, "peer absorbs", PEER)
    second = _prep(repo, base)
    assert second["error"] is None
    assert _sidecar_fm(repo, second)["peer_bases"] == {"a.py": peer_sha}
    assert "+own = 1" not in Path(second["whole_diff_path"]).read_text(encoding="utf-8")


def test_a_served_reply_costs_exactly_one_peer_log_spawn(repo, monkeypatch):
    from coordinator_core.ops import review_peer_bases as rpb  # noqa: PLC0415

    base = _peer_scenario(repo, PEER, None)
    (repo / "a.py").write_text("v = 0\npeer = 1\nown = 1\n", encoding="utf-8")
    first = _prep(repo, base)
    calls: list = []
    real = rpb.run_git

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(rpb, "run_git", counting)
    monkeypatch.setattr(rp, "freeze_diff", lambda *a, **k: pytest.fail("froze"))
    assert _prep(repo, base) == first
    assert len(calls) == 1


def test_range_mode_untracked_and_uncommitted_paths_get_a_head_fallback_slice(repo):
    base = _base(repo)
    _write(repo, ["a/new.py"], 3)
    result = rp.review_prep(
        repo, base=base, paths=["a/new.py"], slice_prefix="prep-fb", plan_path="docs/plans/p.md",
        session_id=SESSION, head=base,
    )
    assert result["error"] is None
    assert [f for s in result["slices"] for f in s["files"]] == ["a/new.py"]
    assert result["excluded"] == []
    assert "+v = 1" in Path(result["whole_diff_path"]).read_text(encoding="utf-8")


def test_a_declared_path_with_no_delta_is_named_excluded(repo):
    _write(repo, ["a/mine.py"], 3)
    result = _call(repo, ["a/mine.py", "seed.py"])
    assert [f for s in result["slices"] for f in s["files"]] == ["a/mine.py"]
    assert result["excluded"] == [{"path": "seed.py", "reason": "no-delta"}]
    fm = _sidecar_fm(repo, result)
    assert fm["excluded"] == result["excluded"]


def test_an_outside_repo_path_is_named_excluded_and_zero_paths_still_reply(repo):
    result = _call(repo, ["../x.py"])
    assert result["slices"] == [] and result["error"] is None
    assert result["excluded"] == [{"path": "../x.py", "reason": "outside-repo"}]


def _peer_holder() -> dict:
    from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun  # noqa: PLC0415

    return {"peer": LiveRun(plan="other.md", superseded_by="", successor_landed=False,
                            withheld=frozenset({"a/theirs.py"}), claims=frozenset())}


def test_review_prep_keeps_a_path_a_halted_peer_withholds(repo):
    _write(repo, ["a/mine.py", "a/theirs.py"], 3)
    result = _call(repo, ["a/mine.py", "a/theirs.py"], run_id="me", live_runs=_peer_holder())
    assert result["error"] is None
    assert [f for s in result["slices"] for f in s["files"]] == ["a/mine.py", "a/theirs.py"]
    assert result["peer_held"] == ["a/theirs.py"]
    assert result["excluded"] == []


@pytest.mark.parametrize("ranged", [False, True], ids=["worktree", "range"])
def test_a_run_whose_only_path_is_peer_withheld_gets_one_slice(repo, ranged):
    _write(repo, ["a/theirs.py"], 3)
    mode = {"head": _base(repo)} if ranged else {}
    result = _call(repo, ["a/theirs.py"], run_id="me", live_runs=_peer_holder(), **mode)
    assert result["error"] is None
    assert [f for s in result["slices"] for f in s["files"]] == ["a/theirs.py"]
    assert result["peer_held"] == ["a/theirs.py"]
