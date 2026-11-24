"""Tests for the plan.tasks.mutate ``em-discharge`` verb and ``read_em_discharges``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import (
    EM_DISCHARGE_KEY,
    _handler,
    evidence_sidecar_path,
    read_em_discharges,
    read_row_evidence,
)
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Discharge Plan"
status: ready
---

# Discharge Plan

## Tasks

```yaml plan-tasks
- id: E1
  title: EM step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: open
  body: |
    Do it.
- id: M1
  title: Send memo
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  kind: memo-send
  body: |
    Send.
- id: O1
  title: Operator step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  execution_mode: operator
  body: |
    Operator.
- id: X1
  title: Executor step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  body: |
    Code.
- id: D1
  title: Already closed
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: wont_do
  body: |
    Closed.
```

## Trailer
"""

_B1_BODY = """\
  body: |
    Run `pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8`.
"""


def _derived_plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    rows = f"""\
- id: B1
  title: Build plugin
  change_kind: verification
  surface: scripts/build-plugin.ps1
  writes: []
  agent_type: coordinator:executor
  queue_scope: project
  disposition: open
{_B1_BODY}- id: B2
  title: Build and author
  change_kind: code-edit
  surface: scripts/build-plugin.ps1
  writes: [x.cpp]
  queue_scope: project
  disposition: open
{_B1_BODY}- id: B3
  title: Operator build
  change_kind: verification
  surface: scripts/build-plugin.ps1
  writes: []
  queue_scope: project
  execution_mode: operator
  disposition: open
{_B1_BODY}"""
    plan.write_text(
        "---\ntitle: P\nstatus: ready\n---\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n",
        encoding="utf-8",
        newline="",
    )
    return repo, plan


def _plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    plan.write_text(_PLAN, encoding="utf-8", newline="")
    return repo, plan


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "em-discharge", "plan_path": str(plan), "id": "E1", "text": "probe done"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_discharge_writes_key_and_leaves_plan_bytes(tmp_path):
    repo, plan = _plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan)
    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert plan.read_bytes() == before
    got = read_em_discharges(plan)
    assert set(got) == {"E1"}
    assert got["E1"]["text"] == "probe done"
    assert got["E1"]["recorded_at"]
    assert read_row_evidence(plan) == {}
    assert EM_DISCHARGE_KEY == "em_discharged"


def test_plan_cited_at_a_shard_path_discharges_at_its_written_flat_path(tmp_path):
    repo, plan = _plan(tmp_path)
    minted = plan.parent / "2026-11" / plan.name
    assert _call(repo, minted)["applied"] is True
    assert set(read_em_discharges(plan)) == {"E1"}
    assert not minted.parent.exists()


@pytest.mark.parametrize("sidecar_text, found", [
    ("- a\n- b\n", "found list"),
    ("em_discharged:\n- E1\n", "found em_discharged: list"),
])
def test_malformed_sidecar_refusal_names_file_and_wanted_shape(tmp_path, sidecar_text, found):
    repo, plan = _plan(tmp_path)
    evidence_sidecar_path(plan).write_text(sidecar_text, encoding="utf-8")
    result = _call(repo, plan)
    msg = str(result)
    assert result["exit_code"] != 0 and evidence_sidecar_path(plan).name in msg
    assert "wanted a mapping" in msg and found in msg


def test_second_discharge_is_noop_not_overwrite(tmp_path):
    repo, plan = _plan(tmp_path)
    _call(repo, plan)
    first = read_em_discharges(plan)["E1"]
    result = _call(repo, plan, text="different")
    assert result["exit_code"] == 0 and result["applied"] is False
    assert read_em_discharges(plan)["E1"] == first


def test_xs_spine_under_state_warp_discharges(tmp_path):
    repo, plan = _plan(tmp_path)
    spine = repo / "state" / "warp" / "ask-1" / "s.spine.md"
    spine.parent.mkdir(parents=True)
    spine.write_text(_PLAN, encoding="utf-8", newline="")
    result = _call(repo, spine)
    assert result["exit_code"] == 0 and result["applied"] is True, result
    assert set(read_em_discharges(spine)) == {"E1"}


def test_plan_path_outside_plans_and_state_warp_still_refused(tmp_path):
    repo, plan = _plan(tmp_path)
    stray = repo / "state" / "other" / "s.spine.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(_PLAN, encoding="utf-8", newline="")
    result = _call(repo, stray)
    assert result["exit_code"] == 1 and "escapes" in result["error"]
    assert not evidence_sidecar_path(stray).exists()


