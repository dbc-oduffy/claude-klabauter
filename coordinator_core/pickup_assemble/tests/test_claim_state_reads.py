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
  - row 20 `classify`
  - row 35 `brief()`'s `self_claimed_in_frontmatter`

Row 19 (`compute_successor_handoffs`) coverage was removed 2026-08-21
(chunk C3, docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md):
the function itself was deleted outright as an unread field-consumption
finding, so its regression tests dissolved with the code.

Negative-spec: does NOT touch `compute_claim_gate`/`compute_claim_grant`
(Appendix A row 34) — that pair is already ledger-only and is not part of
this chunk (see the C11 dispatch brief's "Already correct" section).

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_claim_state_reads.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.pickup_assemble as pa
from coordinator_core.win_portability import no_console_creationflags

_HANDOFF_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontmatter"
    / "schemas"
    / "handoff.schema.json"
)


def test_classify_does_not_gate_on_deployment_state_the_schema_never_required():
    """Producer/verifier pin (cross-repo/archive/2026-08-18-market-
    intelligence-em-lint-valid-handoff-classifies-ambiguous.md): `classify()`
    must never require `deployment_state` for handoff routing while
    `handoff.schema.json`'s `required` set still omits it — otherwise a
    lint-valid handoff can silently strand as `ambiguous` again. If the
    schema's `required` set ever grows to include `deployment_state`, this
    assertion is the prompt to revisit `classify()`'s deliberate non-gate,
    not something this test should silently start passing on stale grounds."""
    import json

    schema = json.loads(_HANDOFF_SCHEMA_PATH.read_text(encoding="utf-8"))
    required = set(schema.get("required", []))
    assert "deployment_state" not in required, (
        "handoff.schema.json now requires deployment_state — "
        "classify()'s deliberate non-gate on it (see its own docstring) "
        "needs revisiting, not silent staleness"
    )

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
    **no_console_creationflags())


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


def test_compute_liveness_signal_fires_for_a_holder_live_in_a_sibling_repo(
    tmp_path, monkeypatch
):
    """The cross-repo holder (bug-backlog 2026-08-13-session-live-s-repo-
    scoping-makes-a-live-...). `session_live` is repo-scoped and answers False
    for a holder the harness registry confirms is live with its cwd in a
    sibling repo. Read through this reaper that rendered as "no live holder",
    so the brief nudged the EM to take over a claim whose holder was alive and
    reachable — takeover-bait, which is why the failure direction matters more
    than the frequency."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _make_ledger_claim(repo, "h1.md", "peer-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        pa._liveness,
        "session_verdict",
        lambda sid, cwd=None: (
            True,
            "harness-registry-elsewhere",
            "/some/sibling-repo",
        )
        if sid == "peer-sid"
        else None,
    )

    fm = {"status": "open", "deployment_state": "active"}
    fired = pa.compute_liveness_signal(
        repo, fm, artifact_path="state/handoffs/h1.md", self_session_id="self-sid"
    )

    assert fired is True


def test_compute_liveness_signal_elsewhere_arm_does_not_widen_the_in_repo_answer(
    tmp_path, monkeypatch
):
    """The elsewhere arm is ADDITIVE, and this pins that it stays additive.

    `session_live` and `session_verdict` are not the same computation in-repo —
    `session_live` honours the `COORDINATOR_SESSION_LAYER1_DISABLE` rollback
    lever and `_verdict_for_sdir` deliberately does not — so accepting any
    truthy verdict here would silently re-answer the in-repo case rather than
    only adding the cross-repo one. A `stable-pid` verdict reading live while
    `session_live` reads not-live is exactly that structural disagreement, and
    it must NOT fire this signal."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _make_ledger_claim(repo, "h1.md", "peer-sid")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        pa._liveness,
        "session_verdict",
        lambda sid, cwd=None: (True, "stable-pid", None),
    )

    fm = {"status": "open", "deployment_state": "active"}
    fired = pa.compute_liveness_signal(
        repo, fm, artifact_path="state/handoffs/h1.md", self_session_id="self-sid"
    )

    assert fired is False


