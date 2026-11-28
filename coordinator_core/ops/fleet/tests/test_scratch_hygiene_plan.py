"""Pins the read-only purge planner against the shared two-root fixture."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet import scratch_hygiene_plan as plan
from coordinator_core.ops.fleet.tests._scratch_hygiene_fixture import build_two_root_fixture


def _backdate(path: Path) -> None:
    """Backdate ``path`` and its subtree, never touching a link (utime would follow it on Windows)."""
    stamp = time.time() - 10 * 24 * 3600
    stack = [path]
    while stack:
        p = stack.pop()
        if os.path.islink(p) or is_junction(p):
            continue
        os.utime(p, (stamp, stamp))
        if os.path.isdir(p):
            stack.extend(p / n for n in os.listdir(p))


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = build_two_root_fixture(tmp_path, monkeypatch)
    # The shared fixture's age() swallows NotImplementedError from utime(follow_symlinks=False),
    # which Windows raises, leaving every entry young; re-age here.
    for p, action in f.expected.items():
        if action != "skipped-young" and not (os.path.islink(p) or is_junction(p)):
            _backdate(p)
    _backdate(f.temp_root / "pytest")
    yield f
    f.close()


def _plan(fx, **kw):
    return plan.plan_purge(
        fx.repo_root, registry_dir=fx.registry_dir, verify_copy=fx.verify_copy, **kw
    )


def _by_path(fx, records):
    return {os.path.normcase(str(fx.repo_root / r["path"])): r for r in records}


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                digest = ""
                if os.path.isfile(p) and not os.path.islink(p):
                    digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
                out[p] = (st.st_mtime_ns, st.st_size, digest)
    return out


def test_every_entry_classified_as_expected(fx) -> None:
    got = _by_path(fx, _plan(fx))
    assert set(got) == {os.path.normcase(str(p)) for p in fx.expected}
    for path, action in fx.expected.items():
        rec = got[os.path.normcase(str(path))]
        assert rec["action"] == action, (path, rec)
        want = fx.expected_reason.get(path)
        if want:
            assert rec["reason"] == want


def test_would_delete_set_and_skips_present(fx) -> None:
    actions = {r["action"] for r in _plan(fx)}
    assert {"would-delete", "skipped-link", "skipped-live", "skipped-young", "skipped-unverified"} <= actions


def test_pytest_dir_is_not_an_entry_but_its_runs_are(fx) -> None:
    paths = [r["path"] for r in _plan(fx)]
    assert not any(p.endswith("/pytest") for p in paths)
    assert any(p.endswith("pytest/run-old") for p in paths)


def test_planning_deletes_nothing(fx) -> None:
    before = _snapshot(fx.scratch, fx.temp_root, fx.outside, fx.hold)
    _plan(fx)
    assert _snapshot(fx.scratch, fx.temp_root, fx.outside, fx.hold) == before


def test_planning_process_time_under_half_second(fx) -> None:
    start = time.process_time()
    _plan(fx)
    assert time.process_time() - start < 0.5


def test_missing_roots_are_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    assert plan.plan_purge(tmp_path / "bare-repo", registry_dir=tmp_path / "none") == []


def test_young_window_is_configurable(fx) -> None:
    old = fx.scratch / "old-plain"
    rec = plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir, quiescence_hours=24 * 30)
    assert rec["action"] == "skipped-young"
    rec = plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)
    assert rec["action"] == "would-delete"
    assert rec["files"] == 2 and rec["bytes"] == 6 and rec["age_days"] >= 9


def test_classify_entry_rechecks_current_disk_state(fx) -> None:
    old = fx.scratch / "old-plain"
    assert plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)["action"] == "would-delete"
    (old / "late.txt").write_bytes(b"new")
    assert plan.classify_entry(old, repo_root=fx.repo_root, registry_dir=fx.registry_dir)["action"] == "skipped-young"


def test_matching_copy_is_not_unverified(fx) -> None:
    src = fx.scratch / "copy-source"
    (Path(fx.copy_dest) / "two.bin").write_bytes(b"abcd")
    rec = plan.classify_entry(
        src, repo_root=fx.repo_root, registry_dir=fx.registry_dir, verify_copy=fx.verify_copy
    )
    assert rec["action"] == "would-delete"


def test_top_level_file_entry(fx) -> None:
    f = fx.scratch / "loose.log"
    f.write_bytes(b"12345")
    _backdate(f)
    rec = plan.classify_entry(f, repo_root=fx.repo_root, registry_dir=fx.registry_dir)
    assert (rec["action"], rec["files"], rec["bytes"]) == ("would-delete", 1, 5)


def test_nested_link_is_not_traversed(fx) -> None:
    rec = plan.classify_entry(
        fx.scratch / "old-with-nested-link", repo_root=fx.repo_root, registry_dir=fx.registry_dir
    )
    assert rec["action"] == "would-delete" and rec["files"] == 1


def test_no_subprocess_spawned(fx, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def boom(*a, **k):
        raise AssertionError("planner spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _plan(fx)


# --- open-run skip -----------------------------------------------------------------------

_FUTURE = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
_PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    return tmp_path


def _run(repo: Path, name: str, days: float = 10, record=None, raw: str | None = None) -> list[Path]:
    warp = repo / "scratch" / "warp"
    d = warp / name
    d.mkdir()
    (d / "f.txt").write_text("x")
    paths = [d, warp / f"{name}.workflow.mjs", warp / f"{name}.workflow.mjs.emitted.json"]
    for p in paths[1:]:
        p.write_text("x")
    for p in paths:
        _backdate_to(p, days)
    if record is not None or raw is not None:
        rec = repo / ".coordinator-local" / "close-jobs" / f"{name}.workflow.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(raw if raw is not None else json.dumps(record))
    return paths


def _backdate_to(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    stack = [path]
    while stack:
        p = stack.pop()
        os.utime(p, (stamp, stamp))
        if p.is_dir():
            stack.extend(p / n for n in os.listdir(p))


def _classify(repo: Path, p: Path) -> dict:
    return plan.classify_entry(p, repo_root=repo, registry_dir=repo / "no-registry")


def _record(deadline: str, *statuses: str) -> dict:
    return {"deadline": deadline, "legs": {f"leg{i}": {"status": s} for i, s in enumerate(statuses)}}


def test_warp_is_not_an_entry_but_its_children_are(repo) -> None:
    paths = _run(repo, "a")
    got = plan.purge_candidates(repo)
    assert str(repo / "scratch" / "warp") not in got
    assert all(str(p) in got for p in paths)


def test_queued_leg_inside_deadline_holds_the_run(repo) -> None:
    for p in _run(repo, "a", record=_record(_FUTURE, "queued", "ran")):
        r = _classify(repo, p)
        assert r["action"] == "skipped-live"
        assert r["reason"] == "open-run-close-job:a.workflow"


@pytest.mark.parametrize("deadline", [_PAST, "not-a-date"], ids=["past", "unparseable"])
def test_queued_leg_past_or_unparseable_deadline_is_finished(repo, deadline) -> None:
    for p in _run(repo, "a", record=_record(deadline, "queued")):
        assert _classify(repo, p)["action"] == "would-delete"


def test_every_leg_with_an_outcome_is_finished_even_failed(repo) -> None:
    for p in _run(repo, "a", record=_record(_FUTURE, "ran", "failed")):
        assert _classify(repo, p)["action"] == "would-delete"


def test_no_record_two_days_old_is_held(repo) -> None:
    for p in _run(repo, "a", days=2):
        r = _classify(repo, p)
        assert r["action"] == "skipped-live"
        assert r["reason"] == "open-run-no-close-job:a"


def test_no_record_eight_days_old_is_deleted(repo) -> None:
    for p in _run(repo, "a", days=8):
        assert _classify(repo, p)["action"] == "would-delete"


@pytest.mark.parametrize("kw", [{"raw": "{not json"}, {"record": {"deadline": _PAST}}, {"record": [1]}])
def test_invalid_record_is_an_open_run(repo, kw) -> None:
    for p in _run(repo, "a", **kw):
        r = _classify(repo, p)
        assert r["action"] == "skipped-live"
        assert r["reason"] == "open-run-close-job:a.workflow"


def test_stray_file_under_warp_follows_the_no_record_rule(repo) -> None:
    stray = repo / "scratch" / "warp" / "stray.txt"
    stray.write_text("x")
    _backdate_to(stray, 2)
    r = _classify(repo, stray)
    assert (r["action"], r["reason"]) == ("skipped-live", "open-run-no-close-job:stray.txt")
    _backdate_to(stray, 8)
    assert _classify(repo, stray)["action"] == "would-delete"


def test_apply_keeps_open_runs_and_removes_finished(repo) -> None:
    from coordinator_core.ops.fleet.scratch_hygiene import run_hygiene

    queued = _run(repo, "q", record=_record(_FUTURE, "queued"))
    fresh = _run(repo, "n", days=2)
    done = _run(repo, "d", record=_record(_FUTURE, "ran"))
    run_hygiene({"repo_root": str(repo), "cadence": "workday_complete", "apply": True})
    assert all(p.exists() for p in queued + fresh)
    assert not any(p.exists() for p in done)


def test_non_warp_entry_is_classified_as_before(repo) -> None:
    f = repo / "scratch" / "old.txt"
    f.write_text("x")
    _backdate_to(f, 10)
    assert _classify(repo, f)["action"] == "would-delete"
    _backdate_to(f, 0.5)
    assert _classify(repo, f)["action"] == "skipped-young"


def _plan_actions(repo: Path) -> dict[str, str]:
    recs = plan.plan_purge(repo, registry_dir=repo / "no-registry")
    return {r["path"]: r["action"] for r in recs}


def test_state_scratch_entries_are_planned(repo) -> None:
    old = repo / "state" / "scratch" / "old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("x")
    _backdate_to(old, 10)
    assert str(old) in plan.purge_candidates(repo)
    assert _classify(repo, old)["action"] == "would-delete"


def test_open_run_entries_are_skipped_live_with_state_scratch_enabled(repo) -> None:
    (repo / "state" / "scratch").mkdir(parents=True)
    paths = _run(repo, "q", record=_record(_FUTURE, "queued"))
    for p in paths:
        r = _classify(repo, p)
        assert (r["action"], r["reason"]) == ("skipped-live", "open-run-close-job:q.workflow")


def test_state_scratch_entry_named_like_a_run_is_not_an_open_run(repo) -> None:
    _run(repo, "q", record=_record(_FUTURE, "queued"))
    twin = repo / "state" / "scratch" / "q"
    twin.mkdir(parents=True)
    (twin / "f.txt").write_text("x")
    _backdate_to(twin, 10)
    r = _classify(repo, twin)
    assert r["action"] == "would-delete"


def test_queue_grind_evidence_is_never_planned(repo) -> None:
    ev = repo / "state" / "queue-grind" / "bug" / "evidence" / "r1"
    ev.mkdir(parents=True)
    (ev / "i1.done.md").write_text("x")
    (repo / "state" / "queue-grind" / "bug" / "ledger.jsonl").write_text("x")
    _backdate_to(repo / "state" / "queue-grind", 10)
    (repo / "state" / "scratch").mkdir(parents=True)
    assert not any("queue-grind" in p for p in _plan_actions(repo))


def _scratch_entry(repo: Path, name: str) -> Path:
    d = repo / "scratch" / name
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x")
    _backdate_to(d, 10)
    return d


def _plan_doc(repo: Path, stem: str, status: str, body: str = "", evidence: str = "") -> None:
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{stem}.md").write_text(f"---\nstatus: {status}\n---\n{body}\n")
    if evidence:
        (plans / f"{stem}.evidence.yaml").write_text(evidence)


@pytest.mark.parametrize("where", ["evidence", "body"])
def test_scratch_cited_by_a_non_terminal_plan_is_kept(repo, where):
    d = _scratch_entry(repo, "proof-logs")
    cite = "- path: scratch/proof-logs/c6.log\n"
    _plan_doc(repo, "p1", "executing", body=cite if where == "body" else "", evidence=cite if where == "evidence" else "")
    r = _classify(repo, d)
    assert r["action"] == "skipped-live" and r["reason"].startswith("cited-by-open-plan:p1")


@pytest.mark.parametrize("where", ["evidence", "body"])
def test_scratch_cited_only_by_a_terminal_plan_is_deleted(repo, where):
    d = _scratch_entry(repo, "proof-logs")
    cite = "- path: scratch/proof-logs/c6.log\n"
    _plan_doc(repo, "p1", "implemented", body=cite if where == "body" else "", evidence=cite if where == "evidence" else "")
    assert _classify(repo, d)["action"] == "would-delete"


def test_a_citation_of_a_longer_name_does_not_keep_a_prefix_entry(repo):
    d = _scratch_entry(repo, "proof")
    _plan_doc(repo, "p1", "executing", evidence="- path: scratch/proof-logs/c6.log\n")
    assert _classify(repo, d)["action"] == "would-delete"


def test_scratch_cited_by_a_non_terminal_plan_in_a_month_subdirectory_is_kept(repo):
    d = _scratch_entry(repo, "proof-logs")
    month = repo / "docs" / "plans" / "2026-11"
    month.mkdir(parents=True)
    (month / "p1.md").write_text('---\nstatus: "executing"\n---\n')
    (month / "p1.evidence.yaml").write_text("- path: scratch/proof-logs/c6.log\n")
    r = _classify(repo, d)
    assert r["action"] == "skipped-live" and r["reason"] == "cited-by-open-plan:p1.evidence.yaml"


# --- shared keep manifest ----------------------------------------------------------------


def _manifest(repo: Path, text: str) -> None:
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "scratch-keep.yaml").write_text(text, encoding="utf-8")


def _keep_row(path: str, until: str) -> str:
    return f"keep:\n  - path: {path}\n    keep_until: {until}\n    reason: evidence\n"


def _apply(repo: Path) -> dict:
    from coordinator_core.ops.fleet.scratch_hygiene import run_hygiene

    return run_hygiene({"repo_root": str(repo), "cadence": "workday_complete", "apply": True})


@pytest.mark.parametrize("path_form", ["scratch/kept-run", "scratch\\kept-run", "Scratch/Kept-Run/"])
def test_unexpired_manifest_entry_is_kept_under_the_apply_call(repo, path_form):
    kept = _scratch_entry(repo, "kept-run")
    other = _scratch_entry(repo, "other-run")
    until = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
    _manifest(repo, _keep_row(json.dumps(path_form), until))
    r = _classify(repo, kept)
    assert r["action"] == "skipped-live" and r["reason"].startswith("kept-by-manifest:state/scratch-keep.yaml")
    assert _classify(repo, other)["action"] == "would-delete"
    _apply(repo)
    assert kept.exists() and not other.exists()


def test_manifest_entry_is_kept_through_its_last_day(repo):
    kept = _scratch_entry(repo, "kept-run")
    today = datetime.now(timezone.utc).date().isoformat()
    _manifest(repo, _keep_row("scratch/kept-run", today))
    assert _classify(repo, kept)["action"] == "skipped-live"


def test_expired_manifest_entry_no_longer_protects(repo):
    d = _scratch_entry(repo, "kept-run")
    until = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    _manifest(repo, _keep_row("scratch/kept-run", until))
    assert _classify(repo, d)["action"] == "would-delete"
    _apply(repo)
    assert not d.exists()


def test_absent_manifest_is_an_empty_one(repo):
    d = _scratch_entry(repo, "kept-run")
    assert _classify(repo, d)["action"] == "would-delete"


@pytest.mark.parametrize(
    "text",
    [
        "keep: [unclosed\n  - : :\n",
        "keep: not-a-list\n",
        "- just\n- a list\n",
        "keep:\n  - path: scratch/kept-run\n    keep_until: not-a-date\n",
        "keep:\n  - path: scratch/kept-run\n",
        "keep:\n  - scratch/kept-run\n",
    ],
)
def test_unreadable_manifest_keeps_every_entry(repo, text):
    a = _scratch_entry(repo, "kept-run")
    b = _scratch_entry(repo, "other-run")
    _manifest(repo, text)
    for d in (a, b):
        r = _classify(repo, d)
        assert r["action"] == "skipped-live" and r["reason"].startswith("keep-manifest-unreadable")
    _apply(repo)
    assert a.exists() and b.exists()


def test_undecodable_manifest_keeps_every_entry(repo):
    d = _scratch_entry(repo, "kept-run")
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "scratch-keep.yaml").write_bytes(b"\xff\xfe\x00bad")
    assert _classify(repo, d)["reason"].startswith("keep-manifest-unreadable")


def test_manifest_is_read_once_per_plan(repo, monkeypatch):
    for n in ("a", "b", "c"):
        _scratch_entry(repo, n)
    reads: list[Path] = []
    real = plan._keep_manifest
    monkeypatch.setattr(plan, "_keep_manifest", lambda root: reads.append(root) or real(root))
    plan.plan_purge(repo, registry_dir=repo / "no-registry")
    assert len(reads) == 1


def _link_dir(link: Path, target: Path) -> None:
    from coordinator_core.install.junction import JunctionUnsupported, create_junction

    try:
        create_junction(link, target)
    except (OSError, JunctionUnsupported) as exc:
        pytest.skip(f"cannot create junction/symlink here: {exc}")


def _outside_with_entries(tmp_path: Path) -> Path:
    outside = tmp_path / "outside-target"
    (outside / "structural-store").mkdir(parents=True)
    (outside / "structural-store" / "structural.sqlite3").write_bytes(b"precious")
    (outside / "loose.txt").write_bytes(b"precious")
    _backdate(outside)
    return outside


def _hygiene(repo: Path, apply: bool = False) -> list[dict]:
    from coordinator_core.ops.fleet.scratch_hygiene import run_hygiene

    out = run_hygiene({"repo_root": str(repo), "cadence": "workday_start", "apply": apply})
    return [r for r in out["records"] if r.get("kind") == "purge"]


@pytest.mark.parametrize("rel", ["scratch", "state/scratch"])
@pytest.mark.parametrize("apply", [False, True], ids=["plan", "apply"])
def test_linked_candidate_root_lists_nothing_and_records_one_refusal(tmp_path: Path, monkeypatch, rel, apply) -> None:
    """POSIX symlink and Windows junction shape (create_junction picks per host)."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    outside = _outside_with_entries(tmp_path)
    _link_dir(repo / rel, outside)

    assert str(repo / rel) in plan.purge_candidates(repo)
    assert not any(str(outside) in p or "structural-store" in p for p in plan.purge_candidates(repo))
    records = _hygiene(repo, apply)
    assert len(records) == 1
    rec = records[0]
    assert rec["action"] == "skipped-link"
    assert rec["path"] == rel
    assert str(os.path.realpath(outside)) in rec["reason"]
    assert (outside / "structural-store" / "structural.sqlite3").read_bytes() == b"precious"
    assert (outside / "loose.txt").exists()


