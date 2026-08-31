"""Regression coverage for guard (a),
state/bug-backlog/2026-08-31-pickup-after-a-close-supersedes-the-new-baton.yaml:
`route_baton_adoption` (the `/pickup` seam) must never mint a successor.

THE CLAIM-HOLDING TRIGGER, NOT THE CLOSE TRIGGER. `second_instance` (added
2026-08-31, session f2fdabbc) is the shape reproduced here: this session
holds ONE baton claimed and in flight -- no `/workstream-complete`, no
close, no ship stamp anywhere -- and briefs a SECOND, unrelated,
`ready_to_fire` baton. Before this fix, `route_baton_adoption` read the
held claim as a "handover" and unconditionally minted a multi-parent
placeholder successor via `baton_assemble.apply.apply("handoff", "", ...)`,
flipping the baton being READ out of the pickup queue by the act of
reading it. This test proves the routing-level fix: `route_baton_adoption`
never reaches `compute_baton_unification_verdict` or
`_unify_into_successor` at all any more, unconditionally.

Spec backlink: `coordinator_core.pickup_assemble.route_baton_adoption`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.baton_assemble.apply as ba_apply
import coordinator_core.pickup_assemble as pa
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external git process for the fixture repo; runs at cadence
# gates, not per-commit, matching the sibling `test_baton_unification.py`.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


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


def _write_baton(repo: Path, name: str, *, deployment_state: str = "active") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        "baton_role: work\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_ledger_claim(repo: Path, basename: str, holder_sid: str) -> None:
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")


SELF_SID = "sid-second-instance"


@pytest.fixture(autouse=True)
def _mock_ledger_liveness(monkeypatch):
    import coordinator_core.claim_state as claim_state_mod

    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _as_self_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", SELF_SID)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    (r / ".git" / "coordinator-sessions" / SELF_SID).mkdir(parents=True, exist_ok=True)
    return r


def test_briefing_an_unclaimed_baton_while_holding_an_unrelated_one_mints_nothing(
    repo, monkeypatch
):
    """The exact `second_instance` shape: session A holds `held.md` claimed,
    NEVER closed, and now briefs (`route_baton_adoption`, called directly —
    this is the routing seam under test, not `brief()`'s full pipeline) an
    unrelated, unclaimed `picked_up.md`. No close anywhere in this fixture.
    Guard (a): nothing is minted, and `held.md`'s frontmatter is completely
    untouched (no claim stamp of its own either — `route_baton_adoption`
    does not touch the HELD baton at all, only the one being adopted)."""
    held = _write_baton(repo, "held.md")
    picked_up = _write_baton(repo, "picked_up.md")
    _seed_ledger_claim(repo, "held.md", SELF_SID)

    held_before = held.read_text(encoding="utf-8")

    apply_calls: list[tuple] = []
    monkeypatch.setattr(
        ba_apply,
        "apply",
        lambda *a, **k: apply_calls.append((a, k)) or (0, {}),
    )

    verdict_calls: list[tuple] = []
    real_verdict = pa.compute_baton_unification_verdict
    monkeypatch.setattr(
        pa,
        "compute_baton_unification_verdict",
        lambda *a, **k: verdict_calls.append((a, k)) or real_verdict(*a, **k),
    )

    target_fm = {"predecessor": "none", "scope": ["coordinator_core/unrelated.py"]}
    pa.route_baton_adoption(repo, "state/handoffs/picked_up.md", dict(target_fm))

    # Nothing was minted at all.
    assert apply_calls == [], (
        "route_baton_adoption must never reach baton_assemble.apply -- "
        f"got {apply_calls!r}"
    )
    # The unification verdict was never even consulted -- the routing
    # decision that used to cause the corruption is gone, not merely its
    # `proceed` arm.
    assert verdict_calls == [], (
        "route_baton_adoption must never consult compute_baton_unification_"
        f"verdict at all -- got {verdict_calls!r}"
    )
    # No successor was written anywhere in the repo.
    successors = [
        p for p in (repo / "state" / "handoffs").glob("*.md")
        if p.name not in ("held.md", "picked_up.md")
    ]
    assert successors == []

    # The HELD baton (A) is byte-identical -- route_baton_adoption never
    # touches it.
    assert held.read_text(encoding="utf-8") == held_before

    # The PICKED-UP baton (B) carries no unification residue -- its own
    # claim stamp is `route_baton_adoption`'s only legitimate business, and
    # this direct call (bypassing `brief()`'s own `acquire_brief_claim`)
    # takes none, so B is untouched too.
    picked_up_text = picked_up.read_text(encoding="utf-8")
    assert "continued_into" not in picked_up_text
    assert "deployment_state: continued" not in picked_up_text
    assert "additional_predecessors" not in picked_up_text
