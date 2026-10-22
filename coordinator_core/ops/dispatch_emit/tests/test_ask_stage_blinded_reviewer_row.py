"""A falsifier-integrity reviewer run row is staged blinded: its brief is the phase's own spec prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit, falsifier_integrity_phase as fip
from coordinator_core.ops.dispatch_emit.ask_compose import SKILL_WEIGHT_HEADROOM_CLAUSE
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

PLAN_REL = "docs/plans/2026-10-15-host.md"
REVIEWED_REL = "docs/plans/2026-10-02-reviewed-secret-title.md"

_REVIEWED = """---
title: Reviewed Secret Title
prime_exit_criterion:
  statement: the gate can fail
  falsifier:
    how: run tools/inst.py
    baseline_output: green today
    baseline_ref: abc1234
    expected_when_true: red
---
# Reviewed Secret Title

Secret plan body.
"""

_NO_FALSIFIER = "---\ntitle: Bare\n---\n# Bare\n"


def _host(row_extra: str = "  falsifier_review_of: " + REVIEWED_REL, writes: str = "  writes: []") -> str:
    return f"""---
title: Host Plan Title
---
# Host Plan Title

## Tasks

```yaml plan-tasks
- id: S1
  title: Blinded review
  change_kind: verification
  agent_type: coordinator:falsifier-integrity-reviewer
{row_extra}
  surface: {REVIEWED_REL}
{writes}
```
"""


@pytest.fixture(autouse=True)
def _rubric(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("rubric") / "falsifier-integrity-reviewer.md"
    path.write_text("rubric A\n", encoding="utf-8")
    monkeypatch.setattr(fip, "_reviewer_rubric_path", lambda: path)


def _repo(tmp_path: Path, host: str, reviewed: str = _REVIEWED) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "inst.py").write_text("print('ok')\n", encoding="utf-8", newline="\n")
    (tmp_path / PLAN_REL).write_text(host, encoding="utf-8", newline="\n")
    (tmp_path / REVIEWED_REL).write_text(reviewed, encoding="utf-8", newline="\n")


def _brief(tmp_path: Path, manifest) -> bytes:
    return (tmp_path / manifest.rows[0].brief_path).read_bytes()


def test_reviewer_row_brief_is_the_phase_spec_prompt(tmp_path):
    _repo(tmp_path, _host())
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
    report_dir = Path(manifest.run_dir) / "can-report-red"
    expected = fip.review_specs(fip.review_inputs([REVIEWED_REL], repo_root=tmp_path, report_dir=report_dir))[0].prompt
    got = _brief(tmp_path, manifest)
    assert got.startswith(expected.encode("utf-8") + b"\n\n")
    tail = got[len(expected.encode("utf-8")):].decode("utf-8")
    assert tail.startswith("\n\n" + SKILL_WEIGHT_HEADROOM_CLAUSE)
    assert "do not pin prose in tests" in tail
    assert b"\r" not in got
    assert (tmp_path / report_dir / "2026-10-02-reviewed-secret-title.json").is_file()


def test_reviewer_row_brief_carries_no_intent_context_or_plan_text(tmp_path):
    _repo(tmp_path, _host())
    sizing = tmp_path / "state" / "sizings" / "s.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text(
        "estimate: {tshirt: S}\nroute: plan\npm_verbatims:\n  - {text: 'PM words here', at: x}\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel="state/sizings/s.yaml")
    brief = _brief(tmp_path, manifest).decode("utf-8")
    for leaked in (
        "PM intent", "PM words here", "Host Plan Title", "Reviewed Secret Title", "Secret plan body",
        REVIEWED_REL, PLAN_REL, "Spec:", "Execute S1",
    ):
        assert leaked not in brief


@pytest.mark.parametrize(
    "host, reviewed",
    [
        (_host(row_extra=""), _REVIEWED),
        (_host(row_extra="  falsifier_review_of: ../outside.md"), _REVIEWED),
        (_host(), _NO_FALSIFIER),
        (_host(writes="  writes:\n    - docs/x.md"), _REVIEWED),
    ],
    ids=["missing-key", "path-escapes-repo", "no-falsifier-block", "non-empty-writes"],
)
def test_reviewer_row_refusals(tmp_path, host, reviewed):
    _repo(tmp_path, host, reviewed)
    with pytest.raises(AskStageError):
        stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_reviewer_row_verdict_persists_at_report_path():
    script = _compose()
    branch = script.split("r.agent_type === 'coordinator:falsifier-integrity-reviewer'", 1)[1].split("else _rows", 1)[0]
    assert "dispatch-reports/" in branch and "+ r.id + '.md'" in branch
    assert "JSON.stringify(_v)" in branch
    assert emit.stage_schema_literal("falsifier_integrity_result") in branch
