"""
coordinator_core.ops.fleet.tests.test_migrate_vocabulary_discharges_archival

Pins AC1 closure for the "fifth writer" DR-324 left open
(docs/decisions/DR-324-succession-child-does-not-retain-for-arch.md § Open
item, docs/plans/2026-08-18-supersede-stamps-and-archives-atomically.md):
`fleet.migrate_handoff_vocabulary`'s `_plan_one` writes
`deployment_state: continued` (plus `continued_into`) on records that may be
resident in `state/handoffs/` — `apply_migration` must discharge that
record's archival in the SAME operation (archive it, or commit the flip in
place when a live `forked_from` child retains it), never leave it loose.

Covers:
  - a resident record migrated to `continued`, whose only live child is the
    SUCCESSION child (`predecessor:`) `continued_into` itself names — the
    narrow DR-324 exemption (`edge_kinds={"forked_from"}`) means that child
    does not retain it: FILE MOVED (gone from state/handoffs/, present under
    archive/handoffs/YYYY-MM/).
  - a resident record migrated to `continued` with an ADDITIONAL live
    `forked_from` child — retained, and the flip is COMMITTED in place (not
    left loose in the shared tree).
  - an already-archived record migrated to `continued` — untouched by the
    archival machinery (no double-move; the plain vocabulary rewrite still
    applies, matching this op's pre-existing archive/handoffs/** behavior).

Real git spawn is load-bearing here (archive_and_commit / commit_authored_
content orchestrate actual git state) — same convention as
coordinator_core/ops/tests/test_supersede_archives_atomically.py, which this
module's fixtures deliberately mirror.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.fleet import migrate_handoff_vocabulary as mig
from coordinator_core.ops.deliverable_equivalence import _reset_equivalence_map_cache
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


@pytest.fixture(autouse=True)
def _reset_equivalence_cache():
    """See migrate_handoff_vocabulary's own test module for why this reset is
    required (load_equivalence_map's documented per-process memoization)."""
    _reset_equivalence_map_cache()
    yield
    _reset_equivalence_map_cache()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _common_dir(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return Path(result.stdout.strip()).resolve()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _seed_abandoned_parent(repo: Path, rel: str, name: str) -> Path:
    """Old-vocab, resident-or-archived (caller picks `rel`) parent: status
    consumed, deployment_state abandoned — the shape `_plan_one` re-expresses
    as `continued` once a successor is found."""
    path = repo / rel / name
    _write(path, (
        f'---\ntitle: "{name}"\ncreated: 2026-01-01\nstatus: consumed\n'
        'predecessor: "none"\ndeployment_state: abandoned\n'
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\n\nBody.\n"
    ))
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_succession_child(repo: Path, name: str, predecessor_rel: str, handoff_id: str) -> Path:
    """Live handoff naming `predecessor_rel` via `predecessor:` (reverse-lineage
    succession edge `_find_successor` resolves) — this is the successor
    `continued_into` ends up naming, and per DR-324 it must NOT retain its
    own predecessor for archival (edge_kinds={"forked_from"} excludes it)."""
    path = repo / "state" / "handoffs" / name
    _write(path, (
        f'---\ntitle: "{name}"\ncreated: 2026-01-02\nstatus: open\n'
        f'predecessor: "{predecessor_rel}"\nhandoff_id: {handoff_id}\n'
        "deployment_state: in_flight\n---\n\nBody.\n"
    ))
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_forked_from_child(repo: Path, name: str, forked_from: str) -> Path:
    """Live handoff naming `forked_from` (bare filename) — a spinoff, which
    DOES still retain per DR-324's narrow exemption."""
    path = repo / "state" / "handoffs" / name
    _write(path, (
        f'---\ntitle: "{name}"\ncreated: 2026-01-02\nstatus: open\n'
        f'predecessor: "none"\nforked_from: "{forked_from}"\n'
        "deployment_state: in_flight\n---\n\nBody.\n"
    ))
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _fm(text: str):
    split = split_frontmatter(text)
    assert split is not None
    return split.fm_text


# ---------------------------------------------------------------------------
# Guard-safe: succession child alone does not retain -> FILE MOVED
# ---------------------------------------------------------------------------


def test_resident_continued_record_is_archived(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent = _seed_abandoned_parent(repo, "state/handoffs", "parent-archive.md")
    _seed_succession_child(
        repo, "child-archive.md", "state/handoffs/parent-archive.md", "hnd-child-archive-1"
    )
    common_dir = _common_dir(repo)

    plan = mig.plan_migration(str(repo))
    assert not plan["failures"], plan["failures"]
    rec = {r["path"]: r for r in plan["records"]}["state/handoffs/parent-archive.md"]
    assert rec["_continued_resident"] is True, rec

    archival = mig.apply_migration(plan, repo_root=common_dir)

    assert not parent.exists(), "guard-safe continued record must be archived, not left resident"
    archived = list((repo / "archive" / "handoffs").rglob("parent-archive.md"))
    assert len(archived) == 1, archived
    fm = _fm(archived[0].read_text(encoding="utf-8"))
    assert read_fm_field(fm, "status") == "claimed", fm
    assert read_fm_field(fm, "deployment_state") == "continued", fm
    assert read_fm_field(fm, "continued_into") == "hnd-child-archive-1", fm
    assert archival["archived"] == ["state/handoffs/parent-archive.md"], archival
    assert archival["retained"] == [], archival

    # Archival landed as a real commit — not left staged/loose.
    status = _git(repo, "status", "--porcelain")
    assert status.stdout.strip() == "", status.stdout


# ---------------------------------------------------------------------------
# Guard-retain: an ADDITIONAL live forked_from child retains -> flip committed in place
# ---------------------------------------------------------------------------


def test_resident_continued_record_with_forked_from_child_is_retained_and_committed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent = _seed_abandoned_parent(repo, "state/handoffs", "parent-retain.md")
    _seed_succession_child(
        repo, "child-retain.md", "state/handoffs/parent-retain.md", "hnd-child-retain-1"
    )
    _seed_forked_from_child(repo, "spinoff-retain.md", "parent-retain.md")
    common_dir = _common_dir(repo)

    plan = mig.plan_migration(str(repo))
    assert not plan["failures"], plan["failures"]
    rec = {r["path"]: r for r in plan["records"]}["state/handoffs/parent-retain.md"]
    assert rec["_continued_resident"] is True, rec

    archival = mig.apply_migration(plan, repo_root=common_dir)

    assert parent.exists(), "a live forked_from child must retain — file stays resident"
    assert archival["archived"] == [], archival
    assert archival["retained"] == ["state/handoffs/parent-retain.md"], archival

    fm = _fm(parent.read_text(encoding="utf-8"))
    assert read_fm_field(fm, "deployment_state") == "continued", fm
    assert read_fm_field(fm, "continued_into") == "hnd-child-retain-1", fm

    # COMMITTED in place — never left loose on disk in the shared tree.
    status = _git(repo, "status", "--porcelain")
    assert status.stdout.strip() == "", status.stdout
    log = _git(repo, "log", "-1", "--format=%s", "--", "state/handoffs/parent-retain.md")
    assert "parent-retain.md" in log.stdout or "supersede" in log.stdout.lower(), log.stdout


# ---------------------------------------------------------------------------
# Already-archived: untouched by the archival machinery, no double-move
# ---------------------------------------------------------------------------


def test_already_archived_continued_record_is_untouched_by_archival(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    parent = _seed_abandoned_parent(repo, "archive/handoffs", "parent-archived.md")
    _seed_succession_child(
        repo, "child-archived.md", "archive/handoffs/parent-archived.md", "hnd-child-archived-1"
    )
    common_dir = _common_dir(repo)

    plan = mig.plan_migration(str(repo))
    assert not plan["failures"], plan["failures"]
    rec = {r["path"]: r for r in plan["records"]}["archive/handoffs/parent-archived.md"]
    # Already archived — never a discharge target, even though it is re-expressed continued.
    assert rec["_continued_resident"] is False, rec

    archival = mig.apply_migration(plan, repo_root=common_dir)

    assert parent.exists(), "an already-archived record must never be moved"
    assert list((repo / "archive" / "handoffs").rglob("parent-archived.md")) == [parent]
    assert archival["archived"] == [], archival
    assert archival["retained"] == [], archival

    fm = _fm(parent.read_text(encoding="utf-8"))
    assert read_fm_field(fm, "deployment_state") == "continued", fm
    assert read_fm_field(fm, "continued_into") == "hnd-child-archived-1", fm

    # The plain vocabulary rewrite still applies (apply_migration's pre-existing,
    # uncommitted-write behavior for archive/handoffs/** is unchanged by this fix).
    status = _git(repo, "status", "--porcelain")
    assert "parent-archived.md" in status.stdout, status.stdout
