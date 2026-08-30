"""
coordinator_core.pickup_assemble.tests.test_drop_holder_gate — C5 falsifier
LEG 1 (docs/plans/2026-08-30-drop-releases-a-claim-it-never-held.md).

Purpose: `pickup-assemble drop` invoked by a session that is NOT the recorded
holder of a stamped (apply-stage) claim must mutate NOTHING and say so. The
first falsifier attempt on this criterion read GREEN by testing
`release_artifact` in isolation and by grepping for commit sites only within
`coordinator_core/pickup_assemble/` — a false green, since `drop` lives in
`coordinator_core/pickup_assemble/apply.py` and composes real primitives from
`session.claims`/`archive_stamp`. This suite drives the REAL `drop()` entry
point end to end against a real git repo: no primitive mocked, no commit-site
grep, so a regression that reintroduces the split-state defect (frontmatter
stripped while the claim ledger denies, or a stray commit landing behind a
denied drop) fails here.

Coverage:
  - a non-holder drop returns `APPLY_EXIT_CLAIM_DENIED`, `released` is never
    `True`/absent-not-True and `unclaimed` is never `True`
  - the seeded handoff's frontmatter bytes are BYTE-IDENTICAL across the call
  - no commit lands in the repo (`git rev-list --count HEAD` unchanged)

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_holder_gate.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble.apply as pa_apply

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


_HANDOFF_FM = (
    'title: "Test Handoff"\n'
    "created: 2026-01-01\n"
    "branch: work/test/2026-01-01\n"
    "status: claimed\n"
    'predecessor: "none"\n'
    "deployment_state: in_flight\n"
    "claimed_by: sid-holder\n"
    "claimed_at: 2026-01-01T00:00:00Z\n"
)


def _seed_claimed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{_HANDOFF_FM}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_ledger_claim(repo: Path, basename: str, holder_sid: str) -> Path:
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (cdir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    return cdir


def _rev_count(repo: Path) -> str:
    return _git(repo, "rev-list", "--count", "HEAD").stdout.strip()


def test_non_holder_drop_mutates_nothing_and_says_so(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h1.md")
    _write_ledger_claim(repo, "h1.md", "sid-holder")

    before_bytes = handoff.read_bytes()
    before_rev_count = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_not_holder"
    assert report.get("released") is not True
    assert report.get("unclaimed") is not True

    after_bytes = handoff.read_bytes()
    after_rev_count = _rev_count(repo)

    assert after_bytes == before_bytes, "frontmatter must be byte-identical across a denied drop"
    assert after_rev_count == before_rev_count, "a denied drop must land no commit"
    # The claim ledger dir itself must survive untouched too — the holder
    # never asked to release it.
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
    assert cdir.is_dir()
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-holder"
