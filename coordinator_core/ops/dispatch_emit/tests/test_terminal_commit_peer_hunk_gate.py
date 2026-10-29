"""dispatch.terminal_commit refuses a landing whose declared path a co-declaring live run also
claims, or that an unresolved reviewer out-of-ask finding names."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c3"
PATH = "pkg/execute_review.py"
SIBLING = "run-sibling"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    (root / PATH).write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _declare_sibling(repo: Path) -> None:
    rdir = repo / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    run_token.mint(repo, SIBLING, "sibling.mjs", session_id=SELF)


def _fire(repo: Path, plan_path: str | None = None, body: str = "x = 2\n") -> dict:
    (repo / PATH).write_text(body, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(PATH,)),), plan_path=plan_path)
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [], "session_id": SELF}
    return terminal_commit._handler(params, repo_root=repo / ".git")


_GATE_EXTRAS: list = []


@pytest.fixture(autouse=True)
def _capture_gate_extras(monkeypatch):
    """Record the integrity gate's extras: the provenance the landing carries."""
    real = terminal_commit._landing_integrity_gate
    _GATE_EXTRAS.clear()

    def spy(*args, **kwargs):
        extra = real(*args, **kwargs)
        _GATE_EXTRAS.append(extra)
        return extra

    monkeypatch.setattr(terminal_commit, "_landing_integrity_gate", spy)


def _provenance(_out: dict, reason: str) -> list:
    return [e for e in _GATE_EXTRAS[-1].get("provenance") or [] if e["reason"] == reason]


def test_a_path_a_live_sibling_run_also_declares_lands_and_records_it_as_provenance(repo):
    _declare_sibling(repo)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    (entry,) = _provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)
    assert entry["path"] == PATH
    assert SIBLING in entry["detail"]
    assert "Release run" not in entry["detail"]


def test_two_holders_on_two_declared_paths_are_both_named_in_the_provenance(repo):
    path2 = "pkg/other.py"
    (repo / path2).write_text("y = 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed2"], repo)
    sibling2 = "run-sibling-two"
    for sib, path in ((SIBLING, PATH), (sibling2, path2)):
        rdir = repo / "state" / "warp" / sib
        rdir.mkdir(parents=True)
        (rdir / "manifest.json").write_text(
            json.dumps({"rows": [{"id": "C1", "writes": [path]}], "doc_writes": []}), encoding="utf-8"
        )
        run_token.mint(repo, sib, "sibling.mjs", session_id=SELF)
    (repo / PATH).write_text("x = 2\n", encoding="utf-8")
    (repo / path2).write_text("y = 2\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(PATH, path2)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": [], "session_id": SELF}

    out = terminal_commit._handler(params, repo_root=repo / ".git")

    assert out["committed"] is True, out
    text = str(_provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED))
    for needle in (SIBLING, sibling2, PATH, path2):
        assert needle in text, text


def _halted_sibling(repo: Path, sizing_status: str, plan: str | None = None) -> str:
    import time

    (repo / "state/sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state/sizings/s.yaml").write_text(f"status: {sizing_status}\n", encoding="utf-8")
    halted = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-aaaaaa"
    for old in (repo / "state" / "warp").glob("ask-*-aaaaaa"):
        halted = old.name
    rdir = repo / "state" / "warp" / halted
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({
            "status": "halted", "sizing_rel": "state/sizings/s.yaml",
            "run_artifacts": {"withheld": {PATH: "halt-record-only"}},
            **({"plan": plan} if plan else {}),
        }),
        encoding="utf-8",
    )
    return halted


def test_a_halted_holder_provenance_names_its_release_and_a_superseded_sizing_records_none(repo):
    halted = _halted_sibling(repo, "open")

    out = _fire(repo)

    assert out["committed"] is True, out
    (entry,) = _provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)
    assert f"Release run {halted}" in entry["detail"]

    _halted_sibling(repo, "superseded")
    out = _fire(repo, body="x = 3\n")

    assert out["committed"] is True, out
    assert not _provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED), out


def test_a_halted_sibling_on_another_plan_is_provenance_and_on_the_landing_plan_records_none(repo):
    _halted_sibling(repo, "open", plan="docs/plans/p.md")

    out = _fire(repo, plan_path="docs/plans/q.md")

    assert out["committed"] is True, out
    assert _provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED), out

    out = _fire(repo, plan_path="docs/plans/p.md", body="x = 3\n")

    assert out["committed"] is True, out
    assert not _provenance(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED), out


def test_the_same_landing_without_a_co_declaring_sibling_commits(repo):
    out = _fire(repo)

    assert out["committed"] is True, out


def test_an_unresolved_out_of_ask_finding_on_the_path_lands_and_is_provenance(repo):
    share = repo / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True)
    (share / "run.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    row = {"id": "finding-1", "file": PATH, "summary": "failing_detail changes belong to another run",
           "out_of_ask": True}
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\nOut-of-ask hunks.\n"
        f"\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n",
        encoding="utf-8",
    )

    out = _fire(repo)

    assert out["committed"] is True, out
    (entry,) = _provenance(out, peer_hunk_gate.OUT_OF_ASK_HUNKS)
    assert "rev-1" in entry["detail"]