def _claim_dir_holding(tmp_path, sid):
    cdir = tmp_path / "claims" / "artifact"
    cdir.mkdir(parents=True)
    (cdir / "session_id").write_text(sid, encoding="utf-8")
    return cdir


def test_claim_holder_live_or_elsewhere_sees_a_holder_working_in_a_sibling_repo(
    tmp_path, monkeypatch
):
    """The claim layer's half of the same defect, and the worse half.

    `claim_holder_live` resolves the claim dir's `session_id` and hands it to
    the repo-scoped `session_live`, so a holder live in a sibling repo reads
    not-live and the claim reads takeable. Unlike `compute_liveness_signal`'s
    advisory signal, this one feeds `compute_claim_grant` — the MUTATING path.
    The cost of getting it wrong is a live peer's claim taken out from under
    them mid-work, not a misleading brief."""
    cdir = _claim_dir_holding(tmp_path, "peer-sid")

    monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda c, cwd=None: False)
    monkeypatch.setattr(
        pa._liveness,
        "session_verdict",
        lambda sid, cwd=None: (
            True,
            "harness-registry-elsewhere",
            "/some/sibling-repo",
        )
        if sid == "peer-sid"
        else None,
    )

    assert pa._claim_holder_live_or_elsewhere(cdir, str(tmp_path), "peer-sid") is True


def test_claim_holder_live_or_elsewhere_does_not_widen_the_in_repo_answer(
    tmp_path, monkeypatch
):
    """Additive, not a re-answer. A `stable-pid` verdict reading live while
    `claim_holder_live` reads not-live is the in-repo structural disagreement
    (`COORDINATOR_SESSION_LAYER1_DISABLE`), and it must not resurrect a claim
    the claim layer has decided is takeable."""
    cdir = _claim_dir_holding(tmp_path, "peer-sid")

    monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda c, cwd=None: False)
    monkeypatch.setattr(
        pa._liveness,
        "session_verdict",
        lambda sid, cwd=None: (True, "stable-pid", None),
    )

    assert pa._claim_holder_live_or_elsewhere(cdir, str(tmp_path), "peer-sid") is False


def test_claim_holder_live_or_elsewhere_never_consults_the_registry_when_live(
    tmp_path, monkeypatch
):
    """The primitive stays FIRST and authoritative — a live in-repo holder
    short-circuits, so the registry is never reached. Pins that this is an
    added arm rather than a replacement."""
    cdir = _claim_dir_holding(tmp_path, "peer-sid")
    consulted = []

    monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda c, cwd=None: True)

    def _boom(sid, cwd=None):
        consulted.append(sid)
        raise AssertionError("session_verdict must not be reached when live in-repo")

    monkeypatch.setattr(pa._liveness, "session_verdict", _boom)

    assert pa._claim_holder_live_or_elsewhere(cdir, str(tmp_path), "peer-sid") is True
    assert consulted == []


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


def test_classify_no_deployment_state_and_no_ledger_claim_still_classifies_handoff(tmp_path):
    """2026-08-31 fix (cross-repo/archive/2026-08-18-example-market-data-repo-em-
    lint-valid-handoff-classifies-ambiguous.md): `deployment_state` is
    absent from `handoff.schema.json`'s `required` set, so `classify()`
    must not gate on it — a lint-valid handoff with neither a
    `deployment_state` nor a ledger claim still classifies as `handoff`,
    it does not strand as `ambiguous`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _seed_handoff(repo, "h1.md", deployment_state=None)
    # No ledger claim dir at all.

    fm_text = "status: open\npredecessor: \"none\"\n"
    classification = pa.classify(path, fm_text, repo)

    assert classification == "handoff"


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
