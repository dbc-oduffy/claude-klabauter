"""abandoned_runs: the pure predicate and message, and the reaper op over a tmp repo."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs as ar
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ROOT = "state/warp"
NOW = 1_000_000_000.0
OLD = NOW - run_token.TTL_SECONDS - 10


def test_op_name_and_kind():
    assert ar.OP_NAME == "dispatch.reap_abandoned_runs"
    assert ar.HALTED_KIND == "abandoned"


def test_stale_unlive_run_is_abandoned_sorted():
    changed = {f"{ROOT}/b/manifest.json", f"{ROOT}/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": OLD, "b": OLD}, NOW) == ["a", "b"]


def test_live_run_excluded():
    changed = {f"{ROOT}/a/x.json", f"{ROOT}/b/x.json"}
    assert ar.abandoned_run_ids(changed, {"a"}, ROOT, {"a": OLD, "b": OLD}, NOW) == ["b"]


def test_nested_path_ignored():
    assert ar.abandoned_run_ids({f"{ROOT}/a/sub/x.json"}, set(), ROOT, {"a": OLD}, NOW) == []


def test_path_outside_record_root_ignored():
    changed = {"state/sizings/a/x.json", f"{ROOT}x/a/x.json", "other/state/warp/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": OLD}, NOW) == []


def test_young_run_not_abandoned():
    young = NOW - ar.TOKENLESS_QUIET_SECONDS + 10
    assert ar.abandoned_run_ids({f"{ROOT}/a/x.json"}, set(), ROOT, {"a": young}, NOW) == []


def test_missing_mtime_not_abandoned():
    assert ar.abandoned_run_ids({f"{ROOT}/a/x.json"}, set(), ROOT, {}, NOW) == []


def test_empty_input():
    assert ar.abandoned_run_ids(set(), set(), ROOT, {}, NOW) == []


def test_message_carries_halted_by_abandoned():
    msg = ar.reap_message(["a", "b"])
    assert "Halted-By: abandoned" in msg
    assert "a, b" in msg.splitlines()[0]


QUIET_OLD = NOW - ar.QUIET_SECONDS - 10
QUIET_YOUNG = NOW - ar.QUIET_SECONDS + 10


def test_superseded_run_ended_inside_ttl_after_quiet_floor():
    changed = {f"{ROOT}/a/x.json", f"{ROOT}/b/x.json"}
    mt = {"a": QUIET_OLD, "b": QUIET_YOUNG}
    assert ar.abandoned_run_ids(changed, set(), ROOT, mt, NOW, later_stamp_of={"a": "z", "b": "z"}) == ["a"]


def test_halt_marked_run_ended_after_quiet_floor():
    changed = {f"{ROOT}/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_OLD}, NOW, halted={"a"}) == ["a"]
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_YOUNG}, NOW, halted={"a"}) == []


def test_late_written_run_ended_after_quiet_floor():
    changed = {f"{ROOT}/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_OLD}, NOW, late_written={"a"}) == ["a"]


def test_tokenless_run_ended_only_after_tokenless_quiet_floor():
    changed = {f"{ROOT}/a/x.json"}
    old = NOW - ar.TOKENLESS_QUIET_SECONDS - 10
    young = NOW - ar.TOKENLESS_QUIET_SECONDS + 10
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_OLD}, NOW) == []
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": young}, NOW) == []
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": old}, NOW) == ["a"]
    assert ar.abandoned_run_ids(changed, {"a"}, ROOT, {"a": old}, NOW) == []


def test_live_run_stays_even_when_superseded_halted_and_late():
    out = ar.abandoned_run_ids(
        {f"{ROOT}/a/x.json"}, {"a"}, ROOT, {"a": QUIET_OLD}, NOW,
        later_stamp_of={"a": "z"}, halted={"a"}, late_written={"a"},
    )
    assert out == []


# --- the op over a tmp repo -------------------------------------------------------------

LIMIT_WITH_CANDIDATES = 2

STALE = "ask-20261001T000000-aaaaaa"
STALE2 = "ask-20261001T000001-bbbbbb"
LIVE = "ask-20261001T000002-cccccc"
CLAIMED = "ask-20261001T000003-dddddd"
YOUNG = "ask-20261001T000004-eeeeee"
SCRIPT = "run.mjs"
SIZING = "state/sizings/orphan.yaml"
HELD_SIZING = "state/sizings/held.yaml"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _record(root: Path, run_id: str, *, age: float, sizing: str | None = None) -> None:
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing} if sizing else {}), encoding="utf-8")
    stamp = time.time() - age
    for f in rec.iterdir():
        os.utime(f, (stamp, stamp))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "state" / "sizings").mkdir(parents=True)
    for rel in (SIZING, HELD_SIZING):
        (root / rel).write_text("a: 1\n", encoding="utf-8")
    old = run_token.TTL_SECONDS + 3600
    _record(root, STALE, age=old, sizing=SIZING)
    _record(root, STALE2, age=old)
    _record(root, LIVE, age=old, sizing=HELD_SIZING)
    _record(root, CLAIMED, age=old)
    _record(root, YOUNG, age=60)
    run_token.mint(root, LIVE, SCRIPT, session_id="s", sizing_rel=HELD_SIZING)
    token = run_token.mint(root, CLAIMED, SCRIPT)
    assert run_token.redeem(root, CLAIMED, token, SCRIPT)
    return root


def _porcelain(root: Path) -> set[str]:
    return {l[3:] for l in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def _reap(root: Path, **params) -> dict:
    return ar._handler(params, repo_root=root / ".git")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reaper_lands_abandoned_runs_and_orphaned_sizing_in_one_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo)

    assert out["ok"] is True
    assert out["reaped"] == [STALE, STALE2]
    assert out["sizings"] == [SIZING]
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == head
    names = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert names == {
        f"state/warp/{r}/{f}" for r in (STALE, STALE2) for f in ("ask.md", "run.json")
    } | {SIZING}
    assert "Halted-By: abandoned" in _git(["log", "-1", "--format=%B"], repo)
    assert out["committed_sha"] == _git(["rev-parse", "HEAD"], repo).strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_live_claimed_and_young_runs_stay_dirty(repo):
    _reap(repo)

    dirty = _porcelain(repo)
    for run_id in (LIVE, CLAIMED, YOUNG):
        assert f"state/warp/{run_id}/ask.md" in dirty
    assert HELD_SIZING in dirty
    assert not any(STALE in p or STALE2 in p for p in dirty)
    assert SIZING not in dirty


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_second_call_is_a_noop(repo):
    _reap(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dry_run_commits_nothing(repo):
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo, dry_run=True)

    assert out["reaped"] == [STALE, STALE2] and out["committed_sha"] is None
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


MID_AGE = ar.QUIET_SECONDS + 300
assert MID_AGE < run_token.TTL_SECONDS

FOO_SIZING = "state/sizings/foo.yaml"
FOO_BATON = "state/handoffs/foo-baton.md"
FOO_PLAN = "docs/plans/foo.md"
FOO_EVIDENCE = "docs/plans/foo.evidence.yaml"
FOO_OLD = "warp-foo-20261001T000000-aaaaaa"
FOO_NEW = "warp-foo-20261001T000100-bbbbbb"


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _foo_graph(root: Path) -> None:
    _write(root, FOO_SIZING, f"baton: {FOO_BATON}\nplan: {FOO_PLAN}\n")
    _write(root, FOO_BATON, "---\ncarried_items: []\n---\nbaton\n")
    for rel in (FOO_PLAN, FOO_EVIDENCE):
        _write(root, rel)


@pytest.fixture
def bare(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "seed.txt", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _committed_names(root: Path) -> set[str]:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_superseded_run_reaped_inside_ttl_with_baton_plan_and_evidence(bare):
    _foo_graph(bare)
    _record(bare, FOO_OLD, age=MID_AGE, sizing=FOO_SIZING)
    _record(bare, FOO_NEW, age=60, sizing=FOO_SIZING)

    out = _reap(bare)

    assert out["ok"] is True, out.get("error")
    assert out["reaped"] == [FOO_OLD]
    assert set(out["artifacts"][FOO_OLD]) == {FOO_SIZING, FOO_BATON, FOO_PLAN, FOO_EVIDENCE}
    assert _committed_names(bare) == {
        f"state/warp/{FOO_OLD}/{f}" for f in ("ask.md", "run.json")
    } | {FOO_SIZING, FOO_BATON, FOO_PLAN, FOO_EVIDENCE}
    assert f"state/warp/{FOO_NEW}/ask.md" in _porcelain(bare)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_halt_marked_run_reaped_inside_ttl(bare):
    _record(bare, FOO_OLD, age=MID_AGE)
    marker = bare / "state" / "warp" / FOO_OLD / "halted.json"
    marker.write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    os.utime(marker, (time.time() - MID_AGE,) * 2)

    out = _reap(bare)

    assert out["reaped"] == [FOO_OLD]
    assert f"state/warp/{FOO_OLD}/halted.json" in _committed_names(bare)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unmarked_unsuperseded_run_inside_ttl_waits(bare):
    _record(bare, FOO_OLD, age=MID_AGE)

    out = _reap(bare)

    assert out["reaped"] == [] and out["committed_sha"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_late_modified_tracked_requirements_reaped_inside_ttl(bare):
    _record(bare, FOO_OLD, age=MID_AGE)
    req = bare / "state" / "warp" / FOO_OLD / "requirements.md"
    req.write_text("one\n", encoding="utf-8")
    _git(["add", "."], bare)
    _git(["commit", "-q", "-m", "run commit"], bare)
    write_seen_by_git(req, "two\n")
    os.utime(req, (time.time() - MID_AGE,) * 2)

    out = _reap(bare)

    assert out["reaped"] == [FOO_OLD]
    assert _committed_names(bare) == {f"state/warp/{FOO_OLD}/requirements.md"}
    assert _porcelain(bare) == set()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_live_token_halted_run_is_skipped(bare):
    _record(bare, FOO_OLD, age=MID_AGE)
    marker = bare / "state" / "warp" / FOO_OLD / "halted.json"
    marker.write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    os.utime(marker, (time.time() - MID_AGE,) * 2)
    run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")

    assert _reap(bare)["reaped"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_claimed_token_superseded_run_is_skipped(bare):
    _foo_graph(bare)
    _record(bare, FOO_OLD, age=MID_AGE, sizing=FOO_SIZING)
    _record(bare, FOO_NEW, age=60, sizing=FOO_SIZING)
    token = run_token.mint(bare, FOO_OLD, SCRIPT)
    assert run_token.redeem(bare, FOO_OLD, token, SCRIPT)

    assert _reap(bare)["reaped"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_peer_held_baton_is_skipped_and_named(bare, monkeypatch):
    from coordinator_core.session import scope

    _foo_graph(bare)
    _record(bare, FOO_OLD, age=MID_AGE, sizing=FOO_SIZING)
    _record(bare, FOO_NEW, age=60, sizing=FOO_SIZING)
    monkeypatch.setattr(
        scope, "contested_by_live_peers",
        lambda paths, sid, cwd=None: {p: ["peer"] if p == FOO_BATON else [] for p in paths},
    )

    out = _reap(bare, session_id="11111111-1111-4111-8111-111111111111")

    assert FOO_BATON not in _committed_names(bare)
    assert {"run_id": FOO_OLD, "reason": "peer-held", "path": FOO_BATON} in out["skipped"]
    assert FOO_BATON in _porcelain(bare)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_spawn_count_with_and_without_candidates(repo, monkeypatch):
    spawns = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            spawns.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)

    _reap(repo)
    with_candidates = len(spawns)
    spawns.clear()
    _reap(repo)

    assert len(spawns) == 1
    assert with_candidates <= LIMIT_WITH_CANDIDATES, with_candidates


def test_token_activity_window_decides_whether_a_token_is_live():
    now = 1_000_000.0
    fresh = {"run_id": FOO_NEW, "minted_at": now - 60}
    stale = {"run_id": FOO_NEW, "minted_at": now - ar.TOKENLESS_QUIET_SECONDS - 60}
    assert ar._token_active(Path("."), fresh, {}, now) is True
    assert ar._token_active(Path("."), stale, {}, now) is False
    assert ar._token_active(Path("."), stale, {FOO_NEW: now - 60}, now) is True


def test_artifact_runs_are_candidates_without_a_record_change():
    old = NOW - ar.TOKENLESS_QUIET_SECONDS - 10
    assert ar.abandoned_run_ids(set(), set(), ROOT, {"a": old}, NOW, artifact_runs={"a"}) == ["a"]
    assert ar.abandoned_run_ids(set(), set(), ROOT, {"a": old}, NOW) == []


def test_artifact_run_follows_the_same_ended_rule():
    changed: set[str] = set()
    kw = {"artifact_runs": {"a"}}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_OLD}, NOW, halted={"a"}, **kw) == ["a"]
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_YOUNG}, NOW, halted={"a"}, **kw) == []
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": QUIET_OLD}, NOW, **kw) == []
    old = NOW - ar.TOKENLESS_QUIET_SECONDS - 10
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": old}, NOW, **kw) == ["a"]
    assert ar.abandoned_run_ids(changed, {"a"}, ROOT, {"a": old}, NOW, **kw) == []


FOO_HANDOFF = "state/handoffs/foo.md"
FOO_SLICE = "state/sizings/foo-slice-1.yaml"
FOO_DELIVERY = f"docs/delivery/{FOO_OLD}.md"
FOO_ARTIFACTS = {FOO_PLAN, FOO_HANDOFF, FOO_EVIDENCE, FOO_DELIVERY, FOO_SLICE}


def _stranded_artifacts(root: Path, *, age: float) -> None:
    """A halted run whose record dir, sizing and baton landed, with the docs-stage output dirty."""
    _write(root, FOO_SIZING, f"baton: {FOO_BATON}\nplan: {FOO_PLAN}\n")
    _write(root, FOO_BATON, "baton\n")
    _record(root, FOO_OLD, age=age, sizing=FOO_SIZING)
    marker = root / "state" / "warp" / FOO_OLD / "halted.json"
    marker.write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "halt"], root)
    stamp = f"sizing_object: {FOO_SIZING}\n"
    for rel in FOO_ARTIFACTS:
        _write(root, rel, f"---\n{stamp}---\nbody\n")
        os.utime(root / rel, (time.time() - age,) * 2)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dirty_artifacts_of_a_clean_record_dir_are_reaped_in_one_commit(bare):
    _stranded_artifacts(bare, age=MID_AGE)
    head = _git(["rev-parse", "HEAD"], bare).strip()

    out = _reap(bare)

    assert out["ok"] is True, out.get("error")
    assert out["reaped"] == [FOO_OLD]
    assert set(out["artifacts"][FOO_OLD]) == FOO_ARTIFACTS
    landed = set(_git(["diff", "--name-only", f"{head}..HEAD"], bare).split())
    assert landed == FOO_ARTIFACTS
    assert out["committed_sha"]
    assert _porcelain(bare) == set()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dirty_artifacts_of_a_live_run_stay_dirty(bare):
    _stranded_artifacts(bare, age=MID_AGE)
    run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")

    out = _reap(bare)

    assert out["reaped"] == [] and out["committed_sha"] is None
    assert {"run_id": FOO_OLD, "reason": "live-or-recent"} in out["skipped"]
    assert FOO_ARTIFACTS <= _porcelain(bare)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dry_run_reports_artifact_only_run(bare):
    _stranded_artifacts(bare, age=MID_AGE)

    out = _reap(bare, dry_run=True)

    assert out["reaped"] == [FOO_OLD] and out["committed_sha"] is None
    assert set(out["artifacts"][FOO_OLD]) == FOO_ARTIFACTS
    assert FOO_ARTIFACTS <= _porcelain(bare)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unmarked_artifact_run_waits_out_the_tokenless_hour(bare):
    _stranded_artifacts(bare, age=MID_AGE)
    (bare / "state" / "warp" / FOO_OLD / "halted.json").unlink()
    _git(["add", "-A"], bare)
    _git(["commit", "-q", "-m", "unmark"], bare)

    assert _reap(bare)["reaped"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dead_token_does_not_hold_the_sizing(bare):
    _foo_graph(bare)
    _record(bare, FOO_OLD, age=MID_AGE, sizing=FOO_SIZING)
    _record(bare, FOO_NEW, age=ar.TOKENLESS_QUIET_SECONDS + 600, sizing=FOO_SIZING)
    run_token.mint(bare, FOO_NEW, SCRIPT, sizing_rel=FOO_SIZING)
    path = run_token._record_path(bare, FOO_NEW)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - ar.TOKENLESS_QUIET_SECONDS - 600
    path.write_text(json.dumps(record), encoding="utf-8")
    old = record["minted_at"]
    os.utime(path, (old, old))

    out = _reap(bare)

    assert out["reaped"] == [FOO_OLD, FOO_NEW]
    assert FOO_SIZING in out["artifacts"][FOO_OLD]
    assert not [s for s in out["skipped"] if s.get("reason") == "sizing-held-by-live-token"]


def _aged_token(root: Path, run_id: str, age: float) -> None:
    run_token.mint(root, run_id, SCRIPT, session_id="s")
    path = run_token._record_path(root, run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - age
    path.write_text(json.dumps(record), encoding="utf-8")
    os.utime(path, (record["minted_at"],) * 2)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_idle_held_token_run_is_reaped_and_its_token_released(bare):
    _record(bare, FOO_OLD, age=ar.TOKENLESS_QUIET_SECONDS + 600)
    _aged_token(bare, FOO_OLD, ar.TOKENLESS_QUIET_SECONDS + 600)

    dry = _reap(bare, dry_run=True)
    assert dry["reaped"] == [FOO_OLD]
    assert run_token.is_live(bare, FOO_OLD)

    out = _reap(bare)

    assert out["reaped"] == [FOO_OLD]
    assert not run_token.is_live(bare, FOO_OLD)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_active_held_token_run_is_still_skipped(bare):
    _record(bare, FOO_OLD, age=ar.TOKENLESS_QUIET_SECONDS + 600)
    run_token.mint(bare, FOO_OLD, SCRIPT, session_id="s")

    out = _reap(bare)

    assert out["reaped"] == []
    assert run_token.is_live(bare, FOO_OLD)


def test_abandon_run_needs_no_plan_path_for_a_plan_less_run(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run, op

    rid = "warp-xs-20261029T000000-abcdef"
    rdir = tmp_path / "state" / "warp" / rid
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"resumed_from": "ask-20261029T000000-aaaaaa"}), encoding="utf-8")

    out = op._dispatch_emit({"abandon_run": rid}, tmp_path)

    assert out["abandoned"] is True
    assert halted_run._read(rdir)["status"] == halted_run.STATUS_CLOSED
