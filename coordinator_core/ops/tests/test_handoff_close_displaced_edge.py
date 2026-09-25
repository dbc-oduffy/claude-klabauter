"""
coordinator_core.ops.tests.test_handoff_close_displaced_edge

Pins P073-C7: `_close` refuses `reason: displaced` on a record whose
`continued_into` is non-empty (handoff-legal-state-table.md § Q3) — a
non-empty `continued_into` means the record is `continued`, not `closed`,
so stamping `deployment_state: closed` + `closed_reason: displaced` over it
would silently discard that meaning.

No real git fixture: `locked_rmw`'s lock-sidecar resolution
(`coordinator_core.lifecycle.git_common_dir`, lru_cache'd) is monkeypatched
directly, mirroring `test_handoff_close_origin_stub.py`'s
`_fake_git_common_dir` idiom — the module under test does not itself need
real git plumbing to exercise `_close`.

Coverage:
  (a) `displaced` + non-empty `continued_into` -> refuses, no write (bytes
      unchanged on disk).
  (b) `displaced` with no `continued_into` -> still closes (unaffected).
  (c) `cancelled` and `stale` with a non-empty `continued_into` -> still
      close (the refusal is displaced-only, per the row body).

Spec: docs/plans/2026-09-11-handoff-lifecycle-one-legal-state-table.md
(P073-C7).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.handoff_transition import _close


def _fake_git_common_dir(worktree: Path, monkeypatch) -> None:
    import coordinator_core.lifecycle as lifecycle

    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    lock_root = worktree / ".fake-git-common"
    lock_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lifecycle, "git_common_dir", lambda repo_root: lock_root)


def _seed_handoff(
    worktree: Path,
    *,
    deployment_state: str = "open",
    continued_into: str | None = None,
    name: str = "handoff.md",
) -> Path:
    handoff = worktree / "state" / "handoffs" / name
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: session-handoff\n"
        "title: Fixture handoff\n"
        "created: 2026-09-11\n"
        "branch: main\n"
        "status: open\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: displaced-edge refusal fixture\n"
        f"deployment_state: {deployment_state}\n"
    )
    if continued_into is not None:
        fm += f"continued_into: {continued_into}\n"
    handoff.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return handoff


def test_close_displaced_refuses_when_continued_into_non_empty(tmp_path, monkeypatch):
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)
    handoff = _seed_handoff(worktree, continued_into="state/handoffs/successor.md")
    before = handoff.read_text(encoding="utf-8")

    result = _close("state/handoffs/handoff.md", "displaced", worktree, worktree)

    assert result["exit_code"] == 1
    assert "Q3" in result["error"]
    assert "displaced" in result["error"]
    # No write occurred — bytes on disk are unchanged.
    assert handoff.read_text(encoding="utf-8") == before


def test_close_displaced_still_closes_when_no_lineage_edge(tmp_path, monkeypatch):
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)
    handoff = _seed_handoff(worktree, continued_into=None)

    result = _close("state/handoffs/handoff.md", "displaced", worktree, worktree)

    assert result["exit_code"] == 0
    assert result["applied"] is True
    text = handoff.read_text(encoding="utf-8")
    assert "deployment_state: closed" in text
    assert "closed_reason: displaced" in text


def test_close_cancelled_and_stale_unaffected_by_lineage_edge(tmp_path, monkeypatch):
    worktree = tmp_path
    _fake_git_common_dir(worktree, monkeypatch)

    for reason, name in (("cancelled", "h-cancelled.md"), ("stale", "h-stale.md")):
        handoff = _seed_handoff(
            worktree, continued_into="state/handoffs/successor.md", name=name
        )
        result = _close(f"state/handoffs/{name}", reason, worktree, worktree)
        assert result["exit_code"] == 0
        assert result["applied"] is True
        text = handoff.read_text(encoding="utf-8")
        assert "deployment_state: closed" in text
        assert f"closed_reason: {reason}" in text
