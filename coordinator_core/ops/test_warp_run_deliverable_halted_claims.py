"""Tests for warp_run_deliverable.halted_run_write_claims."""

from __future__ import annotations

import json
import time
from calendar import timegm

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

NOW = float(timegm((2026, 10, 19, 12, 0, 0, 0, 0, 0)))
HALTED_ID = "ask-20261019T100000-aaaaaa"
OTHER_ID = "ask-20261019T110000-bbbbbb"
OLD_ID = "ask-20261001T100000-cccccc"
JOB = "tools/runpod-jobs/job.py"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _record(repo, run_id, *, writes=(), withheld=(), status=STATUS_HALTED, manifest_text=None):
    rec = repo / "state/warp" / run_id
    rec.mkdir(parents=True)
    if manifest_text is None:
        manifest_text = json.dumps({"rows": [{"writes": list(writes)}], "doc_writes": []})
    (rec / "manifest.json").write_text(manifest_text, encoding="utf-8")
    if status is not None:
        marker = {"status": status}
        if withheld:
            marker["run_artifacts"] = {"withheld": {p: "peer-held" for p in withheld}}
        (rec / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    return rec


def _claims(repo, paths, **kw):
    kw.setdefault("exclude_run", None)
    return wrd.halted_run_write_claims(repo, paths, now=NOW, **kw)


def test_withheld_write_maps_to_run_id(repo):
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    assert _claims(repo, [JOB, "src/other.py"]) == {JOB: [HALTED_ID]}


def test_declared_but_not_withheld_path_is_not_held(repo):
    _record(repo, HALTED_ID, writes=[JOB, "src/other.py"], withheld=["src/other.py"])
    assert _claims(repo, [JOB, "src/other.py"]) == {"src/other.py": [HALTED_ID]}


def test_halt_without_any_withheld_holds_nothing(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_backslash_path_is_normalized(repo):
    _record(repo, HALTED_ID, withheld=["docs/a.md"])
    assert _claims(repo, ["docs\\a.md"]) == {"docs/a.md": [HALTED_ID]}


def test_two_halted_runs_both_named(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    _record(repo, OTHER_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {JOB: sorted([HALTED_ID, OTHER_ID])}


def test_resumed_marker_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB], status="resumed")
    assert _claims(repo, [JOB]) == {}


def test_no_marker_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB], status=None)
    assert _claims(repo, [JOB]) == {}


def test_out_of_window_stamp_yields_empty(repo):
    _record(repo, OLD_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_exclude_run_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    assert _claims(repo, [JOB], exclude_run=HALTED_ID) == {}


def test_unreadable_manifest_does_not_hide_a_withheld_hold(repo):
    _record(repo, HALTED_ID, withheld=[JOB], manifest_text="{not json")
    assert _claims(repo, [JOB]) == {JOB: [HALTED_ID]}


def test_unstamped_name_skipped_without_read(repo, monkeypatch):
    _record(repo, "no-stamp-here", withheld=[JOB])

    def boom(*a, **k):
        raise AssertionError("read an unstamped record dir")

    monkeypatch.setattr(wrd, "_halted", boom)
    assert _claims(repo, [JOB]) == {}


SIZING = "state/sizings/s.yaml"
PLAN = "docs/plans/p.md"


def _bind(repo, run_id, *, sizing=SIZING, plan=None, marker=STATUS_HALTED):
    rec = repo / "state/warp" / run_id
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    body = {"status": marker, "sizing_rel": sizing}
    try:
        body.update(run_artifacts=json.loads((rec / halted_run.HALTED_FILE).read_text(encoding="utf-8"))["run_artifacts"])
    except (OSError, KeyError, ValueError):
        pass
    if plan:
        body["plan"] = plan
    (rec / halted_run.HALTED_FILE).write_text(json.dumps(body), encoding="utf-8")


def _sizing(repo, rel, status="open", deliverable=""):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"status: {status}\n" + (f"deliverable_id: {deliverable}\n" if deliverable else ""), encoding="utf-8")


def test_superseded_sizing_halted_run_claims_nothing(repo):
    _sizing(repo, SIZING, "superseded")
    _record(repo, HALTED_ID, writes=[JOB])
    _bind(repo, HALTED_ID)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}


def test_same_sizing_successor_releases_and_unrelated_halted_still_holds(repo):
    _sizing(repo, SIZING)
    _sizing(repo, "state/sizings/u.yaml")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID)
    _bind(repo, OTHER_ID)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}
    _bind(repo, OTHER_ID, sizing="state/sizings/u.yaml")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {JOB: [HALTED_ID]}


def test_same_deliverable_successor_releases(repo):
    _sizing(repo, SIZING, deliverable="dlv-x")
    _sizing(repo, "state/sizings/t.yaml", deliverable="dlv-x")
    _record(repo, HALTED_ID, writes=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID)
    _bind(repo, OTHER_ID, sizing="state/sizings/t.yaml")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}


