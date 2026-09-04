"""
coordinator_core.pickup_assemble.tests.test_drop_holder_gate — C5 falsifier
LEG 1 (docs/plans/2026-08-30-drop-releases-a-claim-it-never-held.md).

Purpose: `pickup-assemble drop` invoked by a session that is NOT the recorded
holder of a stamped (apply-stage) claim must mutate NOTHING and say so. The
first falsifier attempt on this criterion read GREEN by testing
`release_artifact` in isolation and by grepping for commit sites only within
`coordinator_core/pickup_assemble/` — a false green, since `drop` lives in
`coordinator_core/pickup_assemble/apply.py` and composes real primitives from
`session.claims`/`archive_stamp`. This suite drives the REAL `drop()` entry
point end to end against a real git repo: no primitive mocked, no commit-site
grep, so a regression that reintroduces the split-state defect (frontmatter
stripped while the claim ledger denies, or a stray commit landing behind a
denied drop) fails here.

Coverage:
  - a non-holder drop returns `APPLY_EXIT_CLAIM_DENIED`, `released` is never
    `True`/absent-not-True and `unclaimed` is never `True`
  - the seeded handoff's frontmatter bytes are BYTE-IDENTICAL across the call
  - no commit lands in the repo (`git rev-list --count HEAD` unchanged)

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_holder_gate.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble.apply as pa_apply

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


_HANDOFF_FM = (
    'title: "Test Handoff"\n'
    "created: 2026-01-01\n"
    "branch: work/test/2026-01-01\n"
    "status: claimed\n"
    'predecessor: "none"\n'
    "deployment_state: in_flight\n"
    "claimed_by: sid-holder\n"
    "claimed_at: 2026-01-01T00:00:00Z\n"
)


def _seed_claimed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{_HANDOFF_FM}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
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


def test_non_holder_drop_mutates_nothing_and_says_so(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h1.md")
    _write_ledger_claim(repo, "h1.md", "sid-holder")

    before_bytes = handoff.read_bytes()
    before_rev_count = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_not_holder"
    assert report.get("released") is not True
    assert report.get("unclaimed") is not True

    after_bytes = handoff.read_bytes()
    after_rev_count = _rev_count(repo)

    assert after_bytes == before_bytes, "frontmatter must be byte-identical across a denied drop"
    assert after_rev_count == before_rev_count, "a denied drop must land no commit"
    # The claim ledger dir itself must survive untouched too — the holder
    # never asked to release it.
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
    assert cdir.is_dir()
    assert (cdir / "session_id").read_text(encoding="utf-8").strip() == "sid-holder"


def test_brief_stage_lock_beside_a_stamped_artifact_takes_the_holder_gated_path(tmp_path, monkeypatch):
    """Finding 3 (code-reviewer, coordinator:code-reviewer.a02e400c836530b5b):
    a brief-stage LOCK beside an apply-stage FRONTMATTER STAMP is not a
    brief-stage claim. Before the fix, `drop` trusted the lock dir alone,
    took the lock-release-only arm, and reported `released: true` while the
    artifact still read `claimed_by: <holder>` on disk — the exact silent
    defect `state/bug-backlog/2026-08-31-claim-handoff-takes-a-foreign-claim-
    and-drop-releases-nothing.yaml` records. The stamp is the authority: a
    non-holder drop attempt against this shape must be DENIED, the same as
    any other apply-stage claim, not silently no-op-released.

    Pinned against the unfixed behaviour: it returns APPLY_EXIT_OK with
    `released: True` and `claim_stage: "brief"` instead of denying.

    `monkeypatch.chdir(repo)`: the new arm's `read_frontmatter_field(
    artifact_path_value, ...)` reads `artifact["path"]`, which is
    root-relative (`live_path = root / artifact["path"]` elsewhere in this
    module) — a real `drop` invocation runs with cwd already at the repo
    root, which this test must reproduce for the relative read to land on
    the seeded file rather than silently missing it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h2.md")
    cdir = _write_ledger_claim(repo, "h2.md", "sid-holder")
    (cdir / "stage").write_text("brief\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    before_bytes = handoff.read_bytes()
    before_rev_count = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h2.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_not_holder"
    assert report.get("released") is not True

    after_bytes = handoff.read_bytes()
    after_rev_count = _rev_count(repo)
    assert after_bytes == before_bytes, "a denied drop must not touch the stamp"
    assert after_rev_count == before_rev_count, "a denied drop must land no commit"


def test_brief_stage_lock_beside_a_stamped_artifact_releases_for_the_real_holder(tmp_path, monkeypatch):
    """Sibling positive case: the holder-gated path this fix falls through to
    must still actually work for the real holder — the stamp gets unclaimed,
    not left behind under a lock-release-only no-op."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h3.md")
    cdir = _write_ledger_claim(repo, "h3.md", "sid-holder")
    (cdir / "stage").write_text("brief\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h3.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["released"] is True
    text = handoff.read_text(encoding="utf-8")
    assert "claimed_by: sid-holder" not in text


def test_a_raising_frontmatter_read_does_not_fall_back_to_lock_only_release(tmp_path, monkeypatch):
    """Finding 4 (nit). `read_frontmatter_field` itself never raises (its own
    docstring: "Never raises"), so the `except Exception: _fm_holder = ""`
    arm is defensive code with no live caller today — but a bare `""` there
    reads exactly like "confirmed unstamped" and silently reinstates the
    lock-release-only bug the moment anything ever does raise (a future
    caller change, a monkeypatched test double, an import-time failure).
    Exercised directly via monkeypatch since production `read_frontmatter_field`
    cannot be made to raise through its own contract.

    Pinned against the unfixed behaviour (`_fm_holder = ""` on except): it
    returns APPLY_EXIT_OK with `released: True` instead of denying."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h4.md")
    cdir = _write_ledger_claim(repo, "h4.md", "sid-holder")
    (cdir / "stage").write_text("brief\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    import coordinator_core.ops.read_frontmatter_field as rff_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("transient read failure")

    monkeypatch.setattr(rff_mod, "read_frontmatter_field", _boom)

    before_bytes = handoff.read_bytes()

    exit_code, report = pa_apply.drop(
        "state/handoffs/h4.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report.get("released") is not True
    assert handoff.read_bytes() == before_bytes


def test_denied_drop_labels_the_identity_the_gate_decided_on(tmp_path):
    """The holder gate is not an authorization boundary — an explicit
    `--session-id` is taken on the caller's word, per `claim_held_by_me`'s
    ratified contract. What is owed instead is telling an asserted identity
    apart from a resolved one downstream, so a reader of the report can see
    which kind of decision the refusal rests on. Without this label the two
    are indistinguishable and the gate reads as stronger than it is."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_claimed_handoff(repo, "h1.md")
    _write_ledger_claim(repo, "h1.md", "sid-holder")

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["identity_source"] == "explicit-session-id"
