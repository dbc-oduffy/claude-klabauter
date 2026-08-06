"""
Tests for coordinator_core.ops.audience_mismatch_scan.

Fixture shape mirrors test_check_harvest_debt.py (same argv-driven,
--root-explicit probe family) -- these tests write hand-crafted run-report
sidecars directly under a tmp_path `state/subagent-share/` tree and assert
on the probe's stdout nudge.

Spec backlink: docs/plans/2026-07-27-claude-md-altitude-triage.md, C14.
"""

from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator_core.ops.audience_mismatch_scan import main

_NOW = datetime.now(tz=timezone.utc)


def _sidecar_text(spawned_at: str, answer: str) -> str:
    return (
        "---\n"
        "plan: docs/plans/2026-07-01-fake-plan.md\n"
        "chunk: C1\n"
        "agent_type: coordinator:executor\n"
        f"spawned_at: {spawned_at}\n"
        "dispatched_by: em-session-fake\n"
        "status: complete\n"
        "divergence: {diverged: false}\n"
        "commits: []\n"
        "sidecar_schema: v1\n"
        "---\n\n"
        "## Observations\n\n"
        "Nothing else notable.\n\n"
        "## Exit interview\n\n"
        f"- What did you have to work out that the brief could have told you? {answer}\n\n"
        "- What did you grep, read, or probe that turned out to be a dead end "
        "-- and what were you actually looking for? N/A.\n\n"
        "- Where did your tool access, permissions, or output contract fight "
        "you? What would you have reached for if it existed? Nothing.\n\n"
        "- Anything you wanted to say and had nowhere to put? No.\n"
    )


def _write_sidecar(
    root: Path,
    rel_path: str,
    answer: str,
    *,
    days_ago: float = 1.0,
) -> Path:
    spawned_at = (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    path = root / "state" / "subagent-share" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sidecar_text(spawned_at, answer), encoding="utf-8")
    return path


def _run(root: Path, *extra_args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(["--root", str(root), *extra_args])
    return exit_code, out.getvalue().rstrip("\n"), err.getvalue()


_GAP = (
    "The brief never mentioned the settings-home forwarder path for "
    "cross-repo-memo, so I had to grep CLAUDE.md to find it."
)
_GAP_PARAPHRASE = (
    "Had to grep CLAUDE.md to find the settings-home forwarder path for "
    "cross-repo-memo -- the brief never named it."
)
_UNRELATED = "Nothing about this dispatch's file layout was documented anywhere."


def test_no_subagent_share_dir_silent_noop(tmp_path: Path) -> None:
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_below_repeat_threshold_silent(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_three_recurring_gaps_nudges(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE)
    _write_sidecar(tmp_path, "a3.md", _GAP)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert "[audience-mismatch]" in out
    assert "3 recent dispatched agents" in out
    assert "settings-home forwarder" in out


def test_null_answers_excluded_from_clustering(tmp_path: Path) -> None:
    for i, answer in enumerate(["Nothing notable.", "N/A.", "None.", "Nothing."]):
        _write_sidecar(tmp_path, f"n{i}.md", answer)
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_unrelated_answers_do_not_cluster_together(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE)
    _write_sidecar(tmp_path, "a3.md", _GAP)
    _write_sidecar(tmp_path, "b1.md", _UNRELATED)
    _write_sidecar(tmp_path, "b2.md", "Nothing about the layout was documented.")
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out.count("[audience-mismatch]") == 1


def test_stale_sidecars_excluded_by_recency_window(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP, days_ago=1)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE, days_ago=2)
    _write_sidecar(tmp_path, "a3.md", _GAP, days_ago=40)  # outside default 14-day window
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_since_days_flag_widens_the_window(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP, days_ago=1)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE, days_ago=2)
    _write_sidecar(tmp_path, "a3.md", _GAP, days_ago=40)
    exit_code, out, _ = _run(tmp_path, "--since-days", "60")
    assert exit_code == 0
    assert "[audience-mismatch]" in out


def test_repeat_threshold_flag_lowers_the_bar(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "a1.md", _GAP)
    _write_sidecar(tmp_path, "a2.md", _GAP_PARAPHRASE)
    exit_code, out, _ = _run(tmp_path, "--repeat-threshold", "2")
    assert exit_code == 0
    assert "[audience-mismatch]" in out
    assert "2 recent dispatched agents" in out


def test_missing_exit_interview_section_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state" / "subagent-share" / "no-interview.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    spawned_at = (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    path.write_text(
        f"---\nspawned_at: {spawned_at}\nstatus: complete\n---\n\n## Findings\n\nSome finding.\n",
        encoding="utf-8",
    )
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_no_explicit_root_and_no_git_returns_zero_silently(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.audience_mismatch_scan.which", lambda _name: None
    )
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main([])
    assert exit_code == 0
    assert out.getvalue() == ""


def test_empty_answer_does_not_bleed_into_next_question(tmp_path: Path) -> None:
    """Regression: an answer left blank before the next bullet must not be
    captured as the literal text of the following question (see module
    docstring Negative-spec)."""
    spawned_at = (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    for i in range(3):
        path = tmp_path / "state" / "subagent-share" / f"empty{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"spawned_at: {spawned_at}\n"
            "status: complete\n"
            "---\n\n"
            "## Exit interview\n\n"
            "- What did you have to work out that the brief could have told you?\n\n"
            "- What did you grep, read, or probe that turned out to be a dead "
            "end -- and what were you actually looking for? N/A.\n\n"
            "- Where did your tool access, permissions, or output contract "
            "fight you? What would you have reached for if it existed? Nothing.\n\n"
            "- Anything you wanted to say and had nowhere to put? No.\n",
            encoding="utf-8",
        )
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert out == ""


def test_mtime_fallback_when_frontmatter_absent(tmp_path: Path) -> None:
    path = tmp_path / "state" / "subagent-share" / "no-frontmatter.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## Exit interview\n\n"
        f"- What did you have to work out that the brief could have told you? {_GAP}\n",
        encoding="utf-8",
    )
    path2 = tmp_path / "state" / "subagent-share" / "no-frontmatter-2.md"
    path2.write_text(
        "## Exit interview\n\n"
        f"- What did you have to work out that the brief could have told you? {_GAP_PARAPHRASE}\n",
        encoding="utf-8",
    )
    path3 = tmp_path / "state" / "subagent-share" / "no-frontmatter-3.md"
    path3.write_text(
        "## Exit interview\n\n"
        f"- What did you have to work out that the brief could have told you? {_GAP}\n",
        encoding="utf-8",
    )
    # mtime defaults to "now" at write time, well inside the recency window.
    exit_code, out, _ = _run(tmp_path)
    assert exit_code == 0
    assert "[audience-mismatch]" in out
