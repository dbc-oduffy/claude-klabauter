"""
coordinator_core.pickup_assemble.tests.test_brief_claim_lease

Purpose: proves `pickup-assemble`'s mutual-exclusion claim now lands at
`brief` rather than at `apply`, and that the `brief`-stage claim it lands
self-expires instead of stranding.

The defect (state/bug-backlog/2026-08-10-pickup-claim-lands-at-apply-not-at-
brief-36f1446e3e4b.yaml): the claim was documented as the thing that "stops
two concurrent pickups of the same handoff from both proceeding", but it only
landed during `apply`. Everything between `brief` and `apply` — reading the
artifact, verifying it against HEAD, drafting the reply — was unguarded, and
that is the window where the work happens. On 2026-08-10 two sessions each
took a clean no-claim brief of one memo, each did the verification, and each
shipped a counter memo to a sibling repo; the second `apply` failed correctly,
but the duplicate had already left the repo.

Why a lease and not liveness alone: `liveness.session_live`'s Layer 1 is
PPID-authoritative and does not consult recency once `stable_pid` plus a birth
witness are present, so the existing dead-holder takeover cannot reclaim a
reservation held by a session that briefed an artifact and walked away without
exiting. `BRIEF_CLAIM_LEASE_MINUTES` bounds exactly that case.

Negative-spec coverage (the three constraints this fix must not break):
  - a re-brief by the HOLDER stays `held_by_self` / `granted`, never contention
  - an `apply`-stage claim held by a LIVE session stays untakeable — the lease
    override is brief-stage-only and must not widen
  - `drop` of a brief-stage claim never reaches `handoff_transition._unclaim`
    (in_flight -> ready_to_fire only, fail-loud on anything else)

Run: cd X:/claude-klabauter && python -m pytest
coordinator_core/pickup_assemble/tests/test_brief_claim_lease.py -q
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa
import coordinator_core.pickup_assemble.apply as pa_apply
from coordinator_core.session import claims as claims_mod
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as liveness_mod

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    # Review: coordinator:code-reviewer P2 — this file exercises
    # session_live/claim_holder_live (via holder_reads_live and the
    # real-registry tests below), which route through liveness's
    # per-process registry-snapshot memoization
    # (liveness_mod._cached_registry_lookup). Only
    # coordinator_core/session/tests/test_liveness.py reset that cache;
    # left unreset here, whichever test in a shared pytest worker process
    # first populates it (including one in a DIFFERENT file) leaks its
    # snapshot into every later session_live/claim_holder_live call in this
    # file, silently masking a monkeypatched registry_dir(). Reset before
    # AND after each test so cross-file ordering never matters.
    liveness_mod._registry_snapshot_cache = None
    yield
    liveness_mod._registry_snapshot_cache = None


# ---------------------------------------------------------------------------
# Minimal self-contained git harness (same convention as this package's
# test_claim_state_reads.py — deliberately not imported from the peer-dirty
# coordinator_core/test_pickup_assemble.py).
# ---------------------------------------------------------------------------


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


def _seed_handoff(repo: Path, name: str, *, deployment_state: str = "active") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_memo(repo: Path, name: str, *, status: str = "open") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        f"status: {status}\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Memo\n\nBody.\n", encoding="utf-8")
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
    stage: str | None,
    age_minutes: int,
) -> Path:
    """Hand-build a claim dir at a chosen age and stage. `stage=None` omits
    the file entirely — the pre-split on-disk shape, which must keep reading
    as `apply`."""
    cdir = _claim_dir(repo, class_, basename)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    claimed = datetime.fromtimestamp(
        session_core.now_epoch() - age_minutes * 60, tz=timezone.utc
    )
    (cdir / "claimed_at").write_text(
        claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    if stage is not None:
        (cdir / "stage").write_text(f"{stage}\n", encoding="utf-8")
    return cdir


@pytest.fixture
def as_session(monkeypatch):
    """Bind the caller's session id (tier 1 of `core.resolve_session_id`)."""

    def _bind(sid: str) -> None:
        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    return _bind


