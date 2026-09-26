"""
coordinator_core.ops.fleet.tests.test_prune_bugs

Tier-T unit coverage for `fleet.prune_closed_bugs` v2
(docs/plans/2026-09-07-fleet-prune-closed-bugs-v2-rebuild.md, C4). Written new
against the v2 contract — the pre-cull `test_prune_bugs.py` tested the deleted
v1 `git mv` handler and is NOT ported (plan Anti-scope).

Coverage, per the plan's Test surface section (one module, five concerns as
classes/functions, not five modules):
  - Discovery correctness: the stage-1 substring scan's candidate set is a
    superset of the stage-2 confirmed set.
  - Fail-closed refusals: a candidate whose YAML will not parse, and one that
    parses but carries no `status` key, are both refused — never archived on
    a bare substring match.
  - Three-way dest disposition: differing dst refuses (_REASON_DEST_CONFLICT),
    byte-identical dst converges (force=True), absent dst moves normally.
  - dry_run -> act envelope shape against build_dry_run_result/build_act_result.
  - A STRUCTURAL spawn-zero assertion: no subprocess call site exists in the
    handler module's own source, not merely an observed call count of 0.

Built entirely on `archive_git_free_seam.patched_disposition_seam` — the
family's mandated git-free vehicle — never a hand-rolled fixture, and never a
real `git init`. See that module's docstring (read first, per its own
instruction) for why a temp-repo test in this family is break-class, not
merely slow (2026-08-07 incident: 405 tests deleted after per-test real
`git init` on a 50-70-session box).

Negative-spec:
  - Does NOT re-test `archive_and_commit`'s own mechanics — that mover has its
    own suites; a test that patches the mover cannot observe what it does.
  - Does NOT measure corpus-scale process time or spawn count at a synthetic
    ~2x corner — that measurement lives in C2's audit, not in a test.
  - Does NOT exercise op registration/dispatch end-to-end — that is C5's
    separate `test_prune_bugs_registration_smoke.py`, gated on a live
    `start_server()` registry rather than this module's patched seam.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from coordinator_core.ops.fleet import prune_bugs as m
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    make_recording_mover,
    patched_disposition_seam,
)


def _run(coro):
    return asyncio.run(coro)


def _make_bug(worktree: Path, name: str, body: str) -> Path:
    bug_dir = worktree / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def test_stage1_candidate_set_is_superset_of_stage2_confirmed(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"

    _make_bug(worktree, "2026-01-01-closed.yaml", "status: closed\ntitle: closed one\n")
    _make_bug(
        worktree,
        "2026-01-02-mentions-only.yaml",
        "status: open\nnote: \"history: status: closed last month\"\n",
    )
    _make_bug(worktree, "2026-01-03-open.yaml", "status: open\ntitle: still going\n")

    stage1 = {p.name for p in m._enumerate_bugs(worktree) if m._stage1_frontmatter_bounded_scan(p)}
    stage2 = {p.name for p in m._discover_candidates(worktree)}

    assert "2026-01-01-closed.yaml" in stage1
    assert "2026-01-01-closed.yaml" in stage2
    assert "2026-01-02-mentions-only.yaml" in stage1
    assert stage2.issubset(stage1)
    assert "2026-01-03-open.yaml" not in stage1
    assert "2026-01-03-open.yaml" not in stage2


def test_unparseable_yaml_candidate_is_refused_not_archived(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-02-01-broken.yaml"
    _make_bug(worktree, "2026-02-01-broken.yaml", "status: closed\nnote: [unterminated\n")

    discovered = {p.name for p in m._discover_candidates(worktree)}
    assert "2026-02-01-broken.yaml" not in discovered

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
                repo_root=str(worktree),
            )
        )

    assert result["acted"] == []
    assert result["skipped"] == [{"id": cid, "reason": "drifted-open: status=None"}]
    assert mover.captured is None


def test_no_status_key_candidate_is_refused_not_archived(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-02-02-no-status.yaml"
    _make_bug(
        worktree,
        "2026-02-02-no-status.yaml",
        "title: mentions status: closed in prose\nno_status_here: true\n",
    )

    discovered = {p.name for p in m._discover_candidates(worktree)}
    assert "2026-02-02-no-status.yaml" not in discovered

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
                repo_root=str(worktree),
            )
        )

    assert result["acted"] == []
    assert result["skipped"] == [{"id": cid, "reason": "drifted-open: status=None"}]
    assert mover.captured is None


def test_dest_disposition_differing_file_refuses(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-03-01-wedged.yaml"
    _make_bug(worktree, "2026-03-01-wedged.yaml", "status: closed\ntitle: live copy\n")

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-03"
    dst_dir.mkdir(parents=True)
    (dst_dir / "2026-03-01-wedged.yaml").write_text(
        "status: closed\ntitle: a DIFFERENT archived copy\n", encoding="utf-8"
    )

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
                repo_root=str(worktree),
            )
        )

    assert result["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}]
    assert result["acted"] == []
    assert mover.captured is None


def test_dest_disposition_byte_identical_twin_converges(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-03-02-dup.yaml"
    body = "status: closed\ntitle: 2026-03-02-dup.yaml\n"
    _make_bug(worktree, "2026-03-02-dup.yaml", body)

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-03"
    dst_dir.mkdir(parents=True)
    (dst_dir / "2026-03-02-dup.yaml").write_text(body, encoding="utf-8")

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
                repo_root=str(worktree),
            )
        )

    assert result["skipped"] == []
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None
    assert len(mover.captured) == 1
    assert mover.captured[0].force is True


def test_dest_disposition_absent_destination_moves_normally(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-03-03-fresh.yaml"
    _make_bug(worktree, "2026-03-03-fresh.yaml", "status: closed\ntitle: fresh close\n")

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
                repo_root=str(worktree),
            )
        )

    assert result["skipped"] == []
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None
    assert len(mover.captured) == 1
    assert mover.captured[0].force is False
    assert mover.captured[0].dst == worktree / "archive" / "bug-backlog" / "2026-03" / "2026-03-03-fresh.yaml"


def test_dry_run_true_returns_candidates_envelope_and_mutates_nothing(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    src = _make_bug(worktree, "2026-04-01-closed.yaml", "status: closed\ntitle: envelope check\n")
    _make_bug(worktree, "2026-04-02-open.yaml", "status: open\ntitle: not this one\n")

    with patched_disposition_seam(m, worktree=worktree) as mover:
        result = _run(m._handler({"mode": "already-terminal", "dry_run": True}, repo_root=str(worktree)))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    ids = {c["id"] for c in result["candidates"]}
    assert ids == {"state/bug-backlog/2026-04-01-closed.yaml"}
    cand = result["candidates"][0]
    assert cand["title"] == "envelope check"
    assert cand["status"] == "closed"
    assert cand["family"] == "bug"
    assert mover.captured is None
    assert src.exists()


def test_act_round_trip_uses_dry_run_candidate_ids_and_returns_act_envelope(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _make_bug(worktree, "2026-04-03-closed.yaml", "status: closed\ntitle: round trip\n")

    with patched_disposition_seam(m, worktree=worktree) as mover:
        preview = _run(m._handler({"mode": "already-terminal", "dry_run": True}, repo_root=str(worktree)))
        candidate_ids = [c["id"] for c in preview["candidates"]]
        assert candidate_ids == ["state/bug-backlog/2026-04-03-closed.yaml"]

        result = _run(
            m._handler(
                {"mode": "already-terminal", "dry_run": False, "candidate_ids": candidate_ids},
                repo_root=str(worktree),
            )
        )

    assert result["exit_code"] == 0
    assert result["dry_run"] is False
    assert result["candidates"] == []
    assert result["acted"] == [{"id": candidate_ids[0], "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []
    assert mover.captured is not None
    assert mover.subject and "prune 1 closed bug entry" in mover.subject


def test_setup_error_on_bad_mode_returns_exit_code_1_envelope(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    with patched_disposition_seam(m, worktree=worktree):
        result = _run(m._handler({"mode": "bogus-mode", "dry_run": True}, repo_root=str(worktree)))

    assert result["exit_code"] == 1
    assert result["candidates"] == []
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


def test_handler_module_has_no_subprocess_call_site() -> None:
    """Structural, not observed: assert no subprocess/git-process call site
    exists anywhere in the handler's own source, so a future edit that adds
    one fails this test regardless of which code path a fixture happens to
    exercise. Mirrors the plan's C6 structural greps (no `subprocess`, no
    `os.replace`, no `git ` string, no direct `commit_paths`), scoped here
    to the act-path symbols this module itself is responsible for.

    Checked against the module's CODE only, with its own module docstring
    stripped first — that docstring narrates the negative-spec in prose
    ("has no subprocess/git call site at all") and would otherwise false-
    positive this exact assertion."""
    source = inspect.getsource(m)
    module_docstring = inspect.getdoc(m) or ""
    code_only = source.replace(module_docstring, "", 1)

    assert "subprocess" not in code_only
    assert "commit_paths" not in code_only
    assert "os.replace" not in code_only
    assert "git mv" not in code_only
    assert "git add" not in code_only

    assert code_only.count("archive_and_commit(") == 1
