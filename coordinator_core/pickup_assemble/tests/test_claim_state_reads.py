"""
coordinator_core.pickup_assemble.tests.test_claim_state_reads

Purpose: proves each of C11's five migrated `pickup_assemble` readers
(Appendix A rows 17-20 and 35 of
docs/research/2026-08-07-claim-state-desync-across-branch-switch-factfind.md)
resolves a handoff's claim state ledger-first — the exact branch-switch-
revert desync (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-
read.md's own Problem section) this plan exists to fix: the tracked
frontmatter mirror carries NO claim fields (as if the claim were reverted by
a branch switch), while the branch-independent claim ledger
(`.git/coordinator-sessions/handoff-claims/<basename>/`) still holds a live
claim.

Row coverage:
  - row 17 `compute_liveness_signal`
  - row 18 `compute_competing_claim` + `_lineage_related_sessions`
  - row 19 the live-successor candidate scan (`compute_successor_handoffs`)
  - row 20 `classify`
  - row 35 `brief()`'s `self_claimed_in_frontmatter`

Negative-spec: does NOT touch `compute_claim_gate`/`compute_claim_grant`
(Appendix A row 34) — that pair is already ledger-only and is not part of
this chunk (see the C11 dispatch brief's "Already correct" section).

Run: cd X:/claude-klabauter && python -m pytest
coordinator_core/pickup_assemble/tests/test_claim_state_reads.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.pickup_assemble as pa

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Minimal, self-contained git harness (deliberately NOT imported from the
# peer-dirty coordinator_core/test_pickup_assemble.py — see the C11 dispatch
# brief's Out-of-scope section).
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
    status: str = "open",
    deployment_state: str | None = "active",
    predecessor: str | None = None,
    scope: list[str] | None = None,
    extra_fm: str = "",
) -> Path:
    """Writes a handoff with NO claim fields at all (`claimed_by`,
    `consumed_by`, `picked_up_by` all absent) — the reverted-mirror shape
    every test in this file pairs with a still-live ledger claim dir."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
    )
    fm += f'predecessor: "{predecessor}"\n' if predecessor else 'predecessor: "none"\n'
    if deployment_state is not None:
        fm += f"deployment_state: {deployment_state}\n"
    if scope:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    if extra_fm:
        fm += extra_fm
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _make_ledger_claim(
    repo: Path, basename: str, holder_sid: str, *, stamped: bool = False
) -> Path:
    """Writes a live-shaped ledger claim dir for `state/handoffs/<basename>`
    — the branch-independent half of the split that survives a branch
    switch untouched. Liveness itself is mocked separately
    (`cs_claim_holder_live`/`session_live`), never derived from real process
    state, per this repo's existing test convention.

    `stamped` (default False) controls whether the claim dir also carries
    the `session.claims.mark_claim_stamped` durable marker — the fact
    `pickup_assemble`'s `stamp_evidence` fallback now reads (cross-repo/inbox/
    2026-08-13-doe-claude-em-pickup-already-satisfied-masks-a-refused-write.md),
    in place of the old (unsound) `claim_stage(...) == CLAIM_STAGE_APPLY`
    inference. A bare ledger claim with no `stamped` marker is exactly the
    "reservation taken, stamp never confirmed" state that invariant was
    fixed to stop misreading as landed evidence."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    if stamped:
        (claim_dir / "stamped").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    return claim_dir


@pytest.fixture(autouse=True)
def _mock_ledger_liveness(monkeypatch):
    """Every test in this file wants its ledger claim to read as LIVE —
    mock the one primitive `claim_state.resolve_claim_state` consults
    (`cs_claim_holder_live`) rather than standing up a real live session."""
    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: True)


# ---------------------------------------------------------------------------
# Row 17 — compute_liveness_signal
# ---------------------------------------------------------------------------


def test_compute_liveness_signal_ledger_only_claim_fires(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _seed_handoff(repo, "h1.md")
    _make_ledger_claim(repo, "h1.md", "peer-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")

    fm = {"status": "open", "deployment_state": "active"}
    fired = pa.compute_liveness_signal(
        repo, fm, artifact_path="state/handoffs/h1.md", self_session_id="self-sid"
    )

    assert fired is True


def test_compute_liveness_signal_no_ledger_no_mirror_does_not_fire(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    # No ledger claim dir at all — genuinely never-picked-up.

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: True)

    fm = {"status": "open", "deployment_state": "active"}
    fired = pa.compute_liveness_signal(
        repo, fm, artifact_path="state/handoffs/h1.md", self_session_id="self-sid"
    )

    assert fired is False


# ---------------------------------------------------------------------------
# Row 18 — _lineage_related_sessions + compute_competing_claim
# ---------------------------------------------------------------------------


def test_lineage_related_sessions_ledger_only_predecessor_claim(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "predecessor.md")
    _make_ledger_claim(repo, "predecessor.md", "predecessor-sid")

    child_fm = {"predecessor": "state/handoffs/predecessor.md"}
    related = pa._lineage_related_sessions(repo, child_fm)

    assert "predecessor-sid" in related


def test_compute_competing_claim_ledger_only_sibling_is_a_candidate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")

    assert result["verdict"] == "live-peer"
    holders = {c["claimed_by"] for c in result["candidates"]}
    assert "sibling-sid" in holders


def test_compute_competing_claim_send_message_address_resolves(tmp_path, monkeypatch):
    """`state/handoffs/2026-08-13-session-owner-reachability-registry.md`
    § 3; `cross-repo/inbox/2026-08-13-doe-claude-em-peer-roster-doctrine-
    reply.md` § Counter 2 — a candidate whose holder resolves gets a
    populated `send_message_address`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({"sibling-sid": "claude-klabauter-57 [b2afcd]"}, True),
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")

    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    assert by_holder["sibling-sid"]["send_message_address"] == "claude-klabauter-57 [b2afcd]"
    assert by_holder["sibling-sid"]["send_message_address_unavailable_reason"] is None


