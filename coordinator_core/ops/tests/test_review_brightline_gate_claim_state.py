"""
Tests for coordinator_core.ops.review_brightline_gate._resolve_closing_session_id's
ledger-first migration (C6a6).

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks, chunk C6a (AC5).

Covers the C1 incident directly: a seed baton whose claim was stamped while
the shared worktree sat on a branch it has since switched away from — the
tracked-frontmatter mirror reverts to no-claim, but the branch-independent
ledger still holds it. Before this migration, `_resolve_closing_session_id`
read the mirror only and hard-exited `main(["--from-handoff", ...])` with rc
1 on such a baton. After, it resolves ledger-first via
`coordinator_core.claim_state.resolve_claim_state` and the gate runs.

Negative-spec: a baton with NO claim on either source must still resolve to
"" and the gate must still hard-exit rc 1 — this migration widens which
desynced-but-real claims resolve, it does not soften the "nothing to
resolve" failure mode.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import coordinator_core.ops.review_brightline_gate as rbg
from coordinator_core import claim_state
from coordinator_core.ops.review_brightline_gate import _resolve_closing_session_id


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")


def _commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _write_baton_no_claim(path: Path) -> None:
    """A baton whose frontmatter mirror carries NO claimed_by/consumed_by —
    the branch-switch-revert desync shape: an open-looking mirror with the
    real claim living only in the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.name}"',
                "kind: session-handoff",
                "status: open",
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_ledger_claim(repo: Path, sid: str, basename: str) -> None:
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T00:00:00Z", encoding="utf-8")


# ---------------------------------------------------------------------------
# _resolve_closing_session_id — direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_closing_session_id_resolves_desynced_ledger_only_baton(tmp_path, monkeypatch):
    """AC5: a seed baton with a live LEDGER claim but no mirror claim (the
    branch-switch-revert desync) must resolve — this used to resolve to ""
    and hard-exit the gate."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    seed = repo / "state" / "handoffs" / "seed-desynced.md"
    _write_baton_no_claim(seed)
    _write_ledger_claim(repo, "ledger-only-sid", seed.name)

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        resolved = _resolve_closing_session_id(repo, str(seed))

    assert resolved == "ledger-only-sid"


def test_resolve_closing_session_id_empty_when_no_claim_anywhere(tmp_path, monkeypatch):
    """Negative-spec: a baton with no claim on EITHER source must still
    resolve to "" — widening resolution must not soften this failure mode."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    seed = repo / "state" / "handoffs" / "seed-unclaimed.md"
    _write_baton_no_claim(seed)

    resolved = _resolve_closing_session_id(repo, str(seed))

    assert resolved == ""


# ---------------------------------------------------------------------------
# main(["--from-handoff", ...]) — end-to-end gate behavior
# ---------------------------------------------------------------------------


def test_from_handoff_runs_gate_for_desynced_baton_previously_hard_exited(
    tmp_path, capsys, monkeypatch
):
    """The gate must now RUN (rc 0) against a desynced baton that previously
    hard-exited rc 1 because `_resolve_closing_session_id` read the mirror
    only."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    seed = repo / "state" / "handoffs" / "seed-desynced.md"
    _write_baton_no_claim(seed)
    _write_ledger_claim(repo, "ledger-only-sid", seed.name)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(rbg, "_enumerate_owned_batons", lambda repo_root, sid: ([], []))
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "could not resolve a closing session id" not in captured.err


def test_from_handoff_still_hard_exits_for_baton_with_no_claim_anywhere(
    tmp_path, capsys, monkeypatch
):
    """Negative-spec at the gate level: a baton unresolvable on both sources
    must still hard-exit rc 1 with the existing diagnostic — not silently
    run against nothing."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    seed = repo / "state" / "handoffs" / "seed-unclaimed.md"
    _write_baton_no_claim(seed)
    monkeypatch.chdir(repo)

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "could not resolve a closing session id" in captured.err
