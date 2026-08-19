"""
coordinator_core.pickup_assemble.tests.test_baton_unification_decision

Purpose: proves C4 (docs/plans/2026-08-19-batons-unify-into-one-successor.md
§ C4, "the held-set and the four-arm refusal — decision only, no artifact
writes") in isolation. `compute_baton_unification_verdict` computes a
verdict and writes nothing; C5 (a separate, later chunk, behind its own
predicate, ON as of `c09345b56`) is the only thing allowed to act on it.

Coverage:
  - the held set resolves via `baton_assemble._resolve_held_handoff_for_
    session` off the DURABLE CLAIM LEDGER, never re-derived from
    frontmatter — a shipped-but-unarchived baton is excluded (disposed,
    not held)
  - a session holding zero handoff claims yields "nothing held", never a
    raised `ValueError`
  - a `degraded` held set still PROCEEDS — `degraded` is a set-ordering
    signal, never an input to this verdict
  - a held baton skipped for an ABSENT `baton_role` axis is COUNTED and
    surfaced (`unstamped_skipped`), never silently dropped
  - all four `pickup_assemble` dispositions (`live-peer` / `live-unrelated`
    / `handover` / `stale-claim`), one test per arm

Run: cd X:/claude-klabauter && python -m pytest
coordinator_core/pickup_assemble/tests/test_baton_unification_decision.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.pickup_assemble as pa

# Spawns a real external git process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Minimal, self-contained git harness — same shape as the sibling
# test_claim_state_reads.py in this package (deliberately not imported from
# the peer-dirty coordinator_core/test_pickup_assemble.py).
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


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deployment_state: str | None = "active",
    baton_role: str | None = "work",
    scope: list[str] | None = None,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
    )
    if deployment_state is not None:
        fm += f"deployment_state: {deployment_state}\n"
    if baton_role is not None:
        fm += f"baton_role: {baton_role}\n"
    if scope:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _make_ledger_claim(repo: Path, basename: str, holder_sid: str) -> Path:
    """A live-shaped `handoff-claims` ledger entry — the branch-independent
    claim record `_resolve_held_handoff_for_session` (D-F) and
    `_resolve_ledger_first_holder` both read, mirroring
    `test_claim_state_reads.py`'s helper of the same name."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    return claim_dir


@pytest.fixture(autouse=True)
def _mock_ledger_liveness(monkeypatch):
    """Every test wants `resolve_claim_state`'s ledger leg to read as LIVE —
    mock the one primitive it consults (`cs_claim_holder_live`), same
    convention as `test_claim_state_reads.py`. Disposition-arm liveness
    (`session.liveness.live_session_verdicts`) is a SEPARATE signal, mocked
    per-test below."""
    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: True)


SELF_SID = "sid-self"


@pytest.fixture(autouse=True)
def _as_self_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", SELF_SID)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


TARGET_FM: dict = {"predecessor": "none", "scope": ["coordinator_core/target.py"]}
TARGET_PATH = "state/handoffs/target.md"


# ---------------------------------------------------------------------------
# (a) the held set — D-F
# ---------------------------------------------------------------------------