def test_compute_competing_claim_send_message_address_empty_when_unresolvable(
    tmp_path, monkeypatch
):
    """A holder the resolver cannot place, WHILE messaging is available
    box-wide, gets `""` (never absent) and a `None` reason -- the
    peer-specific arm. `verdict`/`disposition` are unaffected."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({}, True),
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")

    assert result["verdict"] == "live-peer"
    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    assert by_holder["sibling-sid"]["send_message_address"] == ""
    assert by_holder["sibling-sid"]["send_message_address_unavailable_reason"] is None


def test_compute_competing_claim_send_message_address_none_when_messaging_unavailable(
    tmp_path, monkeypatch
):
    """`cross-repo/inbox/2026-08-15-example-retrieval-repo-em-peer-messaging-gate-off-
    vs-proven-round-trip.md` § "Smaller, concrete: the empty-string
    rendering" -- when NO peer on the box can have an address (the
    harness's cross-session inbox is unbound), the field must render as
    `None` with `send_message_address_unavailable_reason ==
    "peer-messaging-unavailable"`, distinguishable from the peer-specific
    `""` arm above. `verdict`/`disposition` are unaffected."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({}, False),
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")

    assert result["verdict"] == "live-peer"
    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    assert by_holder["sibling-sid"]["send_message_address"] is None
    assert (
        by_holder["sibling-sid"]["send_message_address_unavailable_reason"]
        == "peer-messaging-unavailable"
    )


