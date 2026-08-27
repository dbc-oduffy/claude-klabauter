"""
coordinator_core.pickup_assemble.tests.test_claim_rule

Purpose: proves `compute_claim_grant`'s R4 claim rule
(docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md C2), which
replaced the former five-row, age-keyed truth table:

    no claimant                        -> GRANT
    claimant, session live             -> REJECT
    claimant, not live or unresolvable -> GRANT_WITH_WARN

Age is NOT an input to this decision and `claimed_at` is never read as part
of it — the prior table's settling-window/staleness cells (a live claimant
treated as takeable once its claim was old enough) granted a baton away
from a demonstrably running session, which the PM killed. Two of the seven
branches below exist specifically to prove that: a live claimant with an
ancient claim still resolves REJECT (not "old enough to take"), and a
claimant recorded with no `claimed_at` file at all resolves purely off
liveness, never off a missing/unreadable age.

Liveness is resolved via `coordinator_core.session.liveness.claim_holder_live`,
itself keyed on `coordinator_core.session.core.stable_pid_alive` against the
holder's `meta.json` — never the harness registry's raw pid or status field.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_claim_rule.py -q
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa
from coordinator_core.session import liveness as liveness_mod

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    # Same cross-file leak guard as test_brief_claim_lease.py (Review:
    # coordinator:code-reviewer P2) — this file exercises claim_holder_live
    # via a real (monkeypatched) liveness module, which routes through
    # liveness's per-process registry-snapshot memoization.
    liveness_mod._registry_snapshot_cache = None
    yield
    liveness_mod._registry_snapshot_cache = None


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


def _seed_handoff(repo: Path, name: str) -> Path:
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
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _claim_dir(repo: Path, class_: str, basename: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename


def _write_claim(
    repo: Path,
    class_: str,
    basename: str,
    holder_sid: str,
    *,
    age_minutes: int | None,
) -> Path:
    """Hand-build a claim dir naming `holder_sid`. `age_minutes=None` omits
    `claimed_at` entirely — one of the "no real baton" branches proving age
    is not consulted even when unreadable."""
    cdir = _claim_dir(repo, class_, basename)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    if age_minutes is not None:
        from coordinator_core.session import core as session_core

        claimed = datetime.fromtimestamp(
            session_core.now_epoch() - age_minutes * 60, tz=timezone.utc
        )
        (cdir / "claimed_at").write_text(
            claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
        )
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    return cdir


@pytest.fixture
def holder_reads_live(monkeypatch):
    """Force `compute_claim_grant`'s liveness resolution — R4's ONE registry
    check — without a real second process/session."""

    def _set(value: bool | Exception) -> None:
        def _fake(*_a, **_k):
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(liveness_mod, "claim_holder_live", _fake)

    return _set


@pytest.fixture
def as_self(monkeypatch):
    """Make `compute_claim_grant`'s self-identity check resolve True — the
    claimant IS this session, orthogonal to the R4 three-outcome table."""

    def _set(value: bool) -> None:
        monkeypatch.setattr(liveness_mod, "claim_held_by_me", lambda *a, **k: value)

    return _set


def test_no_claim_dir_grants(tmp_path, as_self):
    """Row 1: no claimant at all -> GRANT."""
    as_self(False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted"
    assert grant["holder"] is None
    assert grant["held_by_self"] is False


def test_claim_dir_with_no_recorded_session_id_grants(tmp_path, as_self):
    """Row 1 variant: a claim dir exists but names no holder -> still
    "no claimant", GRANT."""
    as_self(False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    cdir = _claim_dir(repo, "handoff", "h1.md")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted"
    assert grant["holder"] is None


def test_claimant_is_self_grants_held_by_self(tmp_path, as_self):
    """Identity pre-check: the recorded claimant IS this session -> GRANT,
    `held_by_self: True` — a session can never contend with itself."""
    as_self(True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-self", age_minutes=5)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted"
    assert grant["held_by_self"] is True
    assert grant["holder"] == "sid-self"


def test_live_peer_claimant_is_denied(tmp_path, as_self, holder_reads_live):
    """Row 2: a DIFFERENT claimant, session live -> REJECT (`denied`)."""
    as_self(False)
    holder_reads_live(True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-peer", age_minutes=5)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "denied"
    assert grant["holder"] == "sid-peer"
    assert grant["holder_live"] is True
    assert grant["unclean_prior_holder"] is False


def test_not_live_peer_claimant_is_granted_with_warning(
    tmp_path, as_self, holder_reads_live
):
    """Row 3: a DIFFERENT claimant, not live -> GRANT_WITH_WARN
    (`granted-with-warning`), `unclean_prior_holder: True`."""
    as_self(False)
    holder_reads_live(False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-dead", age_minutes=5)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted-with-warning"
    assert grant["holder"] == "sid-dead"
    assert grant["holder_live"] is False
    assert grant["unclean_prior_holder"] is True


def test_unresolvable_liveness_is_granted_with_warning(
    tmp_path, as_self, holder_reads_live
):
    """Row 3 variant: liveness could not be resolved at all (an
    OSError/ValueError from the liveness check) collapses to the SAME
    outcome as not-live — an evidence gap is never treated as proof of
    liveness."""
    as_self(False)
    holder_reads_live(OSError("meta.json unreadable"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-unknown", age_minutes=5)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted-with-warning"
    assert grant["holder"] == "sid-unknown"


def test_live_but_ancient_claim_still_denied(tmp_path, as_self, holder_reads_live):
    """No-real-baton exercise (1/2): age is NOT an input. A live claimant
    with an extremely old claim still resolves REJECT, never
    GRANT_WITH_WARN — the killed age-clause behaviour must not resurface."""
    as_self(False)
    holder_reads_live(True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-peer", age_minutes=10_000)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "denied"


def test_claimant_with_no_claimed_at_resolves_on_liveness_only(
    tmp_path, as_self, holder_reads_live
):
    """No-real-baton exercise (2/2): a claim dir with no `claimed_at` file
    at all (unreadable/missing age) still resolves purely off liveness —
    never treated as evidence of staleness or of freshness."""
    as_self(False)
    holder_reads_live(False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "handoff", "h1.md", "sid-dead", age_minutes=None)
    assert not (_claim_dir(repo, "handoff", "h1.md") / "claimed_at").exists()

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted-with-warning"
    assert grant["holder"] == "sid-dead"
