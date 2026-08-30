"""
coordinator_core.pickup_assemble.tests.test_unification_raise_is_clean — C5
falsifier LEG 2 (docs/plans/2026-08-30-drop-releases-a-claim-it-never-held.md).

Purpose: `_unify_into_successor`'s PARENT-READINESS PRECONDITION (checked
BEFORE the mint) must raise with NO commit landed anywhere — counting commits
made transitively through `baton_assemble.apply`, not only ones made directly
inside `coordinator_core/pickup_assemble/`. The first falsifier attempt read
GREEN by grepping for commit sites only within `pickup_assemble` itself; a
real regression here would be a precondition check that raises AFTER the mint
already committed a successor via `baton_assemble.apply`, leaving a committed
half-unified tree behind a raised exception.

This suite drives the REAL `_unify_into_successor` against a real git repo
with `baton_assemble.apply.apply` left UNMOCKED (unlike
`test_baton_unification.py`, which fakes it for its own routing-seam
coverage): the precondition here is engineered to fail BEFORE that primitive
would ever be reached, so leaving it real is what proves it was never
reached, rather than merely trusting a stub was never called.

Coverage:
  - a held parent leg that is neither claimed nor shipped fails the
    precondition and raises `RuntimeError` naming the unready parent
  - the repo's commit count (`git rev-list --count HEAD`) is UNCHANGED across
    the raise
  - `baton_assemble.apply.apply` is never invoked (a spy proves the mint call
    itself, not just its effects, never happened)
  - Defect A (empty parent list): `verdict["held"]` resolving to ZERO
    parents, with `apply`'s own claim-ledger derivation also empty, is
    refused with its own message rather than falling through to the mint
    (the empty-comprehension-is-falsy door)
  - Defect B (verdict/apply parent-set disagreement): a leg present in
    `apply`'s own claim-ledger derivation (`_resolve_held_handoff_for_session`,
    the SAME resolver `apply`'s `brief()` calls) but ABSENT from
    `verdict["held"]` is still caught by the precondition — not just the legs
    named in the verdict

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_unification_raise_is_clean.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.baton_assemble.apply as ba_apply
import coordinator_core.pickup_assemble as pa
from coordinator_core.session.claims import claim_handoff

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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


def _seed_unclaimed_handoff(repo: Path, name: str) -> Path:
    """A held-but-not-ready parent: `baton_role: work`, NOT claimed
    (no ledger claim dir), NOT shipped (`deployment_state: active`) —
    `claimed_or_shipped_at_path` reads this `False`, so it is `unready`."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
        "baton_role: work\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _rev_count(repo: Path) -> str:
    return _git(repo, "rev-list", "--count", "HEAD").stdout.strip()


def test_precondition_raise_leaves_no_commit_and_never_reaches_the_mint(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_unclaimed_handoff(repo, "a.md")

    mint_called = []
    original_apply = ba_apply.apply

    def _spy_apply(*args, **kwargs):
        mint_called.append((args, kwargs))
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(ba_apply, "apply", _spy_apply)

    verdict = {
        "held": {"primary": "state/handoffs/a.md", "additional": [], "degraded": False},
    }

    before_rev_count = _rev_count(repo)

    with pytest.raises(RuntimeError, match="baton unification precondition failed"):
        pa._unify_into_successor(repo, verdict)

    after_rev_count = _rev_count(repo)

    assert after_rev_count == before_rev_count, (
        "the precondition raise must leave no commit behind — counted via "
        "the repo's total commit count, not a grep for commit sites confined "
        "to pickup_assemble"
    )
    assert mint_called == [], "the precondition must raise BEFORE baton_assemble.apply is ever called"


def test_empty_parent_list_refused_before_the_mint(tmp_path, monkeypatch):
    """Defect A: `verdict["held"]` resolves to ZERO parents and this
    session's own claim ledger (consulted for Defect B parity) also holds
    zero handoff claims -- the empty-comprehension-is-falsy door must be
    refused with its own message, never fall through to the mint."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "test-session-empty-parents")

    mint_called = []
    original_apply = ba_apply.apply

    def _spy_apply(*args, **kwargs):
        mint_called.append((args, kwargs))
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(ba_apply, "apply", _spy_apply)

    verdict = {"held": {"primary": None, "additional": [], "degraded": False}}

    before_rev_count = _rev_count(repo)

    with pytest.raises(RuntimeError, match="zero parent legs resolved"):
        pa._unify_into_successor(repo, verdict)

    after_rev_count = _rev_count(repo)

    assert after_rev_count == before_rev_count, (
        "an empty-parent-list raise must leave no commit behind"
    )
    assert mint_called == [], "the empty-parent-list precondition must raise BEFORE the mint is ever called"


def test_apply_derived_parent_absent_from_verdict_is_still_caught(tmp_path, monkeypatch):
    """Defect B: a leg present in `apply`'s OWN claim-ledger derivation
    (`_resolve_held_handoff_for_session`, the same resolver `brief()` calls)
    but ABSENT from `verdict["held"]` must still be checked -- not reaching
    the mint unchecked just because the verdict's own held-set never named
    it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_unclaimed_handoff(repo, "ledger-only.md")

    session_id = "test-session-ledger-only"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)
    assert claim_handoff("ledger-only.md", cwd=str(repo)) is True

    mint_called = []
    original_apply = ba_apply.apply

    def _spy_apply(*args, **kwargs):
        mint_called.append((args, kwargs))
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(ba_apply, "apply", _spy_apply)

    # The verdict's OWN held-set names nothing -- only the claim ledger
    # (read by `_unify_into_successor` via the same resolver `apply` uses)
    # knows about `ledger-only.md`.
    verdict = {"held": {"primary": None, "additional": [], "degraded": False}}

    before_rev_count = _rev_count(repo)

    with pytest.raises(RuntimeError, match="baton unification precondition failed"):
        pa._unify_into_successor(repo, verdict)

    after_rev_count = _rev_count(repo)

    assert after_rev_count == before_rev_count, (
        "a leg caught only via apply's own derived set must still raise before any commit"
    )
    assert mint_called == [], "a leg absent from verdict['held'] but present in apply's derived set must still block the mint"
