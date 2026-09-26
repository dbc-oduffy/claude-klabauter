"""test_prepare_commit_msg_multi_pickup_ambiguity.py -- B2 (F2), CEPEF plan
(docs/plans/2026-09-26-commit-emit-plane-engine-findings.md).

Covers the hook script's own mirror of
`coordinator_core.git.commit_trailers.session_holds_multiple_held_pickups`
(`_session_holds_multiple_held_pickups`, imported not re-derived) and its
wiring into `_resolve_deliverable_id`: a session holding LIVE claims on
two-or-more handoff pickups carrying DISTINCT `deliverable_id`s must OMIT
the trailer at BOTH pickup-derived tiers (the local git-dir tier and the
DoE-git-dir cross-repo tier) rather than stamping whichever pickup happened
last, while the claimed-plan fallback and a single held pickup are
unaffected.

Uses `_load_module()` (the same in-process module-loading fixture
`test_prepare_commit_msg_spawn_budget.py` already established) rather than
spawning the hook as a subprocess -- the property under test is a pure
function's return value, not process exit behaviour, so no new git spawn is
needed (coordinator_core/tests/test_no_new_spawning_tests.py Rule 2/4:
declared here as N/A, this file spawns real `git` only for repo fixture
setup, not for the assertions themselves).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Pre-warm `sys.modules["coordinator_core"]` against THIS repo's own copy
# before any test below chdir()s into a tmp_path fixture outside it -- the
# hook under test resolves `coordinator_core` lazily, by cwd-relative
# bootstrap (`_ensure_claude_klabauter_on_syspath`), so importing it here first (while
# cwd is still this repo) pins the module identity the lazy imports below
# reuse regardless of a later `monkeypatch.chdir`.
import coordinator_core.git.commit_trailers  # noqa: F401

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_HOOK_PATH = _BIN_DIR / "coordinator-prepare-commit-msg.py"

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "coordinator_prepare_commit_msg_b2", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], cwd=root)
    _git(["config", "user.email", "t@example.com"], cwd=root)
    _git(["config", "user.name", "T"], cwd=root)
    return root


@pytest.fixture()
def _live(monkeypatch):
    """Fakes every held-pickup claim as LIVE via the same seam
    `coordinator_core.git.commit_trailers._held_pickup_deliverable_ids`
    reads -- `coordinator_core.liveness.cs_claim_holder_live` -- so this
    file does not need a real session-registry entry."""
    monkeypatch.setattr(
        "coordinator_core.liveness.cs_claim_holder_live", lambda claim_path: True
    )


def _write_shape(repo: Path, sid: str, shape: dict) -> None:
    shape_dir = repo / ".git" / "coordinator-sessions" / sid
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(json.dumps(shape), encoding="utf-8")


def _write_handoff_claim(repo: Path, handoff_relpath: str, sid: str) -> None:
    claim_dir = (
        repo
        / ".git"
        / "coordinator-sessions"
        / "handoff-claims"
        / Path(handoff_relpath).name
    )
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-09-26T00:00:00Z", encoding="utf-8")


def _write_pickup_history(repo: Path, sid: str, entries: list, *, flat_id: str) -> None:
    _write_shape(
        repo,
        sid,
        {"pickup": {"deliverable_id": flat_id}, "pickup_history": entries},
    )


def _git_dir(repo: Path) -> str:
    return str(repo / ".git")


def test_mirror_predicate_is_imported_not_hand_rolled():
    """`_session_holds_multiple_held_pickups` must import B2's engine
    predicate, matching the mirrored-pair convention the plan-claim gate
    already uses -- never a second, hand-rolled liveness walk in this file."""
    text = _HOOK_PATH.read_text(encoding="utf-8")
    assert "session_holds_multiple_held_pickups" in text
    assert "from coordinator_core.git.commit_trailers import (" in text


def test_two_held_pickups_omits_deliverable_id(repo, monkeypatch):
    """Wiring test: `_resolve_deliverable_id` must consult
    `_session_holds_multiple_held_pickups` and skip the pickup tier when it
    answers True -- the ambiguity predicate ITSELF (liveness-per-entry over
    real `pickup_history`/claim-dir fixtures) is exercised end-to-end at the
    engine layer, `coordinator_core/git/test_commit_trailers.py`'s own
    `test_two_held_pickups_code_only_commit_omits_deliverable_id`; patching
    the hook's own predicate seam here isolates the WIRING this file mirrors
    from the liveness mechanics that module already covers."""
    monkeypatch.chdir(repo)
    mod = _load_module()
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-b"}})
    monkeypatch.setattr(mod, "_session_holds_multiple_held_pickups", lambda g, s: True)

    got = mod._resolve_deliverable_id(_git_dir(repo), _SID, paths=[])

    assert got == ""


def test_two_held_pickups_plus_claimed_plan_uses_plan_id(repo, monkeypatch):
    monkeypatch.chdir(repo)
    mod = _load_module()
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-b"}})
    monkeypatch.setattr(mod, "_session_holds_multiple_held_pickups", lambda g, s: True)
    monkeypatch.setattr(
        mod, "_resolve_deliverable_id_from_claimed_plan", lambda: "dlv-plan-value"
    )

    got = mod._resolve_deliverable_id(_git_dir(repo), _SID, paths=[])

    assert got == "dlv-plan-value"


def test_one_pickup_released_one_held_uses_the_held_ones_id(repo, _live, monkeypatch):
    monkeypatch.chdir(repo)
    mod = _load_module()
    _write_pickup_history(
        repo,
        _SID,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_id="dlv-b",
    )
    # Only b's claim dir exists -- a's pickup was released.
    _write_handoff_claim(repo, "state/handoffs/b.md", _SID)

    got = mod._resolve_deliverable_id(_git_dir(repo), _SID, paths=[])

    assert got == "dlv-b"


def test_tier0_artifact_wins_over_multiple_held_pickups(repo, _live, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    mod = _load_module()
    _write_pickup_history(
        repo,
        _SID,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_id="dlv-b",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", _SID)
    _write_handoff_claim(repo, "state/handoffs/b.md", _SID)
    plan_path = repo / "docs" / "plans" / "carries-its-own-id.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        '---\ntitle: x\ndeliverable_id: "dlv-artifact"\n---\n\n# x\n', encoding="utf-8"
    )

    got = mod._resolve_deliverable_id(
        _git_dir(repo), _SID, paths=["docs/plans/carries-its-own-id.md"]
    )

    assert got == "dlv-artifact"


def test_single_held_pickup_unchanged(repo, _live, monkeypatch):
    monkeypatch.chdir(repo)
    mod = _load_module()
    _write_pickup_history(
        repo,
        _SID,
        [{"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"}],
        flat_id="dlv-a",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", _SID)

    got = mod._resolve_deliverable_id(_git_dir(repo), _SID, paths=[])

    assert got == "dlv-a"
