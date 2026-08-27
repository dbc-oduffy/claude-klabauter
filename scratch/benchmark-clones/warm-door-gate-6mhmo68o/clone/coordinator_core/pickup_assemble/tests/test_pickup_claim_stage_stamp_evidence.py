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

The incident (cross-repo/inbox/2026-08-11-doe-claude-em-pickup-claim-never-
reaches-frontmatter.md): a first-ever pickup of an unclaimed handoff took a
`brief`-stage claim, then read that same claim back as "already landed",
skipped `d2`, and left the claim stranded in the ledger with the frontmatter
mirror still `status: open` / `pickup_ready: true`.

Negative-spec preserved (C11 row 35, ledger-first): an `apply`-stage
self-held ledger claim with an empty/reverted mirror must still satisfy
`d2` — the branch-switch-revert desync fix this module's docstring credits
must not regress.

Run from the repo root: python -m pytest
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
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    # Review: coordinator:code-reviewer P2 — this file exercises
    # session_live/claim_holder_live, which route through liveness's
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


def _write_claim(
    repo: Path, basename: str, holder_sid: str, *, stage: str, stamped: bool = False
) -> Path:
    cdir = _claim_dir(repo, basename)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    claimed = datetime.fromtimestamp(session_core.now_epoch(), tz=timezone.utc)
    (cdir / "claimed_at").write_text(
        claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    (cdir / "stage").write_text(f"{stage}\n", encoding="utf-8")
    if stamped:
        (cdir / "stamped").write_text(
            claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
        )
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


def test_reclaim_basis_downgrades_recency_only_evidence_to_liveness_unknown(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    """Root-cause regression for cross-repo/inbox/2026-08-11-market-
    intelligence-em-reclaim-labels-a-live-session-dead-without-checking.md:
    the takeover fired (claim_holder_live -> False, the same boolean
    `claim_artifact` itself acts on), but the ONLY liveness evidence behind
    that False was Layer 2 recency inference (`session_verdict` basis
    `"recency-window"`), never a confirmed-dead process check. The reclaim
    record must say `"holder-liveness-unknown"`, not assert `"dead-holder"`
    on evidence it never had."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (False, "recency-window", 5400)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "holder-liveness-unknown"
    assert record["liveness_basis"] == "recency-window"


def test_reclaim_basis_stays_dead_holder_for_confirmed_dead_process(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    """The one case `"dead-holder"` is still evidence-backed: `session_verdict`
    ran the process-identity (Layer 1) check and it confirmed the holder's
    process gone (`basis == "stable-pid"`, `live is False`)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (False, "stable-pid", None)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "dead-holder"
    assert record["liveness_basis"] == "stable-pid"


def test_reclaim_basis_downgrades_confirmed_live_verdict_to_liveness_unknown(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    """Review: staff-eng F1 — `basis` used to be derived from
    `session_verdict`'s basis string alone, discarding the liveness boolean
    (slot 0). A `(True, "stable-pid", None)` verdict — the process-identity
    check CONFIRMED the holder alive — must never be labelled `"dead-holder"`
    just because the takeover itself fired on `claim_holder_live` (a
    separate, structurally-different computation — reachable via
    `COORDINATOR_SESSION_LAYER1_DISABLE`, which `session_live` honours and
    `session_verdict`/`_verdict_for_sdir` deliberately do not)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (True, "stable-pid", None)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "holder-liveness-unknown"
    assert record["liveness_basis"] == "stable-pid"


def test_reclaim_basis_holder_absent_for_no_evidence_at_all(
    tmp_path, as_session, holder_reads_live
):
    """Review: staff-eng F2 — `session_verdict` returning `None` (no local
    session dir AND no harness-registry record for the holder anywhere) used
    to map to `"dead-holder"`, asserting a process confirmation that never
    ran on the dominant takeover path. Real wiring here (no `session_verdict`
    stub — Review: F3 gap), so the module's real no-local-dir/no-registry
    arm is what produces the `None` this exercises."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-gone", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-gone"
    assert record["basis"] == "holder-absent"
    assert record["liveness_basis"] is None


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
    reverted (empty) mirror. The claim dir carries the `stamped` marker
    (the stamp genuinely landed on the other branch) — that must still
    satisfy `d2`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)
    _write_claim(repo, "h1.md", "sid-a", stage="apply", stamped=True)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert _d2(result)["already_satisfied"] is True


def test_apply_stage_claim_with_refused_stamp_does_not_satisfy_d2(
    tmp_path, as_session, holder_reads_live
):
    """THE regression this module was repaired for (cross-repo/inbox/
    2026-08-13-doe-claude-em-pickup-already-satisfied-masks-a-refused-write.md):
    `apply.py::apply` promotes the claim dir to `apply` stage UNCONDITIONALLY,
    before `d2`'s directive (`archive-stamp-cli claim-handoff`) ever runs — so
    an `apply`-stage claim dir is reachable on a REFUSED stamp attempt exactly
    as much as on a landed one. No `stamped` marker present means no stamp
    ever landed; `d2` must NOT be reported `already_satisfied` on the next
    `brief`, even though the claim dir already reads `apply` stage.

    This is the case that FAILED before the fix (the old fallback read only
    `claim_stage(...) == CLAIM_STAGE_APPLY`, which is True here) and PASSES
    after it (the new fallback reads `claim_stamped(...)`, which is False
    here)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)
    _write_claim(repo, "h1.md", "sid-a", stage="apply", stamped=False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert claims_mod.claim_stage(_claim_dir(repo, "h1.md")) == claims_mod.CLAIM_STAGE_APPLY
    assert _d2(result)["already_satisfied"] is False


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