def test_compute_competing_claim_address_resolution_failure_leaves_brief_unchanged(
    tmp_path, monkeypatch
):
    """Negative-spec: advisory only, never load-bearing. An import failure
    or a raising resolver must degrade every `send_message_address` to `""`
    without touching `verdict`, `disposition`, or `holder_live`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )

    def _raise(sids):
        raise RuntimeError("simulated resolution failure")

    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability", _raise
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    baseline_fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({"sibling-sid": "claude-klabauter-57 [b2afcd]"}, True),
    )
    baseline = pa.compute_competing_claim(repo, baseline_fm, "state/handoffs/self.md")
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability", _raise
    )
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")

    assert result["verdict"] == baseline["verdict"]
    by_holder = {c["claimed_by"]: dict(c) for c in result["candidates"]}
    baseline_by_holder = {c["claimed_by"]: dict(c) for c in baseline["candidates"]}
    for sid, candidate in by_holder.items():
        assert candidate["send_message_address"] == ""
        assert candidate["send_message_address_unavailable_reason"] is None
        other = dict(baseline_by_holder[sid])
        other.pop("send_message_address")
        other.pop("send_message_address_resolved_at")
        this = dict(candidate)
        this.pop("send_message_address")
        this.pop("send_message_address_resolved_at")
        assert this == other


def test_compute_competing_claim_send_message_address_resolved_at_is_stamped(
    tmp_path, monkeypatch
):
    """`send_message_address_resolved_at` is the staleness-detection field
    the EM asked for: the address is not durable identity, so a reader of a
    PERSISTED brief must be able to see WHEN it was resolved rather than
    trust it silently. Stamped on every candidate, resolved or not."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md", scope=["coordinator_core/foo.py"])
    _seed_handoff(repo, "sibling.md", scope=["coordinator_core/foo.py"])
    _make_ledger_claim(repo, "sibling.md", "sibling-sid")

    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {"sibling-sid": (True, "meta")},
    )
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({"sibling-sid": "claude-klabauter-57 [b2afcd]"}, True),
    )

    fm = {"predecessor": "none", "scope": ["coordinator_core/foo.py"]}
    before = pa.datetime.now(pa.timezone.utc)
    result = pa.compute_competing_claim(repo, fm, "state/handoffs/self.md")
    after = pa.datetime.now(pa.timezone.utc)

    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    stamp = by_holder["sibling-sid"]["send_message_address_resolved_at"]
    parsed = pa.datetime.fromisoformat(stamp)
    assert before <= parsed <= after


# ---------------------------------------------------------------------------
# Row 19 — live-successor candidate scan (compute_successor_handoffs)
# ---------------------------------------------------------------------------