def test_detection_target_claude_md_mints_no_em_row_but_edit_target_does(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine

    (tmp_path / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x\n", encoding="utf-8")

    def spine(text):
        sizing = {
            "intent": "i",
            "exit_criterion": {"statement": "s"},
            "requirement_register": {
                "rows": [{"id": "R1", "source_text": text, "status": "open", "anchor": "relayed"}]
            },
        }
        return mint_xs_spine(
            sizing, sizing_rel="state/sizings/s.yaml", writes=["src/x.py"], out_dir=tmp_path / "out", repo_root=tmp_path
        )[0]

    detect = "void the cell if any ancestor of its sandbox holds a CLAUDE.md"
    assert "performer: em" not in spine(detect)
    assert "performer: em" in spine("edit CLAUDE.md")


@pytest.mark.parametrize("row_id", ["NOPE", "M1", "O1", "X1", "D1"])
def test_refusals_write_nothing(tmp_path, row_id):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, id=row_id)
    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert not evidence_sidecar_path(plan).exists()


@pytest.mark.parametrize("extra", [{"id": ""}, {"text": ""}, {"text": "   "}])
def test_empty_id_or_text_refused(tmp_path, extra):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, **extra)
    assert result["exit_code"] == 1
    assert not evidence_sidecar_path(plan).exists()


def test_derived_em_row_discharges_and_leaves_plan_bytes(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan, id="B1")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    assert plan.read_bytes() == before
    assert set(read_em_discharges(plan)) == {"B1"}
    again = _call(repo, plan, id="B1", text="other")
    assert again["exit_code"] == 0 and again["applied"] is False


