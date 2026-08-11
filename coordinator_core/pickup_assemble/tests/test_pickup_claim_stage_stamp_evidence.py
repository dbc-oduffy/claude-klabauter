"""
coordinator_core.pickup_assemble.tests.test_pickup_claim_stage_stamp_evidence

Purpose: proves `d2` (`archive-stamp-cli claim-handoff`, the durable
frontmatter mutation) is only marked `already_satisfied` when there is real
evidence the stamp landed — a mirror-sourced holder, or a ledger-sourced
holder whose claim is at `apply` stage. A `brief`-stage ledger reservation
(what `acquire_brief_claim` takes a few lines above the read in
`pickup_assemble.brief`'s handoff branch, in the SAME claim dir
`claim_state.resolve_claim_state` reads) is a pre-work lock, not evidence of
a landed write, and must never satisfy `d2`.

The incident (cross-repo/inbox/2026-08-11-example-doctrine-repo-em-pickup-claim-never-
reaches-frontmatter.md): a first-ever pickup of an unclaimed handoff took a
`brief`-stage claim, then read that same claim back as "already landed",
skipped `d2`, and left the claim stranded in the ledger with the frontmatter
mirror still `status: open` / `pickup_ready: true`.

Negative-spec preserved (C11 row 35, ledger-first): an `apply`-stage
self-held ledger claim with an empty/reverted mirror must still satisfy
`d2` — the branch-switch-revert desync fix this module's docstring credits
must not regress.

Run: cd X:/claude-klabauter && python -m pytest
coordinator_core/pickup_assemble/tests/test_pickup_claim_stage_stamp_evidence.py -q
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.pickup_assemble as pa
from coordinator_core.session import claims as claims_mod
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as liveness_mod

# Declared, not excused: this file spawns a real git process, the same
# convention as test_brief_claim_lease.py in this package.
pytestmark = [pytest.mark.spawns_process]


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


def _seed_handoff(repo: Path, name: str, *, claimed_by: str | None = None) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    if claimed_by:
        fm += f"claimed_by: {claimed_by}\nclaimed_at: 2026-01-01T00:00:00Z\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _claim_dir(repo: Path, basename: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename


def _write_claim(repo: Path, basename: str, holder_sid: str, *, stage: str) -> Path:
    cdir = _claim_dir(repo, basename)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    claimed = datetime.fromtimestamp(session_core.now_epoch(), tz=timezone.utc)
    (cdir / "claimed_at").write_text(
        claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    (cdir / "stage").write_text(f"{stage}\n", encoding="utf-8")
    return cdir


@pytest.fixture
def as_session(monkeypatch):
    def _bind(sid: str) -> None:
        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    return _bind


@pytest.fixture
def holder_reads_live(monkeypatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(liveness_mod, "claim_holder_live", lambda *a, **k: value)

    return _set


def _d2(result) -> dict:
    for d in result.decision_object["directives"]:
        if d["id"] == "d2":
            return d
    raise AssertionError("d2 not found in directives")


def test_fresh_pickup_brief_stage_claim_does_not_satisfy_d2(tmp_path, as_session):
    """The memo's exact incident: a first-ever pickup of an unclaimed handoff
    takes only a `brief`-stage reservation. That reservation must not read as
    stamp evidence — `d2` must still be dispatched."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    cdir = _claim_dir(repo, "h1.md")
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF
    assert _d2(result)["already_satisfied"] is False


def test_self_held_apply_stage_ledger_claim_with_empty_mirror_satisfies_d2(
    tmp_path, as_session, holder_reads_live
):
    """C11 row 35 preservation: a session that already stamped the claim on a
    different branch reads as ledger-`apply`-stage held-by-self with a
    reverted (empty) mirror. That must still satisfy `d2`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)
    _write_claim(repo, "h1.md", "sid-a", stage="apply")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert _d2(result)["already_satisfied"] is True


def test_mirror_only_evidence_satisfies_d2(tmp_path, as_session, monkeypatch):
    """A mirror-sourced holder (frontmatter `claimed_by` present, ledger
    claim dead so `resolve_claim_state` degrades it to no ledger claim) plus
    `held_by_self` (an IDENTITY match, independent of liveness — see
    `session.liveness.claim_held_by_me`) is already-landed frontmatter — no
    ledger stage concept applies, and it must satisfy `d2`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", claimed_by="sid-a")
    as_session("sid-a")
    _write_claim(repo, "h1.md", "sid-a", stage="apply")
    # Dead per resolve_claim_state's liveness gate
    # (coordinator_core.claim_state's imported cs_claim_holder_live, distinct
    # from session.liveness.claim_holder_live which held_by_self's own row 2
    # does not gate on) -> ledger degrades to "no ledger claim", so the
    # mirror's claimed_by is the only stamp evidence.
    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert _d2(result)["already_satisfied"] is True