def test_compute_successor_handoffs_ledger_only_in_flight_survives(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md")
    _seed_handoff(
        repo,
        "successor.md",
        status="open",
        deployment_state="in_flight",
        predecessor="state/handoffs/self.md",
    )
    _make_ledger_claim(repo, "successor.md", "successor-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "successor-sid")

    result = pa.compute_successor_handoffs(repo, "state/handoffs/self.md")

    kinds_by_holder = {c["claimed_by"]: c["kind"] for c in result["candidates"]}
    assert kinds_by_holder.get("successor-sid") == "in_flight_live"


def test_compute_successor_handoffs_send_message_address_resolves(tmp_path, monkeypatch):
    """Same field, same shared resolution core as `compute_competing_claim`
    (state/handoffs/2026-08-13-session-owner-reachability-registry.md § 3)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md")
    _seed_handoff(
        repo,
        "successor.md",
        status="open",
        deployment_state="in_flight",
        predecessor="state/handoffs/self.md",
    )
    _make_ledger_claim(repo, "successor.md", "successor-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "successor-sid")
    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        lambda sids: ({"successor-sid": "claude-klabauter-11 [aaaaaa]"}, True),
    )

    result = pa.compute_successor_handoffs(repo, "state/handoffs/self.md")

    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    assert by_holder["successor-sid"]["send_message_address"] == "claude-klabauter-11 [aaaaaa]"
    assert by_holder["successor-sid"]["send_message_address_resolved_at"]
    assert by_holder["successor-sid"]["send_message_address_unavailable_reason"] is None


def test_compute_successor_handoffs_address_resolution_failure_leaves_kind_unchanged(
    tmp_path, monkeypatch
):
    """Negative-spec: a raising resolver degrades `send_message_address` to
    `""` without touching `kind`/`status`/`deployment_state`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "self.md")
    _seed_handoff(
        repo,
        "successor.md",
        status="open",
        deployment_state="in_flight",
        predecessor="state/handoffs/self.md",
    )
    _make_ledger_claim(repo, "successor.md", "successor-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "successor-sid")

    def _raise(sids):
        raise RuntimeError("simulated resolution failure")

    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability", _raise
    )

    result = pa.compute_successor_handoffs(repo, "state/handoffs/self.md")

    by_holder = {c["claimed_by"]: c for c in result["candidates"]}
    assert by_holder["successor-sid"]["send_message_address"] == ""
    assert by_holder["successor-sid"]["send_message_address_unavailable_reason"] is None
    assert by_holder["successor-sid"]["kind"] == "in_flight_live"


# ---------------------------------------------------------------------------
# Row 20 — classify
# ---------------------------------------------------------------------------


def test_classify_ledger_only_claim_with_dropped_deployment_state(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # deployment_state=None -> the field is entirely absent from the
    # frontmatter, the exact "revert also dropped deployment_state" shape
    # fact-find row 20 names.
    path = _seed_handoff(repo, "h1.md", deployment_state=None)
    _make_ledger_claim(repo, "h1.md", "peer-sid")

    fm_text = "status: open\npredecessor: \"none\"\n"
    classification = pa.classify(path, fm_text, repo)

    assert classification == "handoff"


def test_classify_no_ledger_claim_and_dropped_deployment_state_stays_ambiguous(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _seed_handoff(repo, "h1.md", deployment_state=None)
    # No ledger claim dir at all.

    fm_text = "status: open\npredecessor: \"none\"\n"
    classification = pa.classify(path, fm_text, repo)

    assert classification == "ambiguous"


# ---------------------------------------------------------------------------
# Row 35 — brief()'s self_claimed_in_frontmatter
# ---------------------------------------------------------------------------


def test_brief_self_claimed_ledger_only_stamped_marks_d2_already_satisfied(
    tmp_path, monkeypatch
):
    """Row 35, repaired for cross-repo/inbox/2026-08-13-doe-claude-em-pickup-
    already-satisfied-masks-a-refused-write.md: ledger-sourced stamp evidence
    now requires the durable `stamped` marker (`session.claims.
    mark_claim_stamped`), not merely `claim_stage(...) == CLAIM_STAGE_APPLY`
    — a stage the claim reaches unconditionally, pre-directive, regardless of
    whether the frontmatter stamp attempt that follows actually lands. This
    test still pins the thing it was built to pin (a ledger-only holder, with
    a reverted mirror, satisfying `d2`) — it just sources that evidence from
    the marker a confirmed-successful stamp writes, mirroring
    `test_pickup_claim_stage_stamp_evidence.py`'s own fixture shape."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Reverted mirror: status stays "open", no claimed_by at all — as if a
    # prior pass's `d2` stamp never survived a branch switch.
    _seed_handoff(repo, "h1.md", status="open")
    _make_ledger_claim(repo, "h1.md", "self-sid", stamped=True)

    # This session holds the ledger claim (row 34's own primitive, untouched
    # by this chunk).
    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: True)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})

    directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
    assert directives_by_id["d2"]["already_satisfied"] is True


def test_brief_self_claimed_ledger_only_unstamped_does_not_mark_d2_already_satisfied(
    tmp_path, monkeypatch
):
    """Companion to the stamped case above, pinning the new invariant's
    negative side: a ledger-only holder with NO `stamped` marker (a
    reservation taken, or a stamp attempt that was refused — the memo's exact
    incident shape) must NOT satisfy `d2`, even though the claim dir has no
    `stage` file at all (`claim_stage` therefore reads `CLAIM_STAGE_APPLY` by
    its own no-stage-file-means-apply default) — proving the fallback no
    longer draws a stamp-landed inference from stage alone."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", status="open")
    _make_ledger_claim(repo, "h1.md", "self-sid", stamped=False)

    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: True)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})

    directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
    assert directives_by_id["d2"]["already_satisfied"] is False