@pytest.fixture
def holder_reads_live(monkeypatch):
    """Force the claim layer's liveness verdict. Both `claim_artifact` and
    `compute_claim_grant` route through `liveness.claim_holder_live`, so one
    patch covers the takeover path and the grant path together."""

    def _set(value: bool) -> None:
        monkeypatch.setattr(liveness_mod, "claim_holder_live", lambda *a, **k: value)

    return _set


# ---------------------------------------------------------------------------
# The claim now lands at brief
# ---------------------------------------------------------------------------


def test_brief_takes_a_brief_stage_claim_on_a_handoff(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    cdir = _claim_dir(repo, "handoff", "h1.md")
    assert cdir.is_dir()
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-a"
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF
    assert result.decision_object["gates"]["claim_grant"]["held_by_self"] is True


def test_brief_takes_a_brief_stage_claim_on_a_memo(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    as_session("sid-a")

    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    cdir = _claim_dir(repo, "memo", "m1.md")
    assert cdir.is_dir()
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF


def test_brief_claims_nothing_by_default(tmp_path, as_session):
    """The in-process default stays a pure computation — only the CLI's
    single-artifact path opts in."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    pa.brief("state/handoffs/h1.md", repo_root=repo)

    assert not _claim_dir(repo, "handoff", "h1.md").exists()


def test_the_incident_shape_second_session_is_denied_at_brief(
    tmp_path, as_session, holder_reads_live
):
    """The 2026-08-10 regression test: two sessions brief one memo. The second
    must be told at BRIEF time, before it does any work, not at apply time
    after its counter memo has already shipped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")

    as_session("sid-first")
    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    holder_reads_live(True)
    as_session("sid-second")
    second = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    grant = pa.compute_claim_grant(
        repo, "memo", "m1.md", "cross-repo/inbox/m1.md", cwd=str(repo)
    )
    assert grant["verdict"] == "denied"
    assert grant["holder"] == "sid-first"
    assert grant["held_by_self"] is False
    # The loser never displaces the holder's reservation.
    cdir = _claim_dir(repo, "memo", "m1.md")
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-first"
    assert second.decision_object["artifact"]["path"] == "cross-repo/inbox/m1.md"


def test_a_rebrief_by_the_holder_is_resuming_not_contention(
    tmp_path, as_session, holder_reads_live
):
    """Constraint: `held_by_self` must keep working. Re-typing `/pickup` on an
    artifact this session already holds is resuming."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    again = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    grant = again.decision_object["gates"]["claim_grant"]
    assert grant["verdict"] == "granted"
    assert grant["held_by_self"] is True
    assert grant["reason"] == "you already hold this"


def test_a_live_foreign_holder_denies_a_memo_brief_with_a_standdown(
    tmp_path, as_session, holder_reads_live
):
    """Memo/handoff parity fix (cross-repo/inbox/2026-08-17-doe-claude-em-
    memo-claim-fires-after-the-em-can-already-act.md): a DENIED grant against
    a live foreign holder must halt the SECOND session's brief before the memo
    body is worth reading — a stand-down (`directives: []`), never the full
    actionable object `test_the_incident_shape_second_session_is_denied_at_
    brief` above already proves the grant resolves `denied` for."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")

    as_session("sid-first")
    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    holder_reads_live(True)
    as_session("sid-second")
    second = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    obj = second.decision_object
    assert obj["directives"] == []
    assert second.exit_code == pa.EXIT_BUSINESS_FAIL
    assert obj["gates"]["claim"]["holder"] == "sid-first"
    assert obj["gates"]["claim_grant"]["verdict"] == "denied"
    assert obj["gates"]["claim_grant"]["held_by_self"] is False


def test_a_memo_rebrief_by_the_holder_is_resuming_not_contention(
    tmp_path, as_session, holder_reads_live
):
    """Mirrors `test_a_rebrief_by_the_holder_is_resuming_not_contention`
    (handoff branch) for a memo: a re-brief in the SAME session must come
    back `held_by_self: True`, `already_satisfied: True`, actionable — never
    the stand-down. `compute_claim_grant`'s row 2 already handles this."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    as_session("sid-a")
    holder_reads_live(True)

    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)
    again = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    obj = again.decision_object
    grant = obj["gates"]["claim_grant"]
    assert grant["verdict"] == "granted"
    assert grant["held_by_self"] is True
    assert obj["directives"] != []
    assert obj["directives"][0]["already_satisfied"] is True


def test_an_uncontended_memo_brief_carries_claim_gates(tmp_path, as_session):
    """Key-presence contract the pickup skill consumes (documented alongside
    `held_by_self` / `already_satisfied`, see the handoff branch's equivalent
    gates): `gates.claim` and `gates.claim_grant` must be present on an
    ordinary uncontended memo brief, not just on the handoff branch."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    as_session("sid-a")

    result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    gates = result.decision_object["gates"]
    assert "claim" in gates
    assert "claim_grant" in gates
    assert gates["claim_grant"]["held_by_self"] is True


def test_repeated_rebriefs_are_byte_identical(tmp_path, as_session, holder_reads_live):
    """Steady-state idempotency: once the claim is held, further briefs of the
    same artifact with the same decisions resolve identically."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    second = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    third = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert second.decision_object == third.decision_object
    assert second.exit_code == third.exit_code


# ---------------------------------------------------------------------------
# What a brief must NOT claim
# ---------------------------------------------------------------------------


def test_a_multi_artifact_brief_claims_nothing(tmp_path, as_session):
    """An ` AND `-joined argument is a survey. Locking every baton in it would
    strand each one the EM did not go on to pick up."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _seed_handoff(repo, "h2.md")
    as_session("sid-a")

    pa.brief_multi("state/handoffs/h1.md AND state/handoffs/h2.md", repo_root=repo)

    assert not _claim_dir(repo, "handoff", "h1.md").exists()
    assert not _claim_dir(repo, "handoff", "h2.md").exists()


def test_a_single_artifact_brief_multi_does_claim(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    pa.brief_multi("state/handoffs/h1.md", repo_root=repo)

    assert _claim_dir(repo, "handoff", "h1.md").is_dir()


def test_an_actioned_memo_is_not_claimed(tmp_path, as_session):
    """A terminal artifact has no `apply` to reach, so it has no window to
    guard — locking it would only strand the lock."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md", status="actioned")
    as_session("sid-a")

    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    assert not _claim_dir(repo, "memo", "m1.md").exists()


def test_an_unresolvable_artifact_is_not_claimed(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")

    result = pa.brief("state/handoffs/nope.md", repo_root=repo, claim_at_brief=True)

    assert result.exit_code == pa.EXIT_BUSINESS_FAIL
    assert not _claim_dir(repo, "handoff", "nope.md").exists()


# ---------------------------------------------------------------------------
# The lease — what bounds a stranded reservation
# ---------------------------------------------------------------------------


def test_a_fresh_brief_stage_claim_is_not_expired(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cdir = _write_claim(
        repo, "handoff", "h1.md", "sid-peer", stage="brief", age_minutes=5
    )
    assert claims_mod.brief_lease_expired(cdir) is False


def test_an_expired_brief_lease_is_takeable_from_a_live_holder(
    tmp_path, as_session, holder_reads_live
):
    """The stranding bound. `session_live` would report this holder alive
    forever; the lease is what makes the reservation recoverable."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    cdir = _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-peer",
        stage="brief",
        age_minutes=claims_mod.BRIEF_CLAIM_LEASE_MINUTES + 60,
    )
    holder_reads_live(True)
    assert claims_mod.brief_lease_expired(cdir) is True

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )
    assert grant["verdict"] == "granted-with-warning"
    assert grant["claim_stage"] == claims_mod.CLAIM_STAGE_BRIEF

    as_session("sid-b")
    assert claims_mod.claim_artifact("handoff", "h1.md", cwd=str(repo)) is True
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-b"
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_APPLY


