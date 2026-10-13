"""A hand-worked plan reaches a judged stamp: emit --rejudge, record, mint, check, close-out dry-run."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.close_out_and_stamp import close_out_and_stamp
from coordinator_core.ops import review_stamp as rs
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_ID = "pln-hand-worked-fixture-000001"
PLAN_REL = "docs/plans/hand-worked.md"
_FLAGS = no_console_creationflags()

_ROW = (
    "- id: {id}\n  title: Row {id}\n  change_kind: code-edit\n  surface: {id}.py\n"
    "  deferred: false\n  disposition: coded\n  disposition_ref: {sha}\n"
    "  disposition_detail: 'by hand'\n  body: |\n    Row {id}.\n"
)
_PLAN = """---
title: "Hand-worked fixture"
created: 2026-10-13
author: test-fixture
status: executing
branch: "work/test/2026-10-13"
plan_id: "{plan_id}"
deliverable_id: "dlv-hand-worked-fixture-000001"
---

# Hand-worked fixture

## Tasks

```yaml plan-tasks
{rows}```
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _hand_commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"hand work: {name}")
    return _git(repo, "rev-parse", "HEAD")


def _write_plan(repo: Path, shas: list[str]) -> Path:
    rows = "".join(_ROW.format(id=f"C{i}", sha=sha) for i, sha in enumerate(shas, 1))
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(_PLAN.format(plan_id=PLAN_ID, rows=rows), encoding="utf-8")
    _git(repo, "add", PLAN_REL)
    _git(repo, "commit", "-q", "-m", "plan rows coded by hand")
    return plan


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _case_one(tmp_path: Path) -> tuple[Path, Path]:
    """Every row coded by a hand commit; no stamp, no run record."""
    repo = _new_repo(tmp_path)
    shas = [_hand_commit(repo, "C1.py"), _hand_commit(repo, "C2.py")]
    return repo, _write_plan(repo, shas)


def _case_two(tmp_path: Path) -> tuple[Path, Path]:
    """A halted run left a bookkeeping record with no delivery block and no Inline-Review
    trailer commit; the last row was coded by a later hand commit."""
    repo = _new_repo(tmp_path)
    first = _hand_commit(repo, "C1.py")
    halted = repo / ".coordinator-local" / "subagent-share" / "sess" / f"{PLAN_ID}.review-wave-bookkeeping.md"
    halted.parent.mkdir(parents=True)
    fm = {"agent_type": "engine:review-wave-bookkeeping", "plan_id": PLAN_ID, "status": "halted",
          "recorded_at": "2026-10-13T00:00:00.000000Z"}
    halted.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")
    second = _hand_commit(repo, "C2.py")
    return repo, _write_plan(repo, [first, second])


def _emit_and_record(repo: Path, plan: Path, status: str) -> tuple[Path, str]:
    out = repo / "judge.workflow.mjs"
    rd.emit_rejudge(repo_root=repo, plan_path=str(plan), out_path=str(out))
    script = out.read_text(encoding="utf-8")
    assert script.count("agent(") == 1
    ident = json.loads(re.search(r"\{[^{}]*\"rejudge\": true[^{}]*\}", script).group(0))
    assert "run_base_sha" in ident
    returned = {
        "reverify_delivery": ident,
        "criterion": {"status": status, "observation": "the criterion holds at HEAD"},
    }
    payload = json.dumps({"result": json.dumps(returned)})
    assert rd.main(["record", "--result-json", payload, "--repo-root", str(repo)]) == 0
    records = list((repo / "state" / "delivery-verdicts").glob("*/*.md"))
    assert len(records) == 1
    fm = yaml.safe_load(records[0].read_text(encoding="utf-8").split("---\n")[1])
    assert fm["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert fm["run_base_sha"] == ident["run_base_sha"]
    return records[0], fm["head_sha"]


@pytest.mark.parametrize("build", [_case_one, _case_two], ids=["coded-by-hand", "halted-run-finished-by-hand"])
def test_met_judge_mints_a_stamp_that_check_and_close_out_accept(tmp_path, capsys, build):
    repo, plan = build(tmp_path)
    assert "no review_stamp" in rs.check(plan, repo)
    _, before = close_out_and_stamp(str(plan), repo_root=repo, dry_run=True)
    assert before["review_stamp_gate"]["refused"] and not before["stamped"]
    record, head = _emit_and_record(repo, plan, "met")

    stamp = rs.mint(plan, repo, build_test_path=None)

    rel = record.relative_to(repo).as_posix()
    assert stamp["criterion"]["sidecar"] == rel and stamp["criterion"]["status"] == "met"
    assert stamp["terminal_commit_sha"] == head and "delivery" not in stamp
    assert rs.check(plan, repo, supersession=True) is None
    _, result = close_out_and_stamp(str(plan), repo_root=repo, dry_run=True)
    assert "review_stamp_gate" not in result
    assert result["stamped"] and result["status_target"] == "implemented"
    assert "no review_stamp" not in json.dumps(result, default=str)
    assert "review_stamp refused" not in json.dumps(result, default=str)
    capsys.readouterr()


@pytest.mark.parametrize("build", [_case_one, _case_two], ids=["coded-by-hand", "halted-run-finished-by-hand"])
@pytest.mark.parametrize("status", ["not_met", "indeterminate"])
def test_unmet_judge_mints_nothing(tmp_path, build, status):
    repo, plan = build(tmp_path)
    _emit_and_record(repo, plan, status)
    before = plan.read_bytes()

    with pytest.raises(rs.MintRefusal):
        rs.mint(plan, repo, build_test_path=None)

    assert plan.read_bytes() == before
