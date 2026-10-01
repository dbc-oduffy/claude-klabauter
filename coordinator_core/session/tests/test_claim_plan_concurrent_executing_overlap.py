"""``claim_plan(for_execution=True)`` names a live, executing peer plan whose
``scope:`` overlaps the claimed plan's — the check that catches one op planned
twice under two deliverable_ids before both plans dispatch waves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claims, core
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _repo(tmp_path):
    kw = no_console_passthrough_kwargs()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **kw)
    return tmp_path


def _plan(repo, slug, status, scope):
    p = Path(repo) / "docs" / "plans" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"  - {s}" for s in scope)
    p.write_text(f"---\nstatus: {status}\nscope:\n{body}\n---\n# {slug}\n", encoding="utf-8")


def _peer_claim(repo, slug, sid, live=True):
    sess = Path(repo) / ".git" / "coordinator-sessions"
    cdir = sess / "plan-claims" / slug
    cdir.mkdir(parents=True)
    (cdir / "session_id").write_text(sid)
    (cdir / "claimed_at").write_text(core.now_iso())
    sdir = sess / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last = core.now_iso() if live else "2000-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(json.dumps({"pid": "1", "last_activity": last}))


@pytest.fixture
def me(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "me-sid")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def test_live_executing_peer_with_shared_scope_is_named(tmp_path, me, capsys):
    repo = _repo(tmp_path)
    _plan(repo, "plan-a", "executing", ["coordinator_core/handoff_reconcile.py"])
    _plan(repo, "plan-b", "approved", ["coordinator_core/handoff_reconcile.py"])
    _peer_claim(repo, "plan-a", "peer-sid")
    assert claims.claim_plan("plan-b", cwd=str(repo), for_execution=True) is True
    err = capsys.readouterr().err
    assert "plan-a" in err and "peer-sid" in err
    assert "coordinator_core/handoff_reconcile.py" in err


def test_directory_scope_entry_overlaps_file_entry(tmp_path, me, capsys):
    repo = _repo(tmp_path)
    _plan(repo, "plan-a", "executing", ["coordinator_core/"])
    _plan(repo, "plan-b", "approved", ["coordinator_core/x/y.py"])
    _peer_claim(repo, "plan-a", "peer-sid")
    assert claims.claim_plan("plan-b", cwd=str(repo), for_execution=True) is True
    assert "plan-a" in capsys.readouterr().err


@pytest.mark.parametrize(
    "peer_status,peer_scope,live",
    [
        ("executing", ["docs/other.md"], True),
        ("landed", ["coordinator_core/a.py"], True),
        ("executing", ["coordinator_core/a.py"], False),
    ],
)
def test_disjoint_scope_non_executing_or_dead_peer_is_silent(
    tmp_path, me, capsys, peer_status, peer_scope, live
):
    repo = _repo(tmp_path)
    _plan(repo, "plan-a", peer_status, peer_scope)
    _plan(repo, "plan-b", "approved", ["coordinator_core/a.py"])
    _peer_claim(repo, "plan-a", "peer-sid", live=live)
    assert claims.claim_plan("plan-b", cwd=str(repo), for_execution=True) is True
    assert "shares scope" not in capsys.readouterr().err


def test_default_claim_does_not_check(tmp_path, me, capsys):
    repo = _repo(tmp_path)
    _plan(repo, "plan-a", "executing", ["coordinator_core/a.py"])
    _plan(repo, "plan-b", "approved", ["coordinator_core/a.py"])
    _peer_claim(repo, "plan-a", "peer-sid")
    assert claims.claim_plan("plan-b", cwd=str(repo)) is True
    assert "shares scope" not in capsys.readouterr().err


def test_check_failure_never_fails_the_claim(tmp_path, me, monkeypatch):
    repo = _repo(tmp_path)
    _plan(repo, "plan-b", "approved", ["coordinator_core/a.py"])

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(claims, "_concurrent_executing_plan_overlaps", boom)
    assert claims.claim_plan("plan-b", cwd=str(repo), for_execution=True) is True


def _audit_plan(repo, slug):
    p = Path(repo) / "state" / "audits" / "e2e" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nstatus: approved\nscope:\n  - x.py\n---\n# {slug}\n", encoding="utf-8")
    return p


def test_plan_path_locates_a_plan_outside_docs_plans(tmp_path, me):
    repo = _repo(tmp_path)
    p = _audit_plan(repo, "run-met")
    assert claims.claim_plan(
        "run-met", cwd=str(repo), for_execution=True, plan_path="state/audits/e2e/run-met.md"
    ) is True
    assert "status: executing" in p.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["state/audits/e2e/other.md", "../run-met.md", "state/audits/e2e/run-met.txt"])
def test_plan_path_whose_stem_or_shape_disagrees_is_refused(tmp_path, me, bad):
    repo = _repo(tmp_path)
    _audit_plan(repo, "run-met")
    assert claims.claim_plan("run-met", cwd=str(repo), for_execution=True, plan_path=bad) is False
    assert not (Path(repo) / ".git" / "coordinator-sessions" / "plan-claims" / "run-met").exists()