def test_writes_row_with_same_body_stays_agent(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    result = _call(repo, plan, id="B2")
    assert result["exit_code"] == 1
    assert "is not performer: em" in str(result)
    assert not evidence_sidecar_path(plan).exists()


def test_derived_em_operator_row_hits_operator_refusal(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    result = _call(repo, plan, id="B3")
    assert result["exit_code"] == 1
    assert "execution_mode: operator" in str(result)
    assert not evidence_sidecar_path(plan).exists()


def test_reader_never_raises(tmp_path):
    plan = tmp_path / "p.md"
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text(": : [garbled", encoding="utf-8")
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text("em_discharged: [1, 2]\n", encoding="utf-8")
    assert read_em_discharges(plan) == {}


_ONLY_EM_ROW = """\
---
title: "Discharge Only"
status: ready
---

## Tasks

```yaml plan-tasks
- id: E1
  title: EM step
  change_kind: verification
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: open
  body: |
    Do it.
- id: X1
  title: Executor step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  disposition: open
  body: |
    Code.
```
"""


def _rev(repo: Path, rev: str) -> str:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "rev-parse", rev],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_executor_row_discharged_with_commit_and_evidence(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import em_discharge_closes

    repo, plan = _plan(tmp_path)
    sha = _rev(repo, "HEAD")
    result = _call(repo, plan, id="X1", commit=sha, evidence="vitest 69/69")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    entry = read_em_discharges(plan)["X1"]
    assert entry["commit"] == sha and entry["evidence"] == "vitest 69/69"
    assert em_discharge_closes({"id": "X1"}, entry) is True


@pytest.mark.parametrize(
    "extra",
    [
        {"evidence": "ok"},
        {"commit": "HEAD", "evidence": "ok"},
        {"commit": "ab" * 20, "evidence": "ok"},
        {"commit": "{head}", "evidence": "  "},
        {"commit": "{head}"},
        {"commit": "{tree}", "evidence": "ok"},
        {"commit": "abc", "evidence": "ok"},
        {"commit": "{tree7}", "evidence": "ok"},
    ],
)
def test_executor_row_refused_without_a_resolving_commit_and_evidence(tmp_path, extra):
    repo, plan = _plan(tmp_path)
    subs = {"{head}": _rev(repo, "HEAD"), "{tree}": _rev(repo, "HEAD^{tree}"), "{tree7}": _rev(repo, "HEAD^{tree}")[:7]}
    extra = {k: subs.get(v, v) for k, v in extra.items()}
    result = _call(repo, plan, id="X1", **extra)
    assert result["exit_code"] == 1 and result["applied"] is False, result
    assert not evidence_sidecar_path(plan).exists()


def test_abbreviated_commit_resolves_and_is_stored_in_full(tmp_path):
    repo, plan = _plan(tmp_path)
    sha = _rev(repo, "HEAD")
    result = _call(repo, plan, id="X1", commit=sha[:10].upper(), evidence="ok")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    assert read_em_discharges(plan)["X1"]["commit"] == sha


def test_unresolvable_commit_refusal_does_not_claim_absence(tmp_path):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, id="X1", commit="ab" * 20, evidence="ok")
    assert result["exit_code"] == 1, result
    assert "not a commit in this repo" not in result["error"]
    assert "did not resolve" in result["error"]
    assert not evidence_sidecar_path(plan).exists()


def test_executor_citation_never_widens_memo_operator_or_closed_rows(tmp_path):
    repo, plan = _plan(tmp_path)
    cite = {"commit": _rev(repo, "HEAD"), "evidence": "ok"}
    for row_id in ("M1", "O1", "D1"):
        assert _call(repo, plan, id=row_id, **cite)["exit_code"] == 1
    assert not evidence_sidecar_path(plan).exists()


def test_executor_closes_predicate_needs_both_fields():
    from coordinator_core.ops.plan_tasks_mutate import em_discharge_closes

    row = {"id": "X1"}
    assert em_discharge_closes(row, None) is False
    assert em_discharge_closes(row, {"text": "t"}) is False
    assert em_discharge_closes(row, {"commit": "a" * 40}) is False
    assert em_discharge_closes(row, {"commit": "a" * 40, "evidence": " "}) is False
    assert em_discharge_closes(row, {"commit": "a" * 40, "evidence": "e"}) is True
    assert em_discharge_closes({"id": "E1", "performer": "em"}, None) is True


def test_discharged_row_no_longer_counts_as_open_for_stamp_and_gate(tmp_path):
    """em-discharge leaves disposition open by contract; the close oracles must read the sidecar."""
    from coordinator_core.archive_stamp import _open_spine_row_ids
    from coordinator_core.workstream_complete.directives_spine_worklist import (
        compute_open_spine_row_gate,
    )

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d2.md", "")
    plan.write_text(_ONLY_EM_ROW, encoding="utf-8", newline="")

    assert _open_spine_row_ids(plan) == ["E1", "X1"]
    assert _call(repo, plan)["applied"] is True
    assert _call(repo, plan)["applied"] is False  # the "already discharged" reply

    assert _open_spine_row_ids(plan) == ["X1"]
    gate = compute_open_spine_row_gate(plan.stem, plan)
    assert [r.id for r in gate.rows] == ["X1"]


def _ue_index(tmp_path: Path, states: dict) -> Path:
    import json

    index = tmp_path / "ue" / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps({"tests": [{"fullTestPath": n, "testDisplayName": n, "state": s} for n, s in states.items()]}),
        encoding="utf-8",
    )
    return index


def test_ue_report_all_passed_records_discharge_with_report_evidence(tmp_path):
    repo, plan = _plan(tmp_path)
    index = _ue_index(tmp_path / "outside", {"Proj.A": "Success", "Proj.B": "Success"})
    result = _call(repo, plan, ue_report={"index_json": str(index), "tests": ["Proj.A", "Proj.B"]})
    assert result["exit_code"] == 0 and result["applied"] is True, result
    entry = read_em_discharges(plan)["E1"]
    assert index.name in entry["evidence"] and "Proj.A" in entry["evidence"] and "Proj.B" in entry["evidence"]


def test_ue_report_relative_path_resolves_against_the_worktree(tmp_path):
    repo, plan = _plan(tmp_path)
    index = _ue_index(repo, {"Proj.A": "Success"})
    result = _call(repo, plan, ue_report={"index_json": index.relative_to(repo).as_posix(), "tests": ["Proj.A"]})
    assert result["exit_code"] == 0 and result["applied"] is True, result