def test_an_apply_stage_claim_is_never_takeable_from_a_live_holder(
    tmp_path, as_session, holder_reads_live
):
    """Negative-spec: the lease override is brief-stage-only. An apply-stage
    claim is backed by a landed frontmatter stamp and real mutation — however
    old it is, a LIVE holder keeps it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    cdir = _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-peer",
        stage="apply",
        age_minutes=claims_mod.BRIEF_CLAIM_LEASE_MINUTES + 600,
    )
    holder_reads_live(True)

    assert claims_mod.brief_lease_expired(cdir) is False

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )
    assert grant["verdict"] == "denied"

    as_session("sid-b")
    assert claims_mod.claim_artifact("handoff", "h1.md", cwd=str(repo)) is False
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-peer"


def test_unclean_prior_holder_fires_on_a_dead_stale_apply_claim(
    tmp_path, holder_reads_live
):
    """`gates.claim_grant.unclean_prior_holder` (pickup/SKILL.md's recovery-
    banner producer): a claims dir naming a not-live holder, past the
    settling window, with no clean `drop()` — the only on-disk shape this
    module can witness as an unclean exit, since `drop()` always removes
    the claims dir on a deliberate release."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-dead",
        stage="apply",
        age_minutes=pa.CLAIM_STALE_AFTER_MINUTES + 5,
    )
    holder_reads_live(False)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted-with-warning"
    assert grant["unclean_prior_holder"] is True
    assert grant["holder"] == "sid-dead"
    assert grant["claim_age_minutes"] >= pa.CLAIM_STALE_AFTER_MINUTES