def test_own_plan_landing_releases_and_other_plan_holds(repo):
    _sizing(repo, SIZING)
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID, plan=PLAN)
    assert _claims(repo, [JOB], exclude_run=None, own_plan=PLAN) == {}
    assert _claims(repo, [JOB], exclude_run=None, own_plan="docs/plans/q.md") == {JOB: [HALTED_ID]}
    assert _claims(repo, [JOB], exclude_run=None) == {JOB: [HALTED_ID]}


def test_no_candidates_or_no_state_dir(repo):
    assert _claims(repo, []) == {}
    assert _claims(repo, [JOB]) == {}


def _baselined(repo, run_id, baseline, *, halted_at=None, started_at=None):
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    rec = repo / "state/warp" / run_id
    manifest = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    manifest["write_baseline"] = baseline
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    stage = {}
    if halted_at:
        stage.update(stage="halted", last_activity_at=halted_at)
    if started_at:
        stage["started_at"] = started_at
    (rec / RUN_STAGE_FILE).write_text(json.dumps(stage), encoding="utf-8")


def _dirty_job(repo, text="landing run edit\n"):
    path = repo / JOB
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _blob(text):
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    return blob_sha(text.encode("utf-8"))


HALT_AT = "2026-10-19T10:30:00Z"
START_AT = "2026-10-19T11:00:00Z"


def _two_runs(repo, b_h, b_l):
    _record(repo, HALTED_ID, writes=[JOB])
    _baselined(repo, HALTED_ID, {JOB: b_h}, halted_at=HALT_AT)
    _record(repo, OTHER_ID, status=None)
    _baselined(repo, OTHER_ID, {JOB: b_l}, started_at=START_AT)


def test_halted_holder_with_no_hunks_in_dirty_path_releases(repo):
    base = _blob("base\n")
    _two_runs(repo, base, base)
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {}


def test_halted_holder_with_hunks_in_dirty_path_holds(repo):
    _two_runs(repo, _blob("base\n"), _blob("landing base\n"))
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_halted_holder_without_write_baseline_holds_dirty_path(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_withheld_path_holds_even_when_tree_equals_baseline(repo):
    base = _blob("base\n")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _baselined(repo, HALTED_ID, {JOB: base}, halted_at=HALT_AT)
    _dirty_job(repo, "base\n")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_default_now_uses_wall_clock(repo):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_id = f"ask-{stamp}-abcdef"
    _record(repo, run_id, withheld=[JOB])
    assert wrd.halted_run_write_claims(repo, [JOB], exclude_run=None) == {JOB: [run_id]}


def _git_repo_with_two_withheld(tmp_path, b_head):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args, stdin=""):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, input=stdin,
            capture_output=True, text=True, check=True, **no_console_creationflags(),
        ).stdout.strip()

    git("init", "-q")
    for name, text in (("a.py", "a = 1\n"), ("b.py", "b = 1\n")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    (tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(b_head, encoding="utf-8")
    git("commit", "-q", "-am", "head")
    rec = _record(tmp_path, HALTED_ID, writes=["a.py", "b.py"], withheld=["a.py", "b.py"])
    kept = {p: git("hash-object", "-w", "--stdin", stdin="%s = 2\n" % p[0]) for p in ("a.py", "b.py")}
    (rec / "kept.json").write_text(json.dumps({"chunks": {"X1": {"paths": kept}}, "plan_gate": None}), encoding="utf-8")


def test_withheld_path_kept_at_head_is_released_per_path(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 3\n")
    assert _claims(tmp_path, ["a.py", "b.py"]) == {"b.py": [HALTED_ID]}


def test_withheld_path_released_is_not_reclaimed_through_hunk_attribution(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 3\n")
    seen = []

    def attributable(run_id, paths):
        seen.append(set(paths))
        return set(paths)

    got = _claims(tmp_path, ["a.py", "b.py"], dirty=lambda: {"a.py", "b.py"}, attributable=attributable)
    assert got == {"b.py": [HALTED_ID]}
    assert all("a.py" not in s for s in seen)


def test_all_withheld_paths_at_head_hold_nothing(tmp_path):
    _git_repo_with_two_withheld(tmp_path, "b = 2\n")
    assert _claims(tmp_path, ["a.py", "b.py"]) == {}


def test_b9002c_replay_superseded_sizing_halted_run_claims_nothing_for_a_landing_on_another_sizing(repo):
    _sizing(repo, SIZING, "superseded")
    _sizing(repo, "state/sizings/land.yaml")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID, status=None)
    _bind(repo, OTHER_ID, sizing="state/sizings/land.yaml", marker=STATUS_HALTED)
    (repo / "state/warp" / OTHER_ID / halted_run.HALTED_FILE).unlink()
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}
