"""The baton's own acceptance criterion for
docs/plans/2026-09-11-a-brief-reads-without-succeeding-what-it-reads.md,
driven through the live seam (`pickup_brief.brief`), where
`test_route_baton_adoption_never_unifies` drives the `pickup_assemble` copy.

The session holds a claim on `held.md` (the old trigger shape) and briefs a
live `awaiting_gate` `target.md` twice. Neither record's bytes change and no
successor appears. The held-set assertion pins designed pickup behaviour: a
single-path brief enrols the record, per `docs/wiki/baton-lifecycle.md`
§ "A single-path brief is a pickup, not a read".
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pb
from coordinator_core import baton_assemble as ba
from coordinator_core.session import claims as claims_mod
from coordinator_core.session import liveness as liveness_mod
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)
from coordinator_core.session import record_homes

_TARGET_REL = Path(record_homes.record_path("", "handoffs", "target.md")).as_posix()

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

SID = "sid-double-brief"


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


def _seed(repo: Path, name: str, extra_fm: str) -> Path:
    path = Path(record_homes.record_path(str(repo), "handoffs", name))
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8", newline="\n")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _claims_root(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions" / "handoff-claims"


def test_briefing_a_live_baton_twice_mints_nothing_and_enrols_it(
    tmp_path, as_session, holder_reads_live
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    as_session(SID)
    holder_reads_live(True)

    held = _seed(repo, "held.md", "deployment_state: active\nbaton_role: work\n")
    target = _seed(
        repo,
        "target.md",
        "deployment_state: awaiting_gate\nblocked_by: [some-id]\nbaton_role: work\n",
    )
    held_dir = _claims_root(repo) / "held.md"
    held_dir.mkdir(parents=True, exist_ok=True)
    (held_dir / "session_id").write_text(f"{SID}\n", encoding="utf-8")
    (held_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")

    held_before = held.read_bytes()
    target_before = target.read_bytes()

    for _ in range(2):
        pb.brief(_TARGET_REL, repo_root=repo, claim_at_brief=True)
        cdir = _claims_root(repo) / "target.md"
        assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF

    assert held.read_bytes() == held_before
    assert target.read_bytes() == target_before
    assert sorted(p.name for p in Path(record_homes.home_dir(str(repo), "handoffs")).iterdir()) == [
        "held.md",
        "target.md",
    ]

    primary, additional, _degraded = ba._resolve_held_handoff_for_session(
        repo, allow_standalone=True, session_id=SID
    )
    named = [Path(primary).name] if primary else []
    named += [Path(a).name for a in additional]
    assert "target.md" in named
