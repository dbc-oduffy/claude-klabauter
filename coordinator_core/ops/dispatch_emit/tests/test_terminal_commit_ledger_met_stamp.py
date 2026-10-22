"""A met ledger discharges an indeterminate criterion through the real plan-implemented transition.

Un-stubbed: a real plan file, the real ``cs_stamp_plan_implemented`` and ``criterion_refusal``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SHA = "a" * 40

_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
prime_exit_criterion:
  text: a.py exists
review_stamp:
  delivery: {{verdict: PASS, product_files: 1, claims_unbacked: 0}}
  tests: {{status: not_run, run: 0, failed: 0, sidecar: null}}
  criterion:
    status: {status}
    observation: judged
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: coded
  disposition_ref: {ref}
  deferred: false
```
"""


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _plan(repo: Path, status: str) -> Path:
    plan = repo / "docs" / "plan.md"
    plan.parent.mkdir(exist_ok=True)
    plan.write_text(_PLAN.format(status=status, ref="b" * 40), encoding="utf-8")
    return plan


def test_stamp_plan_implemented_indeterminate_with_ledger_met_stamps(repo):
    plan = _plan(repo, "indeterminate")
    out = terminal_commit._stamp_plan_implemented(repo, "docs/plan.md", _SHA, ledger_met="ledger-met: R1")
    assert out["plan_status"] == "implemented", out
    text = plan.read_text(encoding="utf-8")
    assert "status: implemented" in text
    assert f"criterion met at {_SHA} per requirements ledger" in text


def test_stamp_plan_implemented_not_met_with_ledger_met_refuses(repo):
    plan = _plan(repo, "not_met")
    before = plan.read_text(encoding="utf-8")
    out = terminal_commit._stamp_plan_implemented(repo, "docs/plan.md", _SHA, ledger_met="ledger-met: R1")
    assert out["plan_status"] != "implemented", out
    assert plan.read_text(encoding="utf-8") == before


def test_criterion_refusal_default_keeps_the_indeterminate_refusal():
    from coordinator_core.ops.review_stamp import criterion_refusal

    fm = "review_stamp:\n  criterion:\n    status: indeterminate\n"
    assert criterion_refusal(Path("p.md"), fm) is not None
    assert criterion_refusal(Path("p.md"), fm, ledger_met="  ") is not None
    assert criterion_refusal(Path("p.md"), fm, ledger_met="ledger-met: R1") is None
    assert criterion_refusal(Path("p.md"), fm.replace("indeterminate", "not_met"), ledger_met="ledger-met: R1") is not None


def test_stamp_plan_implemented_absent_criterion_with_ledger_met_stamps(repo):
    plan = repo / "docs" / "plan.md"
    plan.parent.mkdir(exist_ok=True)
    text = _PLAN.format(status="met", ref="b" * 40)
    plan.write_text(text.replace("review_stamp:", "review_stamp_unused:", 1), encoding="utf-8")
    out = terminal_commit._stamp_plan_implemented(repo, "docs/plan.md", _SHA, ledger_met="ledger-met: R1")
    assert out.get("plan_status_reason") != "review_stamp criterion is not met", out


class _Ledger:
    def __init__(self, all_of_pm_ask=False):
        self.all_of_pm_ask = all_of_pm_ask


_OP = {"id": "R3", "reason": "awaiting_operator C6", "row": "C6", "command": "run it"}


def _verdict(**extra):
    return {"items": [{"id": "R2", "status": "met"}, {"id": "R3", "status": "not_met", **extra}]}


def test_no_open_row_stamps_unchanged():
    got = terminal_commit._ledger_met_observation(_Ledger(), {"items": [{"id": "R2", "status": "met"}]}, [])
    assert got == "ledger-met: R2"
    assert terminal_commit._ledger_not_stamped_reason(_Ledger(), {"items": [{"id": "R2", "status": "met"}]}, []) is None


def test_non_essential_operator_row_stamps():
    v = _verdict(essential=False)
    assert terminal_commit._ledger_met_observation(_Ledger(), v, [_OP]) == "ledger-met: R2"
    assert terminal_commit._ledger_not_stamped_reason(_Ledger(), v, [_OP]) is None


def test_essential_operator_row_names_reason():
    v = _verdict()
    assert terminal_commit._ledger_met_observation(_Ledger(), v, [_OP]) is None
    assert "R3: awaiting_operator C6" in terminal_commit._ledger_not_stamped_reason(_Ledger(), v, [_OP])


def test_all_of_pm_ask_keeps_operator_row_blocking():
    v = _verdict(essential=False)
    assert terminal_commit._ledger_met_observation(_Ledger(True), v, [_OP]) is None
    assert terminal_commit._ledger_not_stamped_reason(_Ledger(True), v, [_OP])


def test_non_ledger_run_is_silent():
    assert terminal_commit._ledger_not_stamped_reason(None, None, []) is None