def test_brief_self_claimed_frontmatter_still_works_pre_revert(tmp_path, monkeypatch):
    """Non-regression: the pre-existing mirror-satisfied case (status:
    claimed already landed in the mirror) still satisfies d2 even with no
    ledger claim dir at all — the OR the fix introduces must never make the
    frontmatter half stop counting."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", status="claimed", extra_fm="claimed_by: self-sid\n")

    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})

    directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
    # held_by_self is False here (no live-self ledger claim), so the AND's
    # first conjunct alone determines the outcome — this asserts the second
    # conjunct (claim_state.holder resolution) does not regress the
    # mirror-only read when the mirror itself already says "claimed".
    assert directives_by_id["d2"]["already_satisfied"] is False


# ---------------------------------------------------------------------------
# Observability drift (Review: code-reviewer, Finding 2) —
# _resolve_ledger_first_holder logs a warning on a resolve_claim_state
# failure, matching the sibling migration in
# review_trail_write._scan_workstream, rather than swallowing it silently.
# ---------------------------------------------------------------------------


def test_resolve_ledger_first_holder_logs_on_resolve_claim_state_failure(
    tmp_path, monkeypatch, caplog
):
    import logging

    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")

    def _boom(*a, **k):
        raise RuntimeError("simulated resolve_claim_state failure")

    # `_resolve_ledger_first_holder` relocated to `coordinator_core.session.
    # work_state` (chunk C1a of the fleet-work-state plan); `pa` re-exports it
    # by name. Both the patch target and the logger have to follow it there:
    # patching `pa.resolve_claim_state` rebinds a name in the WRONG module's
    # globals, so the relocated function never sees it, and the warning is now
    # emitted on that module's own logger.
    from coordinator_core.session import work_state as ws

    monkeypatch.setattr(ws, "resolve_claim_state", _boom)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.session.work_state"):
        holder = pa._resolve_ledger_first_holder(repo, "state/handoffs/h1.md", {})

    assert holder is None
    assert any(
        "claim_state resolution failed" in rec.message for rec in caplog.records
    ), f"expected a resolve_claim_state-failure warning; got: {[r.message for r in caplog.records]}"


def test_resolve_ledger_first_holder_still_falls_back_to_picked_up_by_on_failure(
    tmp_path, monkeypatch
):
    """Fail-closed behavior is unchanged by the logging addition: a
    resolve_claim_state failure still falls through to the picked_up_by
    mirror fallback rather than raising or losing the fallback value."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")

    def _boom(*a, **k):
        raise RuntimeError("simulated resolve_claim_state failure")

    monkeypatch.setattr(pa, "resolve_claim_state", _boom)

    holder = pa._resolve_ledger_first_holder(
        repo, "state/handoffs/h1.md", {"picked_up_by": "fallback-sid"}
    )

    assert holder == "fallback-sid"


def test_claim_grant_denied_live_reason_harness_registry_is_strong_arm():
    """AC9: a `harness-registry` basis (C2's new, stronger-than-stable-pid
    liveness evidence) renders in the strong arm — `live (harness-registry)`
    — rather than falling through to the `basis unknown` hedge that made
    registry-covered denials MORE hedged than before (staff-eng Finding 0).
    `age_sec` is `None` on this basis per the pinned Layer-1 contract."""
    evidence = {
        "liveness_basis": "harness-registry",
        "last_activity_age_sec": None,
        "recent_paths": [],
        "scope_overlap": None,
    }

    reason = pa._claim_grant_denied_live_reason("some-sid", evidence)

    assert reason == "held by some-sid — live (harness-registry)"
    assert "basis unknown" not in reason
    assert "may be a stale claim" not in reason
