"""
coordinator_core.pickup_assemble.tests.test_drop_on_a_shipped_baton — the
`confirm-shipped-stand-down` route has to actually release the claim.

Purpose: `/pickup` takes a brief-stage claim UNCONDITIONALLY, ahead of the
`jshipped` judgment point that asks whether to stand down. So a session
answering `confirm-shipped-stand-down` reaches that answer already holding a
claim, and `pickup-assemble drop` is the only route the skill names to
release it. Before this fix `drop` aborted the whole verb on
`cs_unclaim_handoff`'s refusal — that primitive resets `open` +
`ready_to_fire`, correctly out of scope for a shipped record — and returned
`APPLY_EXIT_PARTIAL_MUTATION` with `released: false`, stranding the claim
with no route left. Measured against the real defect: session
ec336d25 stood down on
`state/handoffs/2026-09-01-pickup-assemble-what-a-baton-knows-about.md`,
got exit 4 / `released: false`, and had to reach past the skill to the raw
`session-claim-cli release-artifact` to clear its own claim.

Negative-spec pinned here as much as the positive one: the fix must NOT
relax the primitive's precondition. A shipped baton's frontmatter stays
BYTE-IDENTICAL across the drop — un-shipping it would be a far worse defect
than the stranded claim — and `awaiting_gate`, which is neither terminal nor
unclaimable, must NOT take the skip path.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_on_a_shipped_baton.py -q
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


def _seed_handoff(repo: Path, name: str, deployment_state: str) -> Path:
    fm = (
        'title: "Test Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        "claimed_by: sid-holder\n"
        "claimed_at: 2026-01-01T00:00:00Z\n"
    )
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
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


def _spy_release(monkeypatch) -> list:
    """Records every `release_artifact` call `drop` makes.

    The ledger leg is pinned by the CALL, not by the claim dir vanishing:
    `release_artifact` is holder-identity-checked against ambient session
    identity and no-ops (returning its documented success) under a synthetic
    fixture holder, so `cdir.is_dir()` stays True on the ORDINARY in_flight
    path too — asserting on the dir would pin the fixture's identity plumbing
    rather than this fix. Verified by running the in_flight control against
    unmodified `drop`.
    """
    calls: list = []
    real = pa_apply.release_artifact

    def _spy(class_, basename, **kwargs):
        calls.append((class_, basename))
        return real(class_, basename, **kwargs)

    monkeypatch.setattr(pa_apply, "release_artifact", _spy)
    return calls


@pytest.mark.parametrize("terminal_state", ["shipped", "continued", "closed", "abandoned"])
def test_drop_on_a_terminal_baton_releases_the_ledger_and_leaves_frontmatter(
    tmp_path, monkeypatch, terminal_state
):
    """The whole point: the ledger claim goes, the frontmatter stays.

    Pinned against the unfixed behaviour, which returned
    APPLY_EXIT_PARTIAL_MUTATION / `released: False` with the claim dir still
    on disk for every one of these four states.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(repo, "h-term.md", terminal_state)
    _write_ledger_claim(repo, "h-term.md", "sid-holder")
    monkeypatch.chdir(repo)
    release_calls = _spy_release(monkeypatch)

    before_bytes = handoff.read_bytes()
    before_rev_count = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h-term.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["released"] is True
    assert report["unclaimed"] is None
    assert report["frontmatter_revert"] == "skipped-terminal"
    assert report["deployment_state"] == terminal_state
    assert report["commit_sha"] is None

    assert release_calls == [("handoff", "h-term.md")], (
        "the ledger leg must run — this is the whole point of the terminal path"
    )
    assert handoff.read_bytes() == before_bytes, (
        "a terminal baton's frontmatter must be byte-identical — the fix must not "
        "un-ship the record to get the claim released"
    )
    assert _rev_count(repo) == before_rev_count, (
        "nothing was mutated on disk, so nothing may be committed (and no git spawn "
        "may be paid for)"
    )


def test_awaiting_gate_is_not_terminal_and_still_reaches_the_refusal(tmp_path, monkeypatch):
    """`awaiting_gate` is neither terminal nor in the primitive's
    {in_flight, ready_to_fire} allowlist — the two sets are complements only
    across the states the schema admits. A live baton waiting on a gate must
    keep hitting the loud refusal, never get silently ledger-released by the
    terminal skip."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(repo, "h-gate.md", "awaiting_gate")
    cdir = _write_ledger_claim(repo, "h-gate.md", "sid-holder")
    monkeypatch.chdir(repo)

    before_bytes = handoff.read_bytes()

    exit_code, report = pa_apply.drop(
        "state/handoffs/h-gate.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
    assert report["released"] is False
    assert report.get("frontmatter_revert") != "skipped-terminal"
    assert cdir.is_dir(), "the refusal path must leave the claim in place"
    assert handoff.read_bytes() == before_bytes


def test_in_flight_baton_still_takes_the_ordinary_unclaim_path(tmp_path, monkeypatch):
    """Guard against the skip widening: an ordinary claimed baton must still
    get the full frontmatter revert to `open` + `ready_to_fire`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(repo, "h-live.md", "in_flight")
    _write_ledger_claim(repo, "h-live.md", "sid-holder")
    monkeypatch.chdir(repo)
    release_calls = _spy_release(monkeypatch)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h-live.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["released"] is True
    assert report["unclaimed"] is True
    assert report.get("frontmatter_revert") is None
    assert release_calls == [("handoff", "h-live.md")]

    text = handoff.read_text(encoding="utf-8")
    assert "status: open" in text
    assert "deployment_state: ready_to_fire" in text
