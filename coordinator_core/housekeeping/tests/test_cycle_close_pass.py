"""Close-pass behaviour of `cycle.run` / `cycle.plan_gate_releases`: both
`blocked_by` shapes release, every release rides the one commit, a failure on
one record strands nothing, and only a positively terminal blocker releases."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.housekeeping import cycle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )


def _record(path: Path, handoff_id: str, state: str, *, stub_id: Optional[str] = None,
            blocked_by: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"handoff_id: {handoff_id}", f"deployment_state: {state}"]
    if stub_id:
        lines.append(f"stub_id: {stub_id}")
    if blocked_by:
        lines.append(blocked_by)
    lines += ["category: infra", "---", "", "Body.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    return root


def _porcelain(root: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain", "--", "state/handoffs"],
        cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _release_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    live = root / "state" / "handoffs"
    _record(live / "2026-08-01_00001_inline.md", "hnd-inline", "awaiting_gate",
            blocked_by="blocked_by: [sat-b1]")
    # Block-sequence shape, as in example-retrieval-repo 2026-10-01-tier2-residue-draft.md,
    # including the column-0 comment that follows the sequence there.
    _record(live / "2026-08-02_00002_nested.md", "hnd-nested", "awaiting_gate",
            blocked_by="blocked_by:\n  - sat-b1\n# minted by someone")
    _record(root / "archive" / "handoffs" / "2026-07" / "2026-07-01_00000_blocker.md",
            "hnd-b1", "shipped", stub_id="sat-b1")
    _commit_all(root)
    return root


def test_a_nested_blocked_by_is_released_alongside_an_inline_one_in_one_commit(tmp_path):
    root = _release_repo(tmp_path)

    result = cycle.run(root, cap=10)

    assert result["close_error"] is None
    assert result["closed"] == 2
    for name in ("2026-08-01_00001_inline.md", "2026-08-02_00002_nested.md"):
        text = (root / "state" / "handoffs" / name).read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text
        assert "blocked_by: []" in text and "sat-b1" not in text
        assert "\n  - " not in text
        assert "category: infra" in text
    assert _porcelain(root) == "", "every release is committed, none left dirty"


def test_one_records_release_failure_neither_strands_nor_loses_the_others(tmp_path, monkeypatch):
    root = _release_repo(tmp_path)
    real_apply = cycle.apply_gate_clear

    def _fail_on_nested(path, repo_root, **kwargs):
        if "nested" in Path(path).name:
            raise ValueError("synthetic release failure")
        return real_apply(path, repo_root, **kwargs)

    monkeypatch.setattr(cycle, "apply_gate_clear", _fail_on_nested)

    result = cycle.run(root, cap=10)

    assert result["closed"] == 1
    assert "2026-08-02_00002_nested.md" in result["close_error"]
    assert "synthetic release failure" in result["close_error"]
    assert _porcelain(root) == "", "the released record is committed; the failed one was never written"
    nested = (root / "state" / "handoffs" / "2026-08-02_00002_nested.md").read_text(encoding="utf-8")
    assert "deployment_state: awaiting_gate" in nested


def test_a_failed_commit_restores_every_release_it_wrote(tmp_path, monkeypatch):
    root = _release_repo(tmp_path)

    async def _commit_refused(worktree_root, moves, subject):
        return [], [{"id": m.candidate_id, "reason": "commit-failed: synthetic"} for m in moves]

    monkeypatch.setattr(cycle, "archive_and_commit", _commit_refused)

    result = cycle.run(root, cap=10)

    assert result["closed"] == 0
    assert result["close_error"].count("gate release not committed") == 2
    assert _porcelain(root) == "", "no uncommitted mutation survives a failed commit"


def test_a_record_dirty_against_head_is_not_released_into_the_commit(tmp_path):
    root = _release_repo(tmp_path)
    inline = root / "state" / "handoffs" / "2026-08-01_00001_inline.md"
    inline.write_text(inline.read_text(encoding="utf-8") + "peer edit\n", encoding="utf-8")

    result = cycle.run(root, cap=10)

    assert result["closed"] == 1
    text = inline.read_text(encoding="utf-8")
    assert "peer edit" in text and "deployment_state: awaiting_gate" in text


def test_plan_gate_releases_predicts_what_run_releases(tmp_path):
    root = _release_repo(tmp_path)
    common_dir = cycle.git_common_dir(root)
    worktree_root = cycle.main_worktree_root(common_dir)

    plan = cycle.plan_gate_releases(worktree_root, common_dir)

    assert plan["would_release"] == [
        "state/handoffs/2026-08-01_00001_inline.md",
        "state/handoffs/2026-08-02_00002_nested.md",
    ]
    assert _porcelain(root) == "", "planning writes nothing"
    assert cycle.run(root, cap=10)["closed"] == len(plan["would_release"])


# -- Only a positively terminal blocker releases ------------------------------


def _gated_on_unknown_repo(tmp_path: Path) -> Path:
    """One gated record per id kind the example-retrieval-repo corpus holds that is NOT a
    finished handoff."""
    root = _repo(tmp_path)
    live = root / "state" / "handoffs"
    # stub id whose handoff is open (ready_to_fire)
    _record(live / "2026-08-01_fg-01.md", "hnd-fg-01", "ready_to_fire", stub_id="fg-01")
    _record(live / "2026-08-02_fg-02.md", "hnd-fg-02", "awaiting_gate", stub_id="fg-02",
            blocked_by="blocked_by: [fg-01]")
    # the gate's blocker is itself still gated
    _record(live / "2026-08-03_fg-05.md", "hnd-fg-05", "awaiting_gate",
            blocked_by="blocked_by: [hnd-fg-02]")
    # deliverable id whose open handoff is ready_to_fire
    _record(live / "2026-08-04_deliv.md", "hnd-deliv", "ready_to_fire")
    (live / "2026-08-04_deliv.md").write_text(
        (live / "2026-08-04_deliv.md").read_text(encoding="utf-8").replace(
            "category: infra", "category: infra\ndeliverable_id: dlv-open"),
        encoding="utf-8",
    )
    _record(live / "2026-08-05_cq-17.md", "hnd-cq-17", "awaiting_gate",
            blocked_by="blocked_by: [dlv-open]")
    # peer names: resolve to no handoff at all
    _record(live / "2026-08-06_peer.md", "hnd-peer", "awaiting_gate",
            blocked_by="blocked_by:\n  - coordinator-content-repo-em")
    _record(live / "2026-08-07_peer2.md", "hnd-peer2", "awaiting_gate",
            blocked_by="blocked_by: [claude-klabauter-em]")
    _commit_all(root)
    return root


GATED = {
    "state/handoffs/2026-08-02_fg-02.md",
    "state/handoffs/2026-08-03_fg-05.md",
    "state/handoffs/2026-08-05_cq-17.md",
    "state/handoffs/2026-08-06_peer.md",
    "state/handoffs/2026-08-07_peer2.md",
}


def test_stub_roadmap_deliverable_and_peer_ids_are_never_released(tmp_path):
    root = _gated_on_unknown_repo(tmp_path)
    common_dir = cycle.git_common_dir(root)
    plan = cycle.plan_gate_releases(cycle.main_worktree_root(common_dir), common_dir)

    assert plan["would_release"] == []

    result = cycle.run(root, cap=10)

    assert result["closed"] == 0 and result["close_error"] is None
    assert _porcelain(root) == ""
    for rel in GATED:
        assert "deployment_state: awaiting_gate" in (root / rel).read_text(encoding="utf-8")


def test_unknown_ids_are_reported_unresolvable_not_dead(tmp_path):
    root = _gated_on_unknown_repo(tmp_path)

    inv = cycle.run(root, cap=10)["invariant"]

    # Only ids that match no handoff are unresolvable; the three whose blocker
    # resolves to an open record are simply waiting.
    assert [i["blocker_id"] for i in inv["gate_unresolvable_blockers"]] == [
        "dlv-open", "coordinator-content-repo-em", "claude-klabauter-em",
    ]
    assert inv["gate_dead_blockers"] == []


def test_a_gate_naming_no_blocker_is_dead(tmp_path):
    root = _repo(tmp_path)
    _record(root / "state" / "handoffs" / "2026-08-01_g.md", "hnd-g", "awaiting_gate")
    _commit_all(root)

    inv = cycle.run(root, cap=10, close=False)["invariant"]

    assert [i["reason"] for i in inv["gate_dead_blockers"]] == ["no-blocked-by"]
    assert inv["gate_unresolvable_blockers"] == []
