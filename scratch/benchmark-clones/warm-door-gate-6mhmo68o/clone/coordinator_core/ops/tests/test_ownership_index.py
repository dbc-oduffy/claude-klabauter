"""
coordinator_core.ops.tests.test_ownership_index

AC-C19 coverage for `coordinator_core.ops.ownership_index.build_ownership_index`:
claim-store-first data flow, live+archive union, scan_errors propagation, and
the structural gate/ownership import separation.

Spec backlink: DoE-claude:pln-gate-resolution-widen-the-engi-690ee0 § C19
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops import ownership_index
from coordinator_core.session import claims

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")


def _write_baton(path: Path, sid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.name}"',
                "kind: session-handoff",
                "status: claimed",
                f"claimed_by: {sid}",
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _make_claim(repo: Path, cls: str, basename: str, session_id: str) -> None:
    cdir = repo / ".git" / "coordinator-sessions" / f"{cls}-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(session_id, encoding="utf-8")
    (cdir / "claimed_at").write_text("2026-07-27T00:00:00Z", encoding="utf-8")


def test_ac_c19_1_module_and_signature_exist():
    assert hasattr(ownership_index, "build_ownership_index")


def test_owned_batons_span_live_and_archive(tmp_path):
    repo = tmp_path / "repo"
    sid = "sid-c19-a"
    _init_repo(repo)

    live_baton = repo / "state" / "handoffs" / "still-live.md"
    _write_baton(live_baton, sid)
    _make_claim(repo, "handoff", live_baton.name, sid)

    archived_baton = repo / "archive" / "handoffs" / "2026-07" / "archived-mid-ceremony.md"
    _write_baton(archived_baton, sid)
    _make_claim(repo, "handoff", archived_baton.name, sid)

    completed_baton = repo / "archive" / "completed" / "2026-07" / "wsc-closed.md"
    _write_baton(completed_baton, sid)
    _make_claim(repo, "handoff", completed_baton.name, sid)

    owned, scan_errors = ownership_index.build_ownership_index(repo, sid)
    paths = {p for p, _fm in owned}

    assert scan_errors == []
    assert len(owned) == 3
    assert live_baton.resolve() in paths or live_baton in paths
    assert archived_baton in paths
    assert completed_baton in paths


def test_ownership_decided_by_claim_store_not_by_frontmatter_mirror(tmp_path):
    """A baton whose claimed_by frontmatter names sid but carries NO claim
    record for sid must not be counted — the claim store is authoritative,
    the frontmatter field is a validated mirror only."""
    repo = tmp_path / "repo"
    sid = "sid-claimstore-only"
    _init_repo(repo)

    unclaimed = repo / "state" / "handoffs" / "frontmatter-only.md"
    _write_baton(unclaimed, sid)
    # Deliberately no _make_claim call for this basename.

    owned, _scan_errors = ownership_index.build_ownership_index(repo, sid)
    assert owned == []


def test_frontmatter_mirror_disagreement_is_flagged_but_still_owned(tmp_path, caplog):
    """Review: code-reviewer — Finding 2 (P2) regression test. A basename
    WITH a claim record for sid, but whose `claimed_by` frontmatter names a
    DIFFERENT session (a stale mirror) must still be counted as owned (the
    claim-store decision is final either way) AND must produce a warning
    naming the disagreement — the exact combination the reviewer's finding
    noted had no test coverage."""
    import logging

    repo = tmp_path / "repo"
    sid = "sid-mirror-real"
    _init_repo(repo)

    baton = repo / "state" / "handoffs" / "mirror-drifted.md"
    _write_baton(baton, "sid-mirror-stale")  # frontmatter names a DIFFERENT sid
    _make_claim(repo, "handoff", baton.name, sid)  # claim-store names the real sid

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ownership_index"):
        owned, _scan_errors = ownership_index.build_ownership_index(repo, sid)

    paths = {p for p, _fm in owned}
    assert baton in paths
    assert any(
        "disagreeing" in rec.message and "sid-mirror-stale" in rec.message
        for rec in caplog.records
    )


def test_other_sessions_claims_excluded(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    mine = repo / "state" / "handoffs" / "mine.md"
    _write_baton(mine, "me-sid")
    _make_claim(repo, "handoff", mine.name, "me-sid")

    theirs = repo / "state" / "handoffs" / "theirs.md"
    _write_baton(theirs, "other-sid")
    _make_claim(repo, "handoff", theirs.name, "other-sid")

    owned, _scan_errors = ownership_index.build_ownership_index(repo, "me-sid")
    paths = {p for p, _fm in owned}
    assert mine in paths
    assert theirs not in paths


def test_non_handoff_claim_classes_excluded(tmp_path):
    """memo-claims/plan-claims are ownership over a different artifact class
    entirely; this index is scoped to handoff-claims only."""
    repo = tmp_path / "repo"
    sid = "sid-classes"
    _init_repo(repo)
    _make_claim(repo, "memo", "some-memo.md", sid)
    _make_claim(repo, "plan", "some-plan", sid)

    owned, _scan_errors = ownership_index.build_ownership_index(repo, sid)
    assert owned == []


def test_stale_claim_with_no_matching_path_does_not_crash(tmp_path):
    """A claimed basename with no matching walked file (deleted/relocated
    handoff) is recorded, not raised, and not silently fabricated into a
    result entry."""
    repo = tmp_path / "repo"
    sid = "sid-stale"
    _init_repo(repo)
    _make_claim(repo, "handoff", "ghost.md", sid)

    owned, scan_errors = ownership_index.build_ownership_index(repo, sid)
    assert owned == []
    assert scan_errors == []


def test_scan_errors_propagate_from_unreadable_archive_subtree(tmp_path, monkeypatch):
    """AC-C19-3: a walker-reported scan error over the archive corpus must
    surface through this index's return, not be silently dropped."""
    repo = tmp_path / "repo"
    sid = "sid-scanerr"
    _init_repo(repo)

    live_baton = repo / "state" / "handoffs" / "still-live.md"
    _write_baton(live_baton, sid)
    _make_claim(repo, "handoff", live_baton.name, sid)

    (repo / "archive" / "handoffs").mkdir(parents=True, exist_ok=True)

    import os

    real_walk = os.walk

    def _boom_walk(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None and str(top).endswith(str(Path("archive") / "handoffs")):
            onerror(OSError(13, "Permission denied", str(top)))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", _boom_walk)

    owned, scan_errors = ownership_index.build_ownership_index(repo, sid)
    assert scan_errors != []
    paths = {p for p, _fm in owned}
    assert live_baton in paths


def test_no_cross_import_with_gate_eval():
    """AC-C19-4 (structural, machine-checkable): ownership_index must not
    import gate_eval, and gate_eval must not import ownership_index."""
    import ast

    ownership_src = Path(ownership_index.__file__).read_text(encoding="utf-8")
    ownership_tree = ast.parse(ownership_src)
    for node in ast.walk(ownership_tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "coordinator_core.reconcile.gate_eval"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "coordinator_core.reconcile.gate_eval"

    import coordinator_core.reconcile.gate_eval as gate_eval

    gate_eval_src = Path(gate_eval.__file__).read_text(encoding="utf-8")
    gate_eval_tree = ast.parse(gate_eval_src)
    for node in ast.walk(gate_eval_tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "coordinator_core.ops.ownership_index"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "coordinator_core.ops.ownership_index"


def test_unresolvable_worktree_root_yields_nonempty_scan_errors(tmp_path):
    """An unresolvable claim store (e.g. worktree_root not inside a git repo)
    must surface via scan_errors, distinguishable from a genuine "owns
    nothing" empty result — the whole point of the checked claim-scan arm."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    owned, scan_errors = ownership_index.build_ownership_index(not_a_repo, "sid-unresolvable")
    assert owned == []
    assert scan_errors != []


def test_list_claims_by_session_still_finds_handoff_claim_used_above(tmp_path):
    """Sanity cross-check against claims.list_claims_by_session directly,
    matching the plan's own stated data flow (claim store first)."""
    repo = tmp_path / "repo"
    sid = "sid-crosscheck"
    _init_repo(repo)
    _make_claim(repo, "handoff", "h1.md", sid)
    assert claims.list_claims_by_session(sid, cwd=str(repo)) == [
        ("handoff-claims", "h1.md")
    ]
