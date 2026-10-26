"""dispatch.terminal_commit never stamps a chunk coded when params.held names it (string or object form)
or its report ends a STOP-RULE-FIRED line; an unheld, unfired chunk is still stamped."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000b1"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()
_ROW = (
    "- id: {id}\n  title: t{id}\n  change_kind: code-edit\n  surface: {p}\n"
    "  writes:\n    - {p}\n  disposition: open\n  deferred: false\n"
)
_PLAN = "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n" + _ROW.format(id="C1", p="a.py") + _ROW.format(
    id="C2", p="b.py"
) + "```\n"
_STOP_REPORT = "DONE: C1\nSTOP-RULE-FIRED: the premise is refuted\n"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _run(repo: Path, c1_report: str, **params) -> str:
    """Runs terminal_commit over C1 and C2; returns the plan text after the stamp."""
    from coordinator_core.ops.dispatch_emit import run_token

    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8", newline="\n")
    (repo / "reports").mkdir()
    (repo / "reports" / "C1.md").write_text(c1_report, encoding="utf-8")
    (repo / "reports" / "C2.md").write_text("DONE: C2\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",), report="reports/C1.md"),
            ChunkCommit(id="C2", title="t2", paths=("b.py",), report="reports/C2.md"),
        ),
        plan_path="docs/plan.md",
    )
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    token = run_token.mint(repo, "run-held", "run.mjs")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF, "run_id": "run-held", "run_token": token, "plan_path": "docs/plan.md", **params},
        repo_root=repo / ".git",
    )
    assert out.get("committed") is True, out
    return (repo / "docs" / "plan.md").read_text(encoding="utf-8")


def _coded(plan: str, rid: str) -> bool:
    block = plan.split(f"- id: {rid}\n", 1)[1].split("- id:", 1)[0]
    return "disposition: coded" in block


def test_unheld_unfired_chunks_are_stamped_coded(repo):
    plan = _run(repo, "DONE: C1\n")
    assert _coded(plan, "C1") and _coded(plan, "C2")


def test_a_chunk_held_in_string_form_is_not_stamped_coded(repo):
    plan = _run(repo, "DONE: C1\n", held=["C1"])
    assert not _coded(plan, "C1")
    assert _coded(plan, "C2")


def test_a_chunk_held_in_object_form_is_not_stamped_coded(repo):
    plan = _run(repo, "DONE: C1\n", held=[{"chunk": "C1", "reason": "halted"}])
    assert not _coded(plan, "C1")
    assert _coded(plan, "C2")


def test_a_stop_rule_fired_chunk_is_not_stamped_coded_without_held(repo):
    plan = _run(repo, _STOP_REPORT)
    assert not _coded(plan, "C1")
    assert _coded(plan, "C2")
