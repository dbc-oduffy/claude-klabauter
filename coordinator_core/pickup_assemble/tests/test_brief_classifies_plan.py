"""`pickup-assemble brief <plan>` classifies a plan document as `plan` and
always carries `gates.execution_stamp_match` (`unstamped` when no stamp exists)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa
from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)

# Declared, not excused: `brief()` reads real git state (tree quiescence,
# stamp-commit lookup) that no fixture stands in for.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_PLAN_REL = "docs/plans/2026-01-01-example.md"


def _write_plan(repo: Path, fm_extra: str = "", status: str = "reviewed") -> Path:
    path = repo / _PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Example plan"\n'
        "created: 2026-01-01\n"
        f"status: {status}\n"
        f"{fm_extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Plan\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", _PLAN_REL)
    _git(repo, "commit", "-m", "add plan")
    return path


def _run_brief(repo: Path, monkeypatch, capsys) -> dict:
    monkeypatch.chdir(repo)
    code = pa.main(["brief", _PLAN_REL, "--no-claim"])
    assert code == pa.EXIT_OK
    return json.loads(capsys.readouterr().out)


def test_reviewed_unstamped_plan_classifies_plan_with_unstamped_gate(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = _write_plan(repo)

    out = _run_brief(repo, monkeypatch, capsys)

    assert out["artifact"]["classification"] == "plan"
    assert out["artifact"]["frontmatter"]["status"] == "reviewed"
    gate = out["gates"]["execution_stamp_match"]
    assert gate["verdict"] == "unstamped"
    assert gate["stamped_sha"] is None
    assert gate["computed_sha"] == canonical_body_sha(plan.read_text(encoding="utf-8"))
    assert "authorize-invocation" in gate["next_move"]
    assert out["directives"] == []
    assert out["next_move"] == gate["next_move"]
    assert out["next_move"] != f"/execute-plan {_PLAN_REL}"


def test_reviewed_stamped_plan_reports_match(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = _write_plan(repo, fm_extra="execution_authorized_sha: PENDING\n")
    template = plan.read_text(encoding="utf-8")
    plan.write_text(template.replace("PENDING", canonical_body_sha(template)), encoding="utf-8")
    _git(repo, "add", _PLAN_REL)
    _git(repo, "commit", "-m", "stamp plan")

    out = _run_brief(repo, monkeypatch, capsys)

    assert out["artifact"]["classification"] == "plan"
    assert out["gates"]["execution_stamp_match"]["verdict"] == "match"


def test_terminal_unstamped_plan_carries_no_stamp_gate(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_plan(repo, status=sorted(pa.PLAN_TERMINAL_STATUS)[0])

    out = _run_brief(repo, monkeypatch, capsys)

    assert out["artifact"]["classification"] == "plan"
    assert "execution_stamp_match" not in out["gates"]


def test_plan_without_status_stays_ambiguous():
    fm_text = 'title: "x"\ncreated: 2026-01-01\n'
    assert pa.classify(fm_text, Path("/r/docs/plans/x.md")) == "ambiguous"


def test_non_plan_dir_with_status_stays_ambiguous():
    assert pa.classify("status: reviewed\n", Path("/r/notes/x.md")) == "ambiguous"
    assert pa.classify("status: reviewed\n", Path("/r/docs/plans/../x.md")) == "ambiguous"


def test_handoff_dir_still_classifies_handoff():
    assert pa.classify("status: open\n", Path("/r/state/handoffs/x.md")) == "handoff"
