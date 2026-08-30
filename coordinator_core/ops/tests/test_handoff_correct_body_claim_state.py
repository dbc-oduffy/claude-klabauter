"""
coordinator_core.ops.tests.test_handoff_correct_body_claim_state

Coverage for the C6a widening of `handoff.correct_body`'s ownership gate
(docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md, chunk
C6a): the holder arm now resolves via `coordinator_core.claim_state.
resolve_claim_state` (ledger-first, mirror fallback) rather than the raw
`claimed_by` frontmatter field alone.

Coverage:
  (a) a desynced baton — the claim ledger holds a live claim, the tracked
      `claimed_by` mirror is empty/open — still admits the TRUE ledger
      holder through the holder arm (AC5's motivating gap: the sanctioned
      body-correction door must not refuse the one session actually
      entitled to use it).
  (b) the SAME desynced ledger claim still REFUSES a different calling
      session — widening visibility must not relax who is let through.

Spec backlink: coordinator_core/ops/handoff_correct_body.py's ownership gate
               (module docstring "Ownership gate" section).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest import mock

import pytest

# Import guard — MUST precede any test so @register_op fires first.
import coordinator_core.ops.handoff_correct_body  # noqa: F401 — fires @register_op

from coordinator_core import claim_state as _claim_state_module
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_correct_body import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "handoff.correct_body"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_correct_body @register_op did not fire"
)

_HOLDER_SESSION = "b1111111-2222-3333-4444-555555555555"
_OTHER_SESSION = "c1111111-2222-3333-4444-555555555555"


def _run(coro):
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
    **no_console_creationflags(),
)

    _git("init", "-b", "main")
    _git("config", "user.email", "correct-body-claim-state-test@claude-klabauter.test")
    _git("config", "user.name", "Correct Body Claim State Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


_UNRELATED_AUTHOR_SESSION = "d1111111-2222-3333-4444-555555555555"


def _seed_desynced_claimed_handoff(repo: Path, name: str, *, authoring_session: str = "") -> Path:
    """A `status: claimed` handoff with NO `claimed_by` on the mirror — the
    branch-switch-revert desync shape this chunk targets. Only a claim
    ledger entry (written separately by the caller) carries the true
    holder. `authoring_session`, when given, names a THIRD session — neither
    the ledger holder nor the (b)-test's calling session — so a refused
    non-holder call is proven to fall through to the "neither holder nor
    author" arm rather than the absent-authorship arm."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ['title: "Desynced Handoff"', "status: claimed"]
    if authoring_session:
        fm_lines.append(f"authoring_session: {authoring_session}")
    fm_text = "\n".join(fm_lines)
    content = f"---\n{fm_text}\n---\n\n# Handoff body.\n\nThe count was 29.\n"
    path.write_text(content, encoding="utf-8")
    return path


def _write_ledger_claim(repo: Path, handoff_name: str, holder_session_id: str) -> Path:
    """Write a live claim-ledger entry — the SAME dir shape
    `coordinator_core.claim_state.handoff_claim_dir` derives:
    <common_dir>/coordinator-sessions/handoff-claims/<handoff_name>/session_id."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(holder_session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")
    return claim_dir


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _set_calling_session(monkeypatch, value: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", value)


# ---------------------------------------------------------------------------
# (a) True holder, ledger-only claim (mirror desynced/empty) — ADMITTED
# ---------------------------------------------------------------------------


def test_desynced_ledger_only_claim_admits_true_holder(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_desynced_claimed_handoff(repo, "2026-08-07-desynced.md")
    _write_ledger_claim(repo, hpath.name, _HOLDER_SESSION)
    _set_calling_session(monkeypatch, _HOLDER_SESSION)

    with mock.patch.object(_claim_state_module, "cs_claim_holder_live", return_value=True):
        result = _run(_handler(
            {
                "handoff_path": str(hpath),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.",
            },
            repo_root=repo / ".git",
        ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["ownership_basis"] == "holder"
    assert "The count was 25." in hpath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (b) SAME desynced ledger claim, DIFFERENT calling session — still REFUSED
# ---------------------------------------------------------------------------


def test_desynced_ledger_only_claim_refuses_different_session(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_desynced_claimed_handoff(
        repo, "2026-08-07-desynced-other.md", authoring_session=_UNRELATED_AUTHOR_SESSION
    )
    _write_ledger_claim(repo, hpath.name, _HOLDER_SESSION)
    _set_calling_session(monkeypatch, _OTHER_SESSION)

    with mock.patch.object(_claim_state_module, "cs_claim_holder_live", return_value=True):
        result = _run(_handler(
            {
                "handoff_path": str(hpath),
                "old_string": "The count was 29.",
                "new_string": "The count was 25.",
            },
            repo_root=repo / ".git",
        ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    # _OTHER_SESSION is neither the ledger-resolved holder (_HOLDER_SESSION)
    # nor the authoring_session (_UNRELATED_AUTHOR_SESSION) — proves the
    # widened holder-arm lookup did NOT let a different session through the
    # same ledger claim that admits its true holder in test (a) above.
    assert "neither the claim" in result["error"]
    assert _HOLDER_SESSION in result["error"]
    assert "The count was 29." in hpath.read_text(encoding="utf-8"), (
        "refused correction must write nothing"
    )