def test_unclean_prior_holder_is_false_on_a_clean_pickup(tmp_path):
    """Negative-spec: no competing claim dir at all (the shape left behind by
    a deliberate `drop()`, and the shape of a never-claimed artifact) reads
    `unclean_prior_holder: False`, never a guess."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "granted"
    assert grant["unclean_prior_holder"] is False


def test_unclean_prior_holder_is_false_within_the_settling_window(
    tmp_path, holder_reads_live
):
    """Negative-spec: a not-live holder still inside the settling window is
    an inconclusive liveness read (`liveness.py`'s INDETERMINATE contract),
    not evidence of death — `denied`, and `unclean_prior_holder` stays
    `False`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(
        repo, "handoff", "h1.md", "sid-peer", stage="apply", age_minutes=5
    )
    holder_reads_live(False)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "denied"
    assert grant["unclean_prior_holder"] is False


def test_unclean_prior_holder_is_false_when_holder_reads_live(
    tmp_path, holder_reads_live
):
    """Negative-spec: a live holder (however old the claim) is not a death —
    row 3's `denied` stays `unclean_prior_holder: False`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-peer",
        stage="apply",
        age_minutes=pa.CLAIM_STALE_AFTER_MINUTES + 600,
    )
    holder_reads_live(True)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )

    assert grant["verdict"] == "denied"
    assert grant["unclean_prior_holder"] is False


def test_a_stageless_claim_dir_reads_as_apply_stage(tmp_path, holder_reads_live):
    """Back-compat: every claim dir written before the split has no `stage`
    file and must keep its historical, lease-free semantics."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    cdir = _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-peer",
        stage=None,
        age_minutes=claims_mod.BRIEF_CLAIM_LEASE_MINUTES + 600,
    )

    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_APPLY
    assert claims_mod.brief_lease_expired(cdir) is False


def test_an_unreadable_claim_age_is_not_treated_as_expired(tmp_path):
    """An evidence gap is not evidence of expiry."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    cdir = _write_claim(
        repo, "handoff", "h1.md", "sid-peer", stage="brief", age_minutes=999
    )
    (cdir / "claimed_at").write_text("not-a-timestamp\n", encoding="utf-8")

    assert claims_mod.claim_age_minutes(cdir) is None
    assert claims_mod.brief_lease_expired(cdir) is False


def test_a_rebrief_by_the_holder_refreshes_the_lease(tmp_path, as_session, holder_reads_live):
    """A re-brief is evidence of active work, so the lease measures time since
    the holder LAST looked at the artifact — otherwise a long verification
    window ages out a session that never stopped working."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    cdir = _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-a",
        stage="brief",
        age_minutes=claims_mod.BRIEF_CLAIM_LEASE_MINUTES - 1,
    )
    assert claims_mod.claim_age_minutes(cdir) >= claims_mod.BRIEF_CLAIM_LEASE_MINUTES - 1

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert claims_mod.claim_age_minutes(cdir) == 0
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-a"