def test_zero_handoff_claims_yields_nothing_held_not_valueerror(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # No claim dirs at all — a cross-repo-memo pickup shape.

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["verdict"] == "no-unification"
    assert result["reason"] == "nothing-held"
    assert result["held"]["primary"] is None


def test_shipped_but_unarchived_baton_excluded_from_held_set(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "shipped.md", deployment_state="shipped", baton_role="work")
    _make_ledger_claim(repo, "shipped.md", SELF_SID)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    # It is still HELD (the ledger claim survives ship, per D-F) but excluded
    # from the INHERITABLE set — disposed, not held.
    assert result["held"]["primary"] == "state/handoffs/shipped.md"
    assert result["inheritable"] == []
    assert result["disposed_skipped"] == ["state/handoffs/shipped.md"]
    assert result["verdict"] == "no-unification"
    # The REASON must distinguish this from an empty ledger: batons were held,
    # they were disposed. "nothing-held" here would conflate the two states
    # this verdict exists to keep apart.
    assert result["reason"] == "all-held-disposed"
    assert result["target"] == TARGET_PATH


def test_null_role_axis_counts_as_unstamped_not_as_record(tmp_path, monkeypatch):
    """`baton_role: null` is UNKNOWN, not a stamped `record`. It must land in
    `unstamped_skipped` like an absent key, or a null-valued field reads as a
    deliberate not-work decision that nobody made."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    # `_seed_handoff(baton_role=None)` OMITS the key; this case needs the key
    # PRESENT with a null value, which is a different frontmatter shape.
    seeded = _seed_handoff(repo, "nullrole.md", baton_role=None)
    seeded.write_text(
        seeded.read_text(encoding="utf-8").replace(
            'predecessor: "none"\n', 'predecessor: "none"\nbaton_role: null\n', 1
        ),
        encoding="utf-8",
    )
    _make_ledger_claim(repo, "nullrole.md", SELF_SID)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["inheritable"] == []
    assert result["unstamped_skipped"] == 1
    assert result["reason"] == "unstamped-role-skipped"


def test_degraded_ordering_still_proceeds(tmp_path, monkeypatch):
    """Two claims tying on every ordering leg (simultaneous `/pickup x y`)
    still PROCEEDS — `degraded` is a set-ordering signal, never an input to
    this verdict."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "a.md", baton_role="work")
    _seed_handoff(repo, "b.md", baton_role="work")
    claim_a = _make_ledger_claim(repo, "a.md", SELF_SID)
    claim_b = _make_ledger_claim(repo, "b.md", SELF_SID)
    # Pin identical mtimes on both `claimed_at` files -- real filesystem
    # writes a few microseconds apart would otherwise break the tie on leg 2
    # (`claim_mtime`) even though `claimed_at`'s recorded VALUE (leg 1) is
    # already identical, understating `degraded` for a scenario
    # (simultaneous `/pickup x y`) where nothing actually distinguishes the
    # two claims' recorded order.
    tied_mtime = 1_800_000_000.0
    os.utime(claim_a / "claimed_at", (tied_mtime, tied_mtime))
    os.utime(claim_b / "claimed_at", (tied_mtime, tied_mtime))
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["held"]["degraded"] is True
    assert result["verdict"] == "proceed"
    assert result["disposition"] == "handover"


def test_unstamped_role_axis_skip_is_counted_not_silent(tmp_path, monkeypatch):
    """C7 (the role-stamping chunk) has not landed — every on-disk record is
    absent `baton_role` today. A held baton skipped for that reason must be
    COUNTED, not silently dropped into an indistinguishable 'nothing held'
    verdict."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "unstamped.md", baton_role=None)
    _make_ledger_claim(repo, "unstamped.md", SELF_SID)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["verdict"] == "no-unification"
    assert result["reason"] == "unstamped-role-skipped"
    assert result["unstamped_skipped"] == 1
    assert result["inheritable"] == []


# ---------------------------------------------------------------------------
# (b) the four-arm refusal
# ---------------------------------------------------------------------------


def test_arm_handover_proceeds(tmp_path, monkeypatch):
    """The ordinary case: this session's own already-held baton. Must read
    as a handover (self-inclusion applied), never a live foreign peer."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "held.md", baton_role="work")
    _make_ledger_claim(repo, "held.md", SELF_SID)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["disposition"] == "handover"
    assert result["verdict"] == "proceed"


def test_arm_stale_claim_proceeds(tmp_path, monkeypatch):
    """A held baton whose holder reads not-live — nothing to refuse
    against."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "held.md", baton_role="work")
    _make_ledger_claim(repo, "held.md", SELF_SID)
    monkeypatch.setattr(pa._liveness, "live_session_verdicts", lambda root: {})

    result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)

    assert result["disposition"] == "stale-claim"
    assert result["verdict"] == "proceed"


def test_arm_live_unrelated_refuses(tmp_path, monkeypatch):
    """A live, non-lineage holder whose OWN scope has no overlap with the
    pickup target's scope is still a genuinely live foreign holder —
    absorbing it is what the predecessor's anti-scope forbids outright.

    D-F's held set is, by construction, always THIS session's own claim
    (`list_claims_by_session(self_sid)` — see `_resolve_held_handoff_for_
    session`), so a foreign-held `live-unrelated`/`live-peer` candidate can
    never appear as D-F's own `primary`. This exercises
    `_primary_held_disposition` directly — the classification unit C4's own
    docstring names as "testable in isolation" — rather than routing
    through the (self-only-reachable) held-set resolver, so the arm is
    still proven correct without asserting a production shape that cannot
    occur."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(
        repo, "held.md", baton_role="work", scope=["coordinator_core/unrelated.py"]
    )
    _make_ledger_claim(repo, "held.md", "other-sid")
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {"other-sid": (True, "meta")}
    )

    disposition = pa._primary_held_disposition(
        repo, "state/handoffs/held.md", TARGET_FM
    )

    assert disposition == "live-unrelated"


def test_arm_live_peer_refuses(tmp_path, monkeypatch):
    """A live, non-lineage holder whose scope DOES overlap the pickup
    target's — genuine contention. Same isolation rationale as the
    `live-unrelated` arm above."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(
        repo, "held.md", baton_role="work", scope=["coordinator_core/target.py"]
    )
    _make_ledger_claim(repo, "held.md", "other-sid")
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {"other-sid": (True, "meta")}
    )

    disposition = pa._primary_held_disposition(
        repo, "state/handoffs/held.md", TARGET_FM
    )

    assert disposition == "live-peer"


def test_disposition_to_verdict_mapping_refuses_on_live_peer_and_live_unrelated(
    tmp_path, monkeypatch
):
    """The top-level REFUSE/PROCEED mapping, exercised for all four arms in
    one pass — `compute_baton_unification_verdict` refuses on exactly
    `live-peer`/`live-unrelated` and proceeds on exactly `handover`/
    `stale-claim`, with no fifth value falling through unmapped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "held.md", baton_role="work")
    _make_ledger_claim(repo, "held.md", SELF_SID)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )

    expected = {
        "live-peer": "refuse",
        "live-unrelated": "refuse",
        "handover": "proceed",
        "stale-claim": "proceed",
    }
    for disposition, verdict in expected.items():
        monkeypatch.setattr(pa, "_primary_held_disposition", lambda *a, **k: disposition)
        result = pa.compute_baton_unification_verdict(repo, TARGET_FM, TARGET_PATH)
        assert result["disposition"] == disposition
        assert result["verdict"] == verdict, disposition
