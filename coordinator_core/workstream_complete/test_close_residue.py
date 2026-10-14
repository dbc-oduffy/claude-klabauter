"""Tests for close_residue: the snapshot, the landing check and the payload
builders feeding completion_verdict's two residue readers. Every repo is a
real git repo under tmp_path."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.session import scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import close_residue as cr

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "sid-mine"


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", ".gitignore", "tracked.txt"], tmp_path)
    _git(["commit", "-q", "-m", "seed"], tmp_path)
    return tmp_path


def _claim(repo: Path, rel: str, sid: str = SID) -> None:
    scope.touch(sid, rel, cwd=str(repo), root=str(repo))


def _hold_baton(repo: Path, basename: str, sid: str = SID, state: str | None = None) -> None:
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    if state is not None:
        handoffs = repo / "state" / "handoffs"
        handoffs.mkdir(parents=True, exist_ok=True)
        (handoffs / basename).write_text(f"---\ndeployment_state: {state}\n---\n\nbody\n", encoding="utf-8")


# --- landing_states ----------------------------------------------------------


def test_landing_states_walk_a_single_path_through_every_state(repo: Path) -> None:
    rel = "state/x.md"
    (repo / "state").mkdir()
    assert cr.landing_states(repo, [rel]) == {rel: "absent"}
    (repo / rel).write_text("a\n", encoding="utf-8")
    assert cr.landing_states(repo, [rel]) == {rel: "untracked"}
    _git(["add", rel], repo)
    assert cr.landing_states(repo, [rel]) == {rel: "uncommitted"}
    _git(["commit", "-q", "-m", "x"], repo)
    assert cr.landing_states(repo, [rel]) == {rel: "landed"}
    (repo / rel).write_text("edited after the commit\n", encoding="utf-8")
    assert cr.landing_states(repo, [rel]) == {rel: "dirty"}
    (repo / rel).unlink()
    assert cr.landing_states(repo, [rel]) == {rel: "deleted"}


def test_landing_states_is_unverified_when_the_repo_is_unreadable(tmp_path: Path) -> None:
    assert cr.landing_states(tmp_path / "nonexistent", ["a.txt"]) == {"a.txt": "unverified"}


def test_landing_states_batches_many_paths(repo: Path) -> None:
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    assert cr.landing_states(repo, ["tracked.txt", "new.txt", "gone.txt"]) == {
        "tracked.txt": "dirty",
        "new.txt": "untracked",
        "gone.txt": "absent",
    }


def test_landing_states_empty_input(repo: Path) -> None:
    assert cr.landing_states(repo, []) == {}


# --- snapshot ----------------------------------------------------------------


def test_snapshot_reads_owned_paths_and_held_batons_with_no_spawn(repo: Path, monkeypatch) -> None:
    _claim(repo, "tracked.txt")
    _hold_baton(repo, "b1.md")
    # A claim of another class is not a baton.
    other = repo / ".git" / "coordinator-sessions" / "memo-claims" / "m.md"
    other.mkdir(parents=True)
    (other / "session_id").write_text(SID, encoding="utf-8")

    spawns: list = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawns.append(a) or real_run(*a, **k))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a) or (_ for _ in ()).throw(AssertionError("spawn")))

    snap = cr.snapshot(repo, SID)

    assert snap.owned_paths == ("tracked.txt",)
    assert snap.held_batons == ("b1.md",)
    assert snap.degraded is False
    assert spawns == []


def test_snapshot_of_another_session_is_empty(repo: Path) -> None:
    _claim(repo, "tracked.txt", sid="sid-peer")
    snap = cr.snapshot(repo, SID)
    assert snap == cr.ResidueSnapshot((), (), False)


def test_snapshot_with_no_session_id_is_degraded(repo: Path) -> None:
    assert cr.snapshot(repo, None).degraded is True


def test_snapshot_is_degraded_when_the_own_touch_record_is_malformed(repo: Path) -> None:
    _claim(repo, "tracked.txt")
    sink = repo / ".git" / "coordinator-sessions" / SID / "touch-record.jsonl"
    with sink.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert cr.snapshot(repo, SID).degraded is True


# --- owned_uncommitted_payload -------------------------------------------------


def test_no_owned_paths_is_not_applicable(repo: Path) -> None:
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "not-applicable"


def test_owned_dirty_tracked_file_is_open(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    _claim(repo, "tracked.txt")
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert payload == {"verdict": "open", "items": ["tracked.txt"], "contested": [], "reason": None}


def test_owned_clean_file_is_clean(repo: Path) -> None:
    _claim(repo, "tracked.txt")
    assert cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))["verdict"] == "clean"


def test_excluded_paths_are_not_residue(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    _claim(repo, "tracked.txt")
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID), excluded=["tracked.txt"])
    assert payload["verdict"] == "clean"


def test_gitignored_untracked_owned_path_is_excluded(repo: Path) -> None:
    (repo / "ignored").mkdir()
    (repo / "ignored" / "junk.txt").write_text("j\n", encoding="utf-8")
    _claim(repo, "ignored/junk.txt")
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "clean"
    assert payload["items"] == []


def test_untracked_owned_path_is_open(repo: Path) -> None:
    (repo / "fresh.txt").write_text("f\n", encoding="utf-8")
    _claim(repo, "fresh.txt")
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "open" and payload["items"] == ["fresh.txt"]


def test_peer_claimed_path_is_contested_never_open(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    _claim(repo, "tracked.txt")
    payload = cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID), peer_claimed=["tracked.txt"])
    assert payload["verdict"] == "clean"
    assert payload["items"] == []
    assert payload["contested"] == ["tracked.txt"]


def test_paths_under_dot_git_are_never_residue(repo: Path) -> None:
    rel = f".git/coordinator-sessions/{SID}/review-targets.txt"
    target = repo / rel
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")
    snap = cr.ResidueSnapshot((rel,), (), False)
    payload = cr.owned_uncommitted_payload(repo, snap)
    assert payload["verdict"] == "clean"
    assert payload["items"] == []


def test_dot_git_path_does_not_mask_a_real_owned_path(repo: Path) -> None:
    rel = f".git/coordinator-sessions/{SID}/review-targets.txt"
    (repo / rel).parent.mkdir(parents=True)
    (repo / rel).write_text("x\n", encoding="utf-8")
    (repo / "fresh.txt").write_text("f\n", encoding="utf-8")
    snap = cr.ResidueSnapshot((rel, "fresh.txt"), (), False)
    payload = cr.owned_uncommitted_payload(repo, snap)
    assert payload["verdict"] == "open" and payload["items"] == ["fresh.txt"]


def test_degraded_snapshot_is_indeterminate_never_clean(repo: Path) -> None:
    snap = cr.ResidueSnapshot(("tracked.txt",), (), True)
    payload = cr.owned_uncommitted_payload(repo, snap)
    assert payload["verdict"] == "indeterminate"
    assert payload["reason"]


def test_unverified_state_is_indeterminate(tmp_path: Path) -> None:
    snap = cr.ResidueSnapshot(("a.txt",), (), False)
    assert cr.owned_uncommitted_payload(tmp_path / "nonexistent", snap)["verdict"] == "indeterminate"


def test_check_ignore_spawns_only_when_an_untracked_owned_path_exists(repo: Path, monkeypatch) -> None:
    calls: list = []
    real = cr.git_native.check_ignore
    monkeypatch.setattr(cr.git_native, "check_ignore", lambda *a, **k: calls.append(a) or real(*a, **k))

    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    _claim(repo, "tracked.txt")
    cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert calls == []

    (repo / "fresh.txt").write_text("f\n", encoding="utf-8")
    (repo / "fresh2.txt").write_text("f\n", encoding="utf-8")
    _claim(repo, "fresh.txt")
    _claim(repo, "fresh2.txt")
    cr.owned_uncommitted_payload(repo, cr.snapshot(repo, SID))
    assert len(calls) == 1


# --- unstamped_run_batons_payload ----------------------------------------------


def test_no_held_batons_is_not_applicable(repo: Path) -> None:
    assert cr.unstamped_run_batons_payload(repo, cr.snapshot(repo, SID), dispositions={})["verdict"] == "not-applicable"


def test_pre_close_baton_without_disposition_is_open(repo: Path) -> None:
    _hold_baton(repo, "b1.md", state="in_flight")
    snap = cr.snapshot(repo, SID)
    payload = cr.unstamped_run_batons_payload(repo, snap, dispositions={})
    assert payload["verdict"] == "open" and payload["items"] == ["b1.md"]


def test_pre_close_baton_with_disposition_is_clean(repo: Path) -> None:
    _hold_baton(repo, "b1.md", state="in_flight")
    snap = cr.snapshot(repo, SID)
    payload = cr.unstamped_run_batons_payload(repo, snap, dispositions={"b1.md": {"disposition": "shipped"}})
    assert payload["verdict"] == "clean"


@pytest.mark.parametrize("state", ["awaiting_gate", "ready_to_fire", "in_flight"])
def test_post_close_baton_in_an_open_state_is_open(repo: Path, state: str) -> None:
    _hold_baton(repo, "b1.md", state=state)
    payload = cr.unstamped_run_batons_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "open" and payload["items"] == ["b1.md"]


def test_post_close_baton_stamped_or_archived_is_clean(repo: Path) -> None:
    _hold_baton(repo, "shipped.md", state="shipped")
    _hold_baton(repo, "archived.md")  # no file under state/handoffs
    payload = cr.unstamped_run_batons_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "clean"


def test_post_close_unreadable_baton_frontmatter_is_indeterminate(repo: Path) -> None:
    _hold_baton(repo, "b1.md", state="in_flight")
    (repo / "state" / "handoffs" / "b1.md").write_text("no frontmatter at all\n", encoding="utf-8")
    payload = cr.unstamped_run_batons_payload(repo, cr.snapshot(repo, SID))
    assert payload["verdict"] == "indeterminate"


def test_degraded_snapshot_baton_reading_is_indeterminate(repo: Path) -> None:
    snap = cr.ResidueSnapshot((), ("b1.md",), True)
    assert cr.unstamped_run_batons_payload(repo, snap, dispositions={})["verdict"] == "indeterminate"


# --- owned_dirty_sizing_paths --------------------------------------------------


def test_owned_dirty_sizing_paths_selects_only_dirty_sizings(repo: Path) -> None:
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / "state" / "sizings" / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    (repo / "state" / "sizings" / "peer.yaml").write_text("p: 1\n", encoding="utf-8")
    (repo / "state" / "sizings" / "clean.yaml").write_text("c: 1\n", encoding="utf-8")
    _git(["add", "state/sizings/clean.yaml"], repo)
    _git(["commit", "-q", "-m", "clean"], repo)
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    for rel in ("state/sizings/a.yaml", "state/sizings/peer.yaml", "state/sizings/clean.yaml", "tracked.txt"):
        _claim(repo, rel)

    paths = cr.owned_dirty_sizing_paths(repo, cr.snapshot(repo, SID), peer_claimed=["state/sizings/peer.yaml"])

    assert paths == ["state/sizings/a.yaml"]


# --- orphan_sizing_paths -------------------------------------------------------

_VALID_SIZING = """schema: sizing-object
name: "x"
intent: "an intent"
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: read
  evidence: "read"