def test_touch_brief_claim_never_extends_a_peers_lease(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")
    cdir = _write_claim(
        repo, "handoff", "h1.md", "sid-peer", stage="brief", age_minutes=100
    )

    assert claims_mod.touch_brief_claim("handoff", "h1.md", cwd=str(repo)) is False
    assert claims_mod.claim_age_minutes(cdir) >= 100


def test_touch_brief_claim_does_not_touch_an_apply_stage_claim(tmp_path, as_session):
    """An apply-stage claim has no lease to refresh."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")
    cdir = _write_claim(repo, "handoff", "h1.md", "sid-a", stage="apply", age_minutes=100)

    assert claims_mod.touch_brief_claim("handoff", "h1.md", cwd=str(repo)) is False
    assert claims_mod.claim_age_minutes(cdir) >= 100


def test_the_lease_clears_a_realistic_verification_window(tmp_path):
    """Load-norm guard. On this box (50-70 concurrent LLMs) a brief-to-apply
    window containing a dispatched subagent and a test run runs well past an
    hour; a lease that expires inside that window recreates the duplicate-work
    bug the brief-stage claim exists to prevent. Fails loudly if someone tunes
    the default back down to the `CLAIM_STALE_AFTER_MINUTES` magnitude."""
    assert claims_mod.BRIEF_CLAIM_LEASE_MINUTES >= 120


# ---------------------------------------------------------------------------
# A reclaim must not read as a fresh pickup
# ---------------------------------------------------------------------------


def test_a_reclaim_is_distinguishable_from_a_fresh_pickup(
    tmp_path, as_session, holder_reads_live
):
    """Without this, both collapse to row 2 "you already hold this": the
    takeover lands, the grant re-reads the dir, and the EM cannot tell "nobody
    had this" from "I just took this off a session that may still be working
    it"."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(
        repo,
        "handoff",
        "h1.md",
        "sid-peer",
        stage="brief",
        age_minutes=claims_mod.BRIEF_CLAIM_LEASE_MINUTES + 60,
    )
    holder_reads_live(True)
    as_session("sid-b")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    reclaimed = result.decision_object["gates"]["claim_reclaim"]
    assert reclaimed["holder"] == "sid-peer"
    assert reclaimed["basis"] == "expired-brief-lease"
    assert "RECLAIMED from session sid-peer" in result.decision_object["narration"]
    # And it still resolves as ours to work.
    assert result.decision_object["gates"]["claim_grant"]["held_by_self"] is True


def test_a_fresh_pickup_carries_no_reclaim_record(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert "claim_reclaim" not in result.decision_object["gates"]
    assert "RECLAIMED" not in result.decision_object["narration"]


def test_a_rebrief_by_the_holder_carries_no_reclaim_record(
    tmp_path, as_session, holder_reads_live
):
    """Resuming your own work is not a reclaim."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    again = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    assert "claim_reclaim" not in again.decision_object["gates"]
    assert "RECLAIMED" not in again.decision_object["narration"]


def test_an_absent_holder_takeover_is_also_reported_as_a_reclaim(
    tmp_path, as_session, holder_reads_live
):
    """`holder_reads_live` patches only `claim_holder_live`, so `sid-dead` has
    neither a session dir nor a registry record — `session_verdict` returns no
    verdict at all. That is `holder-absent`, NOT `dead-holder`: no process
    check ran (Review: staff-eng F2). A takeover backed by a real
    process-identity check is covered by
    `test_pickup_claim_stage_stamp_evidence.py`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    _write_claim(repo, "memo", "m1.md", "sid-dead", stage="apply", age_minutes=5)
    holder_reads_live(False)
    as_session("sid-b")

    result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)

    reclaimed = result.decision_object["gates"]["claim_reclaim"]
    assert reclaimed["holder"] == "sid-dead"
    assert reclaimed["basis"] == "holder-absent"


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_promote_claim_stage_flips_a_self_held_brief_claim(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")
    cdir = _write_claim(repo, "handoff", "h1.md", "sid-a", stage="brief", age_minutes=1)

    assert claims_mod.promote_claim_stage("handoff", "h1.md", cwd=str(repo)) is True
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_APPLY


def test_promote_claim_stage_never_touches_a_peers_claim(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")
    cdir = _write_claim(
        repo, "handoff", "h1.md", "sid-peer", stage="brief", age_minutes=1
    )

    assert claims_mod.promote_claim_stage("handoff", "h1.md", cwd=str(repo)) is False
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF


def test_promote_claim_stage_is_a_no_op_success_on_an_apply_claim(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")
    _write_claim(repo, "handoff", "h1.md", "sid-a", stage="apply", age_minutes=1)

    assert claims_mod.promote_claim_stage("handoff", "h1.md", cwd=str(repo)) is True


def test_promote_claim_stage_on_an_absent_claim_is_false(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session("sid-a")

    assert claims_mod.promote_claim_stage("handoff", "h1.md", cwd=str(repo)) is False


def test_apply_promotes_the_reservation_left_by_brief(tmp_path, as_session, holder_reads_live):
    """End to end: the reservation `brief` took must not still be carrying a
    lease once `apply` is mutating behind it — otherwise a peer could take the
    claim out from under a session mid-write."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    as_session("sid-a")
    holder_reads_live(True)

    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)
    cdir = _claim_dir(repo, "memo", "m1.md")
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF

    pa_apply.apply("cross-repo/inbox/m1.md", session_id="sid-a", repo_root=repo)

    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_APPLY
    assert claims_mod.brief_lease_expired(cdir) is False


# ---------------------------------------------------------------------------
# Drop — constraint: `handoff_transition._unclaim` must not be widened
# ---------------------------------------------------------------------------


def test_dropping_a_brief_stage_handoff_claim_skips_the_class_inverse(
    tmp_path, as_session, monkeypatch
):
    """A brief-stage claim has no frontmatter stamp behind it. Handing
    `cs_unclaim_handoff` a `deployment_state` that is not `in_flight` would
    fail loud (exit 1, no write) and turn an ordinary put-down into a
    partial-mutation exit — so `drop` must not call it at all."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", deployment_state="active")
    as_session("sid-a")
    pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)
    assert _claim_dir(repo, "handoff", "h1.md").is_dir()

    called: list[str] = []
    monkeypatch.setattr(
        pa_apply, "cs_unclaim_handoff", lambda *a, **k: called.append("unclaim") or 0
    )

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert called == []
    assert report["claim_stage"] == claims_mod.CLAIM_STAGE_BRIEF
    assert report["unclaimed"] is None
    assert not _claim_dir(repo, "handoff", "h1.md").exists()


def test_dropping_a_brief_stage_memo_claim_skips_the_class_inverse(
    tmp_path, as_session, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md")
    as_session("sid-a")
    pa.brief("cross-repo/inbox/m1.md", repo_root=repo, claim_at_brief=True)
    assert _claim_dir(repo, "memo", "m1.md").is_dir()

    called: list[str] = []

    def _boom(*a, **k):
        called.append("revert")
        return {"exit_code": 0, "commit_sha": None}

    monkeypatch.setattr(pa_apply, "cs_release_memo_revert", _boom)

    exit_code, report = pa_apply.drop(
        "cross-repo/inbox/m1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert called == []
    assert report["claim_stage"] == claims_mod.CLAIM_STAGE_BRIEF
    assert not _claim_dir(repo, "memo", "m1.md").exists()


def test_dropping_an_apply_stage_claim_still_runs_the_class_inverse(
    tmp_path, as_session, monkeypatch
):
    """The other half of the same constraint: promotion must not cost `drop`
    its existing behaviour on a real, applied claim."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", deployment_state="in_flight")
    as_session("sid-a")
    _write_claim(repo, "handoff", "h1.md", "sid-a", stage="apply", age_minutes=1)

    called: list[str] = []
    monkeypatch.setattr(
        pa_apply, "cs_unclaim_handoff", lambda *a, **k: called.append("unclaim") or 0
    )

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert called == ["unclaim"]
    assert report["claim_stage"] == claims_mod.CLAIM_STAGE_APPLY