def test_candidate_root_escaping_via_a_linked_parent_is_refused(tmp_path: Path, monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside-state"
    (outside / "scratch" / "victim").mkdir(parents=True)
    _backdate(outside)
    _link_dir(repo / "state", outside)

    assert not os.path.islink(repo / "state" / "scratch")
    records = _hygiene(repo, apply=True)
    assert [r["action"] for r in records] == ["skipped-link"]
    assert "root-outside-anchor" in records[0]["reason"]
    assert (outside / "scratch" / "victim").is_dir()


def test_temp_leg_root_that_leaves_the_coordinator_temp_root_is_refused(tmp_path: Path, monkeypatch) -> None:
    import tempfile

    from coordinator_core.temp_layout import coordinator_temp_root

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = _outside_with_entries(tmp_path)
    temp_root = coordinator_temp_root(repo)
    temp_root.parent.mkdir(parents=True)
    _link_dir(temp_root, outside)

    records = _hygiene(repo, apply=True)
    assert [r["action"] for r in records] == ["skipped-link"]
    assert str(os.path.realpath(outside)) in records[0]["reason"]
    assert (outside / "loose.txt").exists()


def test_real_candidate_roots_are_still_listed(tmp_path: Path, monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    repo = tmp_path / "repo"
    (repo / "scratch" / "old").mkdir(parents=True)
    (repo / "state" / "scratch" / "old2").mkdir(parents=True)
    assert set(plan.purge_candidates(repo)) == {str(repo / "scratch" / "old"), str(repo / "state" / "scratch" / "old2")}


def test_repo_manifest_seeds_the_gipsy_entries() -> None:
    root = Path(plan.__file__).resolve().parents[3]
    # The manifest is box-local and untracked: a clean checkout or a published stamp has none.
    if not (root / plan.KEEP_MANIFEST_RELPATH).is_file():
        pytest.skip(f"{plan.KEEP_MANIFEST_RELPATH} is box-local and absent from this tree")
    rows = plan._keep_manifest(root)
    assert rows is not None
    for name in ("gipsy-w4-shard-9714b4-run", "gipsy-w5-machine-a-tail-run", "gipsy-w4-ext0078-run"):
        assert rows[f"scratch/{name}"][0].isoformat() == "2026-12-05"