"""


def _orphan(repo: Path, name: str, text: str = _VALID_SIZING, age_s: float = 7200) -> str:
    rel = f"state/sizings/{name}"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return rel


def test_orphan_sizing_paths_sweeps_only_valid_old_unclaimed_untracked(repo: Path) -> None:
    old = _orphan(repo, "old.yaml")
    _orphan(repo, "fresh.yaml", age_s=10)
    excluded = _orphan(repo, "excluded.yaml")
    peer = _orphan(repo, "peer.yaml")
    tracked = _orphan(repo, "tracked.yaml")
    _git(["add", tracked], repo)
    _git(["commit", "-q", "-m", "t"], repo)

    sweepable, refused = cr.orphan_sizing_paths(repo, exclude=[excluded], peer_claimed=[peer])

    assert sweepable == [old]
    assert refused == []


def test_orphan_sizing_paths_refuses_schema_invalid_with_reason(repo: Path) -> None:
    bad = _orphan(repo, "bad.yaml", "a: 1\n")
    broken = _orphan(repo, "broken.yaml", "a: [\n")

    sweepable, refused = cr.orphan_sizing_paths(repo)

    assert sweepable == []
    assert [rel for rel, _ in refused] == [bad, broken]
    assert all(reason for _, reason in refused)
    assert (repo / bad).exists()


def test_orphan_sizing_paths_without_sizings_home_is_empty(repo: Path) -> None:
    assert cr.orphan_sizing_paths(repo) == ([], [])
