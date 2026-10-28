"""An XS refire of a halted run that staged rows resumes its record spine instead of re-entering plan-blitz."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261028T150000-4004db"
SPINE = f"state/warp/{RUN}/gate.spine.md"


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _fixture(repo, *, halted=None, manifest_rows=({"id": "X1"},)):
    """An XS run halted with X1 executed in the tree but not landed: spine, halted.json, manifest."""
    _put(repo, tshirt="XS", route="dispatch")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    d = repo / "state" / "warp" / RUN
    d.mkdir(parents=True)
    (d / "gate.spine.md").write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: X1\n  title: T\n  disposition: open\n  writes:\n    - some/X1.py\n```\n",
        encoding="utf-8", newline="\n",
    )
    record = {"status": "halted", "halted": "criterion_not_met", "sizing_rel": REL, "plan": SPINE}
    record.update(halted or {})
    (d / "halted.json").write_text(json.dumps(record), encoding="utf-8")
    if manifest_rows is not None:
        (d / "manifest.json").write_text(json.dumps({"rows": list(manifest_rows)}), encoding="utf-8")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt", "state")
    _git(repo, "commit", "-q", "-m", "base")


def _sizing(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_xs_refire_of_a_halt_with_staged_rows_resumes_the_spine_rows(repo):  # noqa: F811
    _fixture(repo)
    plan = resumable_plan(_sizing(repo), REL, repo, "xs")
    assert plan is not None and plan.plan_rel == SPINE and plan.incomplete == ("X1",)


def test_gate_names_the_spine_as_resume_plan_not_a_fresh_plan(repo):  # noqa: F811
    _fixture(repo)
    v = gate(repo, REL, writes=["some/X1.py"])
    assert v.halt is None and v.resume_plan == SPINE and v.resume_rows == ["X1"]


def test_refusal_halt_stays_unresumed(repo):  # noqa: F811
    _fixture(repo, halted={"halted": "refusal"})
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_halt_that_staged_no_rows_stays_unresumed(repo):  # noqa: F811
    _fixture(repo, manifest_rows=())
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_halt_without_a_manifest_stays_unresumed(repo):  # noqa: F811
    _fixture(repo, manifest_rows=None)
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_already_resumed_marker_stays_unresumed(repo):  # noqa: F811
    _fixture(repo, halted={"status": "resumed"})
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None
