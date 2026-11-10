"""Handoff apply: the session's handoff claim follows the baton the run mints.

Entrypoint test: drives `apply` (brief -> directives) and executes only the
claim directives through the real `session-claim-cli` `main`, in-process.
"""

from __future__ import annotations

import importlib.util

from coordinator_core.session_hub_path import hub_for_common
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
import coordinator_core.session.claims as claims
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _write_artifact

SESSION = "sess-current-0001"
OTHER = "sess-other-0002"
_CLI = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "session-claim-cli.py"


def _claim_cli():
    spec = importlib.util.spec_from_file_location("session_claim_cli_under_test", _CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _seed_claim(repo: Path, sid: str, basename: str) -> None:
    d = hub_for_common(repo / ".git") / "handoff-claims" / basename
    d.mkdir(parents=True)
    (d / "session_id").write_text(sid + "\n", encoding="utf-8")
    (d / "stage").write_text("apply\n", encoding="utf-8")


def _held(repo: Path, sid: str) -> list[str]:
    return sorted(
        b for c, b in claims.list_claims_by_session(sid, cwd=str(repo)) if c == "handoff-claims"
    )


def _baton(repo: Path, name: str, *extra: str) -> Path:
    return _write_artifact(repo / "state" / "handoffs" / name, ["kind: session-handoff", *extra])


def _continued(into: str) -> list[str]:
    return [
        "claimed_at: 2026-10-28T10:00:00Z",
        "claimed_by: test-session",
        "deployment_state: continued",
        f"continued_into: {into}",
    ]


@pytest.fixture
def run_claim_directives(monkeypatch):
    """Replaces directive execution with: run each `session-claim-cli`
    directive through the CLI's real `main`; everything else is skipped."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.syspath_prepend(str(_CLI.parent))
    cli = _claim_cli()
    ran: list[list[str]] = []

    def _execute(directives, judgment_points, repo_root, *, decisions=None, composition_budget=None):
        for d in directives:
            if d.get("cli") == "session-claim-cli" and d["args"][0] == "follow-handoff-claim":
                ran.append(list(d["args"]))
                assert cli.main(list(d["args"])) == 0
        return ba_apply.APPLY_EXIT_OK, {"landed": [d["id"] for d in directives]}

    monkeypatch.setattr(ba_apply, "_execute_directives", _execute)
    monkeypatch.setattr(ba_apply, "_scoped_commit", lambda *a, **k: None)
    return ran


def test_apply_moves_the_session_claim_onto_the_minted_successor(tmp_path, monkeypatch, run_claim_directives):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    _baton(repo, "pred.md", "claimed_at: 2026-10-28T10:00:00Z", "claimed_by: test-session")
    _seed_claim(repo, SESSION, "pred.md")

    code, report = ba_apply.apply(
        "handoff", "", session_id=SESSION, repo_root=repo, title="Follow the claim"
    )

    assert code == ba_apply.APPLY_EXIT_OK, report
    minted = [a[3] for a in run_claim_directives]
    assert len(minted) == 1 and minted[0].endswith(".md") and minted[0] != "pred.md"
    assert run_claim_directives[0][2] == "pred.md"
    assert _held(repo, SESSION) == minted


def test_apply_moves_the_grandparent_claim_when_the_predecessor_resolved_to_the_leaf(
    tmp_path, monkeypatch, run_claim_directives
):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    _baton(repo, "grand.md", *_continued("leaf.md"))
    _baton(repo, "leaf.md", "claimed_at: 2026-10-28T10:00:00Z", "claimed_by: test-session")
    _seed_claim(repo, SESSION, "grand.md")

    code, report = ba_apply.apply(
        "handoff", "", session_id=SESSION, repo_root=repo, title="Follow the claim"
    )

    assert code == ba_apply.APPLY_EXIT_OK, report
    assert run_claim_directives[0][2] == "grand.md"
    assert "grand.md" not in _held(repo, SESSION)
    assert _held(repo, SESSION) == [run_claim_directives[0][3]]


def test_apply_leaves_another_sessions_claim_alone(tmp_path, monkeypatch, run_claim_directives):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    _baton(repo, "pred.md", "claimed_at: 2026-10-28T10:00:00Z", "claimed_by: test-session")
    _baton(repo, "peer.md", "claimed_at: 2026-10-28T10:00:00Z", "claimed_by: peer")
    _seed_claim(repo, SESSION, "pred.md")
    _seed_claim(repo, OTHER, "peer.md")

    code, report = ba_apply.apply(
        "handoff", "", session_id=SESSION, repo_root=repo, title="Follow the claim"
    )

    assert code == ba_apply.APPLY_EXIT_OK, report
    assert _held(repo, OTHER) == ["peer.md"]


def test_follow_is_convergent_when_the_claim_already_moved(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, SESSION, "new.md")
    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is True
    assert _held(repo, SESSION) == ["new.md"]


def test_follow_still_refuses_when_nothing_is_held(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, OTHER, "new.md")
    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is False
