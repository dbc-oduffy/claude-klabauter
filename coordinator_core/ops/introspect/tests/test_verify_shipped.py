"""Tests for verify_shipped — the unified git-ancestry + frontmatter + cockpit-emission
shipped-state verdict.

Mirrors `coordinator_core/ops/emit/tests/test_check_shipped_on_main.py`'s
`repo_with_origin` fixture: a real tmp git repo with a real bare "origin" remote,
never a mocked git call — the whole point of this primitive is to get the git-ancestry
leg's semantics right, so a mock would test nothing.

Spec backlink: state/handoffs/2026-07-25_000823_shipped-state-verifier.md § Acceptance criteria
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.introspect import verify_shipped as vs_module
from coordinator_core.ops.introspect.verify_shipped import verify_shipped
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real git process because the property under
# test is git's own ancestry semantics (git merge-base against origin/main), which no
# mock stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo_with_origin(tmp_path):
    """A working repo with a real "origin" remote and an on-main + off-main commit.

    Function-scoped, NOT hoisted to module scope like the sibling
    test_check_shipped_on_main.py fixture: most tests here write and commit their own
    `docs/plans/example.md`/`state/handoffs/example.md` with content that repeats
    across tests (e.g. two different tests both write "status: implemented"), so a
    shared repo would make a later test's `git commit` fail with "nothing to commit"
    when its content happens to match a still-pending prior commit in the shared
    working tree -- tried, reproduced the failure, reverted. One repo per test is the
    correct tradeoff here.

    Layout (mirrors test_check_shipped_on_main.py's fixture):
      - bare_origin/  — bare repo acting as "origin"
      - work/         — clone; origin/main tracks bare_origin's main
        - first commit  -> pushed to origin/main (ON_MAIN)
        - second commit -> local-only, NOT pushed (NOT_ON_MAIN)
    """
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True, **no_console_creationflags())
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "a.txt").write_text("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "first")
    on_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "origin", "main")

    (work / "b.txt").write_text("two\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "second (unpushed)")
    off_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    _git(work, "fetch", "origin")

    return {"root": work, "on_main": on_main_sha, "off_main": off_main_sha}


def _write_plan(root: Path, rel_path: str, frontmatter_lines: list[str]) -> None:
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n# Doc\n"
    full.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# indeterminate
# ---------------------------------------------------------------------------

def test_unresolvable_ref_is_indeterminate(repo_with_origin):
    result = verify_shipped("not-a-real-ref", repo_root=repo_with_origin["root"])
    assert result.verdict == "indeterminate"
    assert result.resolved_sha is None
    assert result.git_on_main is None


def test_missing_plan_path_is_indeterminate(repo_with_origin):
    sha = repo_with_origin["on_main"]
    result = verify_shipped(
        sha, plan_path="docs/plans/does-not-exist.md", repo_root=repo_with_origin["root"]
    )
    assert result.verdict == "indeterminate"
    assert result.resolved_sha == sha
    assert result.frontmatter_status is None


# ---------------------------------------------------------------------------
# plan_path omitted — git alone decides
# ---------------------------------------------------------------------------

def test_no_plan_path_shipped_from_git_alone(repo_with_origin):
    result = verify_shipped(repo_with_origin["on_main"], repo_root=repo_with_origin["root"])
    assert result.verdict == "shipped"
    assert result.frontmatter_status is None


def test_no_plan_path_not_shipped_from_git_alone(repo_with_origin):
    result = verify_shipped(repo_with_origin["off_main"], repo_root=repo_with_origin["root"])
    assert result.verdict == "not_shipped"
    assert result.frontmatter_status is None


# ---------------------------------------------------------------------------
# disagreement — both directions
# ---------------------------------------------------------------------------

def test_disagreement_git_ahead_of_frontmatter(repo_with_origin):
    """git says the SHA is on main, but the plan's own frontmatter still says draft —
    the doc is stale, or the merged commit belongs to different work than claimed."""
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: draft"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    result = verify_shipped(
        repo_with_origin["on_main"], plan_path="docs/plans/example.md", repo_root=root
    )
    assert result.git_on_main is True
    assert result.frontmatter_status == "not_shipped"
    assert result.verdict == "disagreement"


def test_disagreement_frontmatter_ahead_of_git(repo_with_origin):
    """The doc claims implemented/shipped, but the SHA it names isn't actually reachable
    from origin/main — the WFVALIDATE/QSUB03 defect shape this module exists to catch."""
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: implemented"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    result = verify_shipped(
        repo_with_origin["off_main"], plan_path="docs/plans/example.md", repo_root=root
    )
    assert result.git_on_main is False
    assert result.frontmatter_status == "shipped"
    assert result.verdict == "disagreement"


# ---------------------------------------------------------------------------
# agreement — both directions, sanity check the non-disagreement path
# ---------------------------------------------------------------------------

def test_agreement_shipped(repo_with_origin):
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: implemented"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    result = verify_shipped(
        repo_with_origin["on_main"], plan_path="docs/plans/example.md", repo_root=root
    )
    assert result.verdict == "shipped"


def test_agreement_not_shipped_handoff(repo_with_origin):
    root = repo_with_origin["root"]
    _write_plan(root, "state/handoffs/example.md", ["status: claimed", "deployment_state: in_flight"])
    _git(root, "add", "state/handoffs/example.md")
    _git(root, "commit", "-m", "add handoff")

    result = verify_shipped(
        repo_with_origin["off_main"], plan_path="state/handoffs/example.md", repo_root=root
    )
    assert result.frontmatter_status == "not_shipped"
    assert result.verdict == "not_shipped"


# ---------------------------------------------------------------------------
# emission_snapshot cross-check (leg 3) — fixture JSON only, never the live file
# ---------------------------------------------------------------------------

def test_emission_snapshot_present_and_notes_disagreement(repo_with_origin, tmp_path, monkeypatch):
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: draft"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    state_dir = tmp_path / "fixture_state"
    state_dir.mkdir()
    (state_dir / "cockpit-emission.json").write_text(
        json.dumps(
            {
                "plans": [
                    {
                        "provenance": {"path": "docs/plans/example.md"},
                        "status": "implemented",
                        "shipped_sha": repo_with_origin["on_main"],
                        "deliverable_status": "shipped",
                        "emitted_at": "2026-07-01T00:00:00Z",
                    }
                ],
                "handoffs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs_module, "_resolve_state_root", lambda: state_dir)

    result = verify_shipped(
        repo_with_origin["on_main"], plan_path="docs/plans/example.md", repo_root=root
    )

    assert result.emission_snapshot is not None
    assert result.emission_snapshot["shipped_sha"] == repo_with_origin["on_main"]
    # Live frontmatter says draft (not_shipped) while the cached snapshot says
    # implemented/shipped — a live-vs-snapshot disagreement note must appear in evidence.
    assert any("disagrees with live frontmatter" in line for line in result.evidence)
    # The snapshot is advisory-only: it must NOT flip the verdict away from what the live
    # git+frontmatter disagreement already computed.
    assert result.verdict == "disagreement"


def test_emission_snapshot_absent_when_no_matching_provenance(repo_with_origin, tmp_path, monkeypatch):
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: implemented"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    state_dir = tmp_path / "fixture_state_empty"
    state_dir.mkdir()
    (state_dir / "cockpit-emission.json").write_text(
        json.dumps({"plans": [], "handoffs": []}), encoding="utf-8"
    )
    monkeypatch.setattr(vs_module, "_resolve_state_root", lambda: state_dir)

    result = verify_shipped(
        repo_with_origin["on_main"], plan_path="docs/plans/example.md", repo_root=root
    )
    assert result.emission_snapshot is None
    assert result.verdict == "shipped"


def test_emission_snapshot_none_when_file_absent(repo_with_origin, tmp_path, monkeypatch):
    root = repo_with_origin["root"]
    _write_plan(root, "docs/plans/example.md", ["status: implemented"])
    _git(root, "add", "docs/plans/example.md")
    _git(root, "commit", "-m", "add plan")

    empty_dir = tmp_path / "no_emission_file_here"
    empty_dir.mkdir()
    monkeypatch.setattr(vs_module, "_resolve_state_root", lambda: empty_dir)

    result = verify_shipped(
        repo_with_origin["on_main"], plan_path="docs/plans/example.md", repo_root=root
    )
    assert result.emission_snapshot is None
    assert result.verdict == "shipped"