@pytest.mark.parametrize("states, tests, named", [
    ({"Proj.A": "Success"}, ["Proj.A", "Proj.Gone"], "Proj.Gone"),
    ({"Proj.A": "Fail"}, ["Proj.A"], "Proj.A"),
])
def test_ue_report_missing_or_failed_test_is_refused_and_records_nothing(tmp_path, states, tests, named):
    repo, plan = _plan(tmp_path)
    index = _ue_index(tmp_path, states)
    result = _call(repo, plan, ue_report={"index_json": str(index), "tests": tests})
    assert result["exit_code"] != 0 and named in str(result)
    assert read_em_discharges(plan) == {}


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {"index_json": "", "tests": ["A"]},
    {"index_json": "x.json", "tests": []},
    {"index_json": "no/such/index.json", "tests": ["A"]},
])
def test_ue_report_malformed_or_unreadable_is_refused(tmp_path, bad):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, ue_report=bad)
    assert result["exit_code"] != 0
    assert read_em_discharges(plan) == {}


def test_ue_report_omitting_a_declared_criterion_test_is_refused_and_records_nothing(tmp_path):
    repo, plan = _plan(tmp_path)
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "  performer: em\n  disposition: open\n",
            "  performer: em\n  disposition: open\n"
            "  test_criterion:\n    kind: ue-automation\n    tests: [Proj.A, Proj.B]\n",
            1,
        ),
        encoding="utf-8",
        newline="",
    )
    index = _ue_index(tmp_path / "outside", {"Proj.A": "Success", "Proj.B": "Success"})
    refused = _call(repo, plan, ue_report={"index_json": str(index), "tests": ["Proj.A"]})
    assert refused["exit_code"] != 0 and "Proj.B" in str(refused)
    assert read_em_discharges(plan) == {}
    accepted = _call(repo, plan, ue_report={"index_json": str(index), "tests": ["Proj.A", "Proj.B"]})
    assert accepted["exit_code"] == 0 and accepted["applied"] is True, accepted


def test_executor_evidence_only_refused_when_no_write_changed_against_head(tmp_path):
    import subprocess

    repo, plan = _writes_plan(tmp_path, "[one.py]")
    (repo / "one.py").write_text("x", encoding="utf-8")
    for args in (["add", "one.py"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "w"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    result = _call(repo, plan, id="X1", evidence="pytest ok")
    assert result["exit_code"] == 1 and "unchanged" in str(result), result


def test_executor_commit_and_evidence_discharge_still_closes(tmp_path):
    import subprocess

    repo, plan = _writes_plan(tmp_path, "[one.py]")
    (repo / "one.py").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "one.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "w"],
                   cwd=str(repo), check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True).stdout.strip()
    result = _call(repo, plan, id="X1", commit=sha, evidence="pytest ok")
    assert result["exit_code"] == 0 and read_em_discharges(plan)["X1"]["commit"] == sha, result


def _writes_plan(tmp_path: Path, writes: str):
    repo, plan = _plan(tmp_path)
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "  body: |\n    Code.\n", f"  writes: {writes}\n  body: |\n    Code.\n", 1
        ),
        encoding="utf-8",
        newline="",
    )
    return repo, plan


def test_executor_evidence_only_discharge_when_every_write_is_on_disk(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import em_discharge_closes

    repo, plan = _writes_plan(tmp_path, "[a/one.py, two.py]")
    (repo / "a").mkdir()
    (repo / "a" / "one.py").write_text("x", encoding="utf-8")
    (repo / "two.py").write_text("y", encoding="utf-8")
    result = _call(repo, plan, id="X1", evidence="pytest 20 passed (EM-run)")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    entry = read_em_discharges(plan)["X1"]
    assert "commit" not in entry and entry["writes_verified"] is True
    assert em_discharge_closes({"id": "X1"}, entry) is True


@pytest.mark.parametrize("writes", ["[a/one.py, missing.py]", "[]", "[../outside.py]"])
def test_executor_evidence_only_refused_when_a_write_is_absent_or_undeclared(tmp_path, writes):
    repo, plan = _writes_plan(tmp_path, writes)
    (repo / "a").mkdir()
    (repo / "a" / "one.py").write_text("x", encoding="utf-8")
    (repo.parent / "outside.py").write_text("z", encoding="utf-8")
    result = _call(repo, plan, id="X1", evidence="pytest ok")
    assert result["exit_code"] == 1 and result["applied"] is False, result
    assert not evidence_sidecar_path(plan).exists()


def test_writes_verified_entry_without_evidence_does_not_close():
    from coordinator_core.ops.plan_tasks_mutate import em_discharge_closes

    assert em_discharge_closes({"id": "X1"}, {"writes_verified": True}) is False
    assert em_discharge_closes({"id": "X1"}, {"writes_verified": True, "evidence": " "}) is False
