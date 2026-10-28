"""plan.readiness_verdict regrades a CONTRADICTED row citing the halted run's own withheld path."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import _own_run_premise
from coordinator_core.ops import plan_readiness_verdict as op

REL = "docs/plans/2026-10-28-fixture.md"
SIZING = "state/sizings/2026-10-28-fixture-aaaaaa.yaml"
RUN = "ask-20261028T010000-aaaaaa"
OTHER_RUN = "ask-20261028T020000-bbbbbb"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _repo(root: Path, sizing: str = SIZING) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "coordinator_core").mkdir()
    (root / ".git").mkdir()
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-28\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\nanchor_premises: []\n"
        f"sizing_object: {sizing}\n"
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        f"  derived_from: {sizing}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def _halt(root: Path, run: str, paths: list, sizing: str = SIZING) -> None:
    record = root / "state" / "warp" / run
    record.mkdir(parents=True)
    (record / "halted.json").write_text(
        json.dumps({"status": "halted", "sizing_rel": sizing, "withheld": paths}), encoding="utf-8"
    )
    index = root / ".coordinator-local" / "halt-withheld"
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{run}.json").write_text(json.dumps({"paths": paths}), encoding="utf-8")
    (index / ".built").write_bytes(b"")


def _row(path: str) -> dict:
    return {"verdict": "CONTRADICTED", "citation": f"{path}:3 the plan cites base content"}


def _reply(common: Path, rows: list) -> dict:
    return op._handler({"plan": REL, "premise": {"rows": rows}}, common)


def test_own_withheld_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _halt(tmp_path, RUN, ["src/x.py"])
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0


def test_path_outside_the_withheld_set_still_pulls(tmp_path):
    common = _repo(tmp_path)
    _halt(tmp_path, RUN, ["src/x.py"])
    reply = _reply(common, [_row("src/y.py")])
    assert reply["verdict"] == "judgment" and reply["blocking_findings"] == 1


def test_path_withheld_by_another_sizings_run_still_pulls(tmp_path):
    common = _repo(tmp_path)
    _halt(tmp_path, RUN, ["src/other.py"])
    _halt(tmp_path, OTHER_RUN, ["src/x.py"], sizing="state/sizings/2026-10-28-other-bbbbbb.yaml")
    reply = _reply(common, [_row("src/x.py")])
    assert reply["verdict"] == "judgment"


def test_regrade_stamps_run_id_and_leaves_other_rows(tmp_path):
    own = {"src/x.py": RUN}
    rows = [_row("./src/x.py"), _row("src/y.py"), {"verdict": "UNRESOLVED", "citation": "src/x.py:1"}]
    out = _own_run_premise.regrade(rows, own)
    assert out[0]["verdict"] == "RESOLVES" and out[0]["own_run_edit"] == RUN
    assert out[1] == rows[1] and out[2] == rows[2]
    assert rows[0]["verdict"] == "CONTRADICTED"


def test_own_run_paths_keeps_a_path_shared_with_a_later_run(tmp_path):
    _halt(tmp_path, RUN, ["src/x.py"])
    _halt(tmp_path, OTHER_RUN, ["src/x.py"], sizing="state/sizings/2026-10-28-other-bbbbbb.yaml")
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/x.py": RUN}


def test_no_halted_run_gives_empty_set(tmp_path):
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {}
