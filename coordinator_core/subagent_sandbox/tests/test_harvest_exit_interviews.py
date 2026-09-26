
from __future__ import annotations

from pathlib import Path

from coordinator_core.subagent_sandbox.harvest_exit_interviews import harvest

_ANSWERED_DOC = """---
status: open
agent_type: coordinator:code-reviewer
spawned_at: 2026-07-24T00:00:00+00:00
divergence:
  diverged: false
commits: []
dispatch_feed: null  # forward-declared, INERT until pcli-04 emitter
---

## Run notes

## Observations

## Exit interview

- What did you have to work out that the brief could have told you?

The exact frontmatter key name for agent_type -- had to read provision_report.py.

- What did you grep, read, or probe that turned out to be a dead end — and what were you actually looking for?

Grepped for a harvest CLI that didn't exist yet.

- Where did your tool access, permissions, or output contract fight you? What was missing that isn't deliberately withheld from this role — a guard denial is not a gap.

Nothing fought me here.

- Anything you wanted to say and had nowhere to put?

No.
"""

_UNANSWERED_DOC = """---
status: open
agent_type: coordinator:executor
spawned_at: 2026-07-24T00:00:00+00:00
divergence:
  diverged: false
commits: []
dispatch_feed: null  # forward-declared, INERT until pcli-04 emitter
---

## Run notes

## Observations

## Exit interview

- What did you have to work out that the brief could have told you?

- What did you grep, read, or probe that turned out to be a dead end — and what were you actually looking for?

- Where did your tool access, permissions, or output contract fight you? What was missing that isn't deliberately withheld from this role — a guard denial is not a gap.

- Anything you wanted to say and had nowhere to put?

"""


def _write_sidecar(tmp_path: Path, session: str, name: str, content: str) -> Path:
    session_dir = tmp_path / ".coordinator-local" / "subagent-share" / session
    session_dir.mkdir(parents=True, exist_ok=True)
    doc_path = session_dir / name
    doc_path.write_text(content, encoding="utf-8")
    return doc_path


def _write_plan_sidecar(tmp_path: Path, name: str, content: str) -> Path:
    plan_sidecars_dir = tmp_path / ".coordinator-local" / "plan-sidecars"
    plan_sidecars_dir.mkdir(parents=True, exist_ok=True)
    doc_path = plan_sidecars_dir / name
    doc_path.write_text(content, encoding="utf-8")
    return doc_path


def test_harvest_includes_answered_excludes_empty_and_counts_skipped(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "session-1", "a.md", _ANSWERED_DOC)
    _write_sidecar(tmp_path, "session-1", "b.md", _UNANSWERED_DOC)

    report_text, included, skipped_empty = harvest(tmp_path, session=None)

    assert included == 1
    assert skipped_empty == 1

    assert "agent_type=coordinator:code-reviewer" in report_text
    assert "The exact frontmatter key name for agent_type" in report_text

    assert "coordinator:executor" not in report_text


def test_harvest_session_filter_restricts_to_one_session(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "session-1", "a.md", _ANSWERED_DOC)
    _write_sidecar(tmp_path, "session-2", "c.md", _ANSWERED_DOC)

    report_text, included, skipped_empty = harvest(tmp_path, session="session-1")

    assert included == 1
    assert skipped_empty == 0
    assert "session-1" in report_text
    assert "session-2" not in report_text


def test_harvest_includes_plan_derivable_sidecars(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "session-1", "a.md", _ANSWERED_DOC)
    _write_plan_sidecar(tmp_path, "my-plan.prior-art-check.md", _ANSWERED_DOC)

    report_text, included, skipped_empty = harvest(tmp_path, session=None)

    assert included == 2
    assert skipped_empty == 0
    assert "plan-sidecars/my-plan.prior-art-check.md" in report_text


def test_harvest_plan_sidecars_included_regardless_of_session_filter(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "session-1", "a.md", _ANSWERED_DOC)
    _write_plan_sidecar(tmp_path, "my-plan.docs-check.md", _ANSWERED_DOC)

    report_text, included, skipped_empty = harvest(tmp_path, session="session-2")

    assert included == 1
    assert skipped_empty == 0
    assert "plan-sidecars/my-plan.docs-check.md" in report_text
    assert "session-1" not in report_text


def test_harvest_plan_sidecars_absent_directory_no_crash(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "session-1", "a.md", _ANSWERED_DOC)

    report_text, included, skipped_empty = harvest(tmp_path, session=None)

    assert included == 1
    assert skipped_empty == 0
