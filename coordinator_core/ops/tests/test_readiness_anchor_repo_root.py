"""plan.readiness_verdict resolves an anchor check against the main worktree root: production
passes the git common dir as repo_root and a foreign cwd, and the real evaluator still RESOLVES.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import plan_readiness_verdict as op

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "docs/plans/2026-10-15-fixture.md"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(root: Path, check_command: str, expect: str) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "coordinator_core" / "ops").mkdir(parents=True)
    (root / "marker.txt").write_text("needle\nneedle\nother\n", encoding="utf-8")
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        "capabilities: []\n"
        "anchor_premises:\n  - id: AP1\n    claim: marker has two needles\n    check:\n"
        f"      command: {check_command}\n      expect: '{expect}'\n"
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root / ".git"


def _reply(tmp_path, monkeypatch, command: str, expect: str) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    common = _repo(repo, command, expect)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    return op._handler({"plan": REL}, common)


def test_passing_anchor_check_resolves_from_git_dir_and_foreign_cwd(tmp_path, monkeypatch):
    reply = _reply(tmp_path, monkeypatch, "grep -c needle marker.txt", "2")
    verdicts = {v["id"]: v for v in reply["anchor_verdicts"]}
    assert verdicts["AP1"]["verdict"] == "RESOLVES"
    subjects = [r["subject"] for r in reply["rulings"] if r["source"] == "premise"]
    assert not any("anchor-premise-no-verdict" in s or "CONTRADICTED" in s for s in subjects)
    assert "AP1" not in " ".join(subjects)


def test_failing_anchor_check_is_still_refused(tmp_path, monkeypatch):
    reply = _reply(tmp_path, monkeypatch, "grep -c needle marker.txt", "9")
    verdicts = {v["id"]: v for v in reply["anchor_verdicts"]}
    assert verdicts["AP1"]["verdict"] == "CONTRADICTED"
    assert reply["verdict"] != "ready"
