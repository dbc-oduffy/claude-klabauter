"""End-to-end tests for the baton.close_report handler on a tmp_path fixture repo."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from coordinator_core import dag
from coordinator_core.ops import baton_close_commit_check as chk
from coordinator_core.ops import baton_close_report as op
from coordinator_core.ops.baton_close_report_contract import OP_KEY, REPORT_KEYS
from coordinator_core.ops.baton_close_report_readers import read_baton_headers, successor_index
from coordinator_core.ops.baton_close_report_targets import target_evidence
import pytest


_SHA = "a" * 40
_SPINE = (
    "\n## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: coded\n"
    f"  disposition_ref: {_SHA}\n```\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _baton(status: str = "open", extra: str = "", did: str | None = "dlv-t") -> str:
    did_line = f"deliverable_id: {did}\n" if did else ""
    if "deployment_state:" not in extra:
        extra += "deployment_state: ready_to_fire\n"
    return f"---\ntitle: t\nstatus: {status}\n{did_line}{extra}---\n\nbody\n"


def _fixture(root: Path) -> None:
    (root / ".git").mkdir()
    _write(root, "docs/plans/done.md", "---\nstatus: implemented\n---\n" + _SPINE)
    _write(root, "docs/plans/live.md", "---\nstatus: reviewed\n---\n")
    _write(root, "state/handoffs/closable.md", _baton(extra="governing_plan: docs/plans/done.md\n"))
    _write(root, "state/handoffs/claimed.md", _baton("claimed", "governing_plan: docs/plans/done.md\n"))
    _write(root, "state/handoffs/conflict.md",
           _baton(extra="governing_plan: docs/plans/done.md\nsizing_object: state/sizings/none.yaml\n"))
    _write(root, "state/handoffs/missing.md", _baton(extra="governing_plan: docs/plans/gone.md\n"))
    _write(root, "state/handoffs/notyet.md", _baton(extra="governing_plan: docs/plans/live.md\n"))
    _write(root, "state/handoffs/bare.md", _baton())
    _write(root, "state/handoffs/broken.md", "no frontmatter here\n")
    _write(root, "state/handoffs/closed.md", _baton("concluded", "governing_plan: docs/plans/done.md\n"))
    _write(root, "state/sizings/none.yaml", "status: draft\n")


def _run(root: Path, **params: str) -> dict:
    return asyncio.run(op._handler(params, root))


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def test_report_end_to_end(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rep = _run(tmp_path)
    assert tuple(rep) == REPORT_KEYS
    assert rep["op"] == OP_KEY and rep["exit_code"] == 0 and rep["scan_incomplete"] is False
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/closable.md"]
    assert rep["closable"][0]["evidence"][0]["done"] is True
    reasons = {r["handoff_path"].rsplit("/", 1)[1]: r["reason"] for r in rep["residue"]}
    assert reasons == {
        "broken.md": "unreadable_frontmatter",
        "claimed.md": "claimed",
        "conflict.md": "evidence_conflict",
        "missing.md": "target_missing",
    }
    assert rep["no_evidence"] == 2  # bare.md and notyet.md; closed.md is not live
    assert [(i["handoff_path"], i["reason"]) for i in rep["no_evidence_batons"]] == [
        ("state/handoffs/bare.md", "nothing_cited"),
        ("state/handoffs/notyet.md", "targets_not_done"),
    ]
    assert len(rep["no_evidence_batons"]) == rep["no_evidence"]
    assert rep["batons_scanned"] == 7


def test_handoff_param_narrows_to_one_baton(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rep = _run(tmp_path, handoff="state/handoffs/claimed.md")
    assert rep["batons_scanned"] == 1
    assert rep["closable"] == [] and [r["reason"] for r in rep["residue"]] == ["claimed"]
    rep = _run(tmp_path, handoff="./state/handoffs/closable.md")
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/closable.md"]


def test_live_successor_is_residue(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _write(tmp_path, "state/handoffs/next.md", _baton(extra="predecessor: state/handoffs/closable.md\n"))
    rep = _run(tmp_path)
    reasons = {r["handoff_path"].rsplit("/", 1)[1]: r["reason"] for r in rep["residue"]}
    assert reasons["closable.md"] == "live_successor"
    assert rep["closable"] == []


def test_missing_handoffs_dir_is_scan_incomplete(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    rep = _run(tmp_path)
    assert rep["scan_incomplete"] is True
    assert rep["batons_scanned"] == 0 and rep["closable"] == [] and rep["residue"] == []


def test_no_repo_root_is_an_error() -> None:
    assert asyncio.run(op._handler({}, None))["exit_code"] == 1


def _plan(status: str, extra: str = "") -> str:
    return f"---\nstatus: {status}\n{extra}---\n" + _SPINE


def _reverse_root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _by_name(items: list[dict]) -> dict[str, dict]:
    return {i["handoff_path"].rsplit("/", 1)[1]: i for i in items}


def test_reverse_implemented_plan_by_deliverable_id_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(did="dlv-a"))
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/b.md"]
    ev = rep["closable"][0]["evidence"]
    assert [e["field"] for e in ev] == ["plan.deliverable_id"]
    assert rep["no_evidence"] == 0


def test_reverse_landed_plan_stays_no_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("landed", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(did="dlv-a"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    assert rep["no_evidence"] == 1


def test_reverse_predecessor_handoff_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "predecessor_handoff: state\\handoffs\\b.md\n"))
    _write(root, "state/handoffs/b.md", _baton())
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/b.md"]
    assert [e["field"] for e in rep["closable"][0]["evidence"]] == ["plan.predecessor_handoff"]


def test_reverse_spinoff_baton_reads_spinoff_residue_with_join_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/s.md", _baton(did="dlv-a", extra="kind: spinoff\n"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["no_evidence"] == 0
    assert [(r["handoff_path"], r["reason"]) for r in rep["residue"]] == [
        ("state/handoffs/s.md", "spinoff_kind")
    ]
    assert [e["field"] for e in rep["residue"][0]["evidence"]] == ["plan.deliverable_id"]


def test_spinoff_with_done_governing_plan_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/s.md",
           _baton(extra="kind: spinoff\ngoverning_plan: docs/plans/done.md\n"))
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/s.md"]
    assert rep["residue"] == []


def test_spinoff_with_archived_implemented_governing_plan_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "archive/specs/2026-09/done.md", _plan("implemented"))
    _write(root, "state/handoffs/s.md",
           _baton(extra="kind: spinoff\ngoverning_plan: docs/plans/done.md\n"))
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/s.md"]
    assert rep["residue"] == []


def test_non_dlv_baton_with_done_evidence_is_no_deliverable_id_residue(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/n.md",
           _baton(did=None, extra="governing_plan: docs/plans/done.md\n"))
    rep = _run(root)
    assert rep["closable"] == []
    assert [(r["handoff_path"], r["reason"]) for r in rep["residue"]] == [
        ("state/handoffs/n.md", "no_deliverable_id")
    ]


def test_shared_predicate_parity_with_close_commit(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    gp = "governing_plan: docs/plans/done.md\ndeployment_state: ready_to_fire\n"
    _write(root, "state/handoffs/plain.md", _baton(did="dlv-plain-1", extra=gp))
    _write(root, "state/handoffs/spin.md", _baton(did="dlv-spin-1", extra=gp + "kind: spinoff\n"))
    _write(root, "state/handoffs/alias.md", _baton(did="dlv-alias-1", extra=gp + "kind: ' Spinoff '\n"))
    _write(root, "state/handoffs/nodlv.md", _baton(did="abc-1", extra=gp))
    _write(root, "state/handoffs/noid.md", _baton(did=None, extra=gp))
    _write(root, "state/handoffs/spin-nodlv.md", _baton(did=None, extra=gp + "kind: spinoff\n"))
    rep = _run(root)
    own_plan_ok = ["state/handoffs/alias.md", "state/handoffs/plain.md", "state/handoffs/spin.md"]
    assert sorted(c["handoff_path"] for c in rep["closable"]) == own_plan_ok
    assert len(rep["residue"]) == 3

    headers, _ = read_baton_headers(str(root))
    rels = sorted(headers)
    fms = {r: dag._read_meta(str(root / r)) for r in rels}
    texts = {
        os.path.abspath(p): p.read_text(encoding="utf-8")
        for p in (root / "state" / "handoffs").glob("*.md")
    }
    fresh = target_evidence(str(root), headers)
    succ = successor_index(headers)

    def _select(rows: list[dict]):
        return chk.select(rows, headers, succ, fresh, fms, texts, root / ".git", root)

    closable_rows = [
        {"handoff_path": c["handoff_path"], "evidence": c["evidence"]} for c in rep["closable"]
    ]
    flips, refused, _ = _select(closable_rows)
    assert refused == []
    assert sorted(f["path"] for f in flips) == own_plan_ok

    residue_rows = [
        {"handoff_path": r["handoff_path"], "evidence": r["evidence"]} for r in rep["residue"]
    ]
    flips, refused, _ = _select(residue_rows)
    assert flips == []
    assert {r["handoff_path"].rsplit("/", 1)[1]: r["reason"] for r in refused} == {
        "nodlv.md": "no_deliverable_id",
        "noid.md": "no_deliverable_id",
        "spin-nodlv.md": "no_deliverable_id",
    }


def test_reverse_implemented_and_draft_plans_conflict(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p1.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "docs/plans/p2.md", _plan("draft", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b.md", _baton(did="dlv-a"))
    rep = _run(root)
    assert rep["closable"] == []
    assert [(r["handoff_path"], r["reason"]) for r in rep["residue"]] == [
        ("state/handoffs/b.md", "evidence_conflict")
    ]


def test_reverse_shared_deliverable_id_across_batons_stays_no_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/b1.md", _baton(did="dlv-a"))
    _write(root, "state/handoffs/b2.md", _baton(did="dlv-a"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    assert rep["no_evidence"] == 2


_SIZING = "state/sizings/s.yaml"


def _run_record(root: Path, run_id: str, *, sizing: str | None = None, baton: str | None = None,
                sha: str | None = _SHA, job: bool = True) -> None:
    import json

    if sizing is not None:
        _write(root, f"state/warp/{run_id}/run.json", json.dumps({"sizing_rel": sizing}))
    if baton is not None:
        _write(root, f"state/warp/{run_id}/run-baton.json", json.dumps({"baton": baton}))
    if job:
        _write(root, "docs/plans/exec.md", _plan("implemented"))
        _write(root, f".coordinator-local/close-jobs/{run_id}.workflow.json",
               json.dumps({"terminal_sha": sha, "claimed_batons": [], "plan_rel": "docs/plans/exec.md"}))


def test_run_evidence_spinoff_via_sizing_rel_is_closable(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "state/handoffs/s.md", _baton(extra=f"kind: spinoff\nsizing_object: {_SIZING}\n"))
    _run_record(root, "r1", sizing=_SIZING)
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/s.md"]
    ev = rep["closable"][0]["evidence"]
    assert [(e["class"], e["field"], e["target_status"], e["done"]) for e in ev
            if e["class"] == "run"] == [("run", "run.sizing_rel", "committed", True)]
    assert rep["closable"][0]["conflict"] is None and rep["residue"] == []


def test_run_evidence_spinoff_via_run_baton_backslash_value(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "state/handoffs/s.md", _baton(extra="kind: spinoff\n"))
    _run_record(root, "r1", baton="state\\handoffs\\s.md")
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/s.md"]
    assert [e["field"] for e in rep["closable"][0]["evidence"]] == ["run.baton"]


def test_run_evidence_parent_join_alone_stays_spinoff_residue(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/p.md", _plan("implemented", "deliverable_id: dlv-a\n"))
    _write(root, "state/handoffs/s.md", _baton(did="dlv-a", extra="kind: spinoff\n"))
    rep = _run(root)
    assert rep["closable"] == []
    assert [r["reason"] for r in rep["residue"]] == ["spinoff_kind"]
    assert rep["residue"][0]["conflict"] is None


def test_run_evidence_claimed_with_ended_run_is_released(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/c.md",
           _baton("claimed", "governing_plan: docs/plans/done.md\nsizing_object: " + _SIZING + "\n"))
    _run_record(root, "r1", sizing=_SIZING, sha=None)
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/c.md"]
    assert rep["residue"] == []


def test_run_evidence_claimed_with_live_run_stays_claimed(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/c.md",
           _baton("claimed", "governing_plan: docs/plans/done.md\nsizing_object: " + _SIZING + "\n"))
    _run_record(root, "r1", sizing=_SIZING, sha=None)
    _run_record(root, "r2", baton="state/handoffs/c.md", job=False)
    rep = _run(root)
    assert rep["closable"] == []
    assert [r["reason"] for r in rep["residue"]] == ["claimed"]


def _conflict_fixture(root: Path, with_run: bool) -> None:
    _write(root, "docs/plans/appr.md", _plan("approved"))
    _write(root, "state/sizings/s.yaml", "status: shipped\n")
    _write(root, "state/handoffs/x.md",
           _baton(extra=f"kind: spinoff\ngoverning_plan: docs/plans/appr.md\nsizing_object: {_SIZING}\n"))
    if with_run:
        _run_record(root, "r1", sizing=_SIZING)


def test_run_evidence_conflict_won_by_committed_run(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _conflict_fixture(root, with_run=True)
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/x.md"]
    note = rep["closable"][0]["conflict"]
    assert note["winner"] == "done"
    assert any(e["class"] == "run" for e in note["done"])
    assert [e["target_status"] for e in note["contradicting"]] == ["approved"]


def test_run_evidence_conflict_without_run_names_both_and_contradicting_wins(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _conflict_fixture(root, with_run=False)
    rep = _run(root)
    assert rep["closable"] == []
    assert [r["reason"] for r in rep["residue"]] == ["evidence_conflict"]
    note = rep["residue"][0]["conflict"]
    assert note["winner"] == "contradicting"
    assert note["done"] and note["contradicting"]


def test_writes_nothing(tmp_path: Path) -> None:
    _fixture(tmp_path)
    before = _snapshot(tmp_path)
    _run(tmp_path)
    assert _snapshot(tmp_path) == before


def test_zero_subprocess_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path)
    calls: list[object] = []

    def _count(*a, **k):
        calls.append(a)
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _count)
    monkeypatch.setattr(os, "system", _count)
    _run(tmp_path)
    assert calls == []


def _gp() -> str:
    return "governing_plan: docs/plans/done.md\n"


def test_shipped_baton_is_absent(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/s.md", _baton(extra=_gp() + "deployment_state: shipped\n"))
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == [] and rep["no_evidence"] == 0


def test_gated_awaiting_gate_is_not_advanceable_with_route(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/g.md",
           _baton(extra=_gp() + "deployment_state: awaiting_gate\ngate_dependency: x\n"))
    rep = _run(root)
    assert rep["closable"] == []
    (item,) = rep["residue"]
    assert item["reason"] == "not_advanceable"
    assert "not consistent with terminal" in item["detail"]
    assert "handoff.transition to ready_to_fire" in item["detail"]


def test_ungated_awaiting_gate_is_closable_with_ship_ref(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", _plan("implemented"))
    _write(root, "state/handoffs/u.md", _baton(extra=_gp() + "deployment_state: awaiting_gate\n"))
    rep = _run(root)
    assert [c["handoff_path"] for c in rep["closable"]] == ["state/handoffs/u.md"]
    assert rep["closable"][0]["ship_ref"] == _SHA


def test_legacy_plan_without_spine_is_no_evidence_sha(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/done.md", "---\nstatus: implemented\n---\n")
    _write(root, "state/handoffs/l.md", _baton(extra=_gp()))
    rep = _run(root)
    assert rep["closable"] == []
    (item,) = rep["residue"]
    assert item["reason"] == "no_evidence_sha" and item["detail"]


def test_no_evidence_names_each_baton_with_a_reason(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "docs/plans/live.md", "---\nstatus: reviewed\n---\n")
    _write(root, "docs/plans/ticked.md", _plan("landed"))
    _write(root, "state/handoffs/a-bare.md", _baton())
    _write(root, "state/handoffs/b-notdone.md", _baton(extra="governing_plan: docs/plans/live.md\n"))
    _write(root, "state/handoffs/c-ticked.md", _baton(extra="governing_plan: docs/plans/ticked.md\n"))
    rep = _run(root)
    items = rep["no_evidence_batons"]
    assert [i["handoff_path"] for i in items] == [
        "state/handoffs/a-bare.md", "state/handoffs/b-notdone.md", "state/handoffs/c-ticked.md",
    ]
    assert [i["reason"] for i in items] == ["nothing_cited", "targets_not_done", "acs_all_ticked"]
    assert len(items) == rep["no_evidence"] == 3
    assert items[2]["detail"] and "docs/plans/ticked.md" in items[2]["detail"] and _SHA in items[2]["detail"]
    assert items[0]["detail"] is None and items[1]["detail"] is None
    assert rep["closable"] == [] and rep["residue"] == []


def test_decided_batons_are_not_listed_as_no_evidence(tmp_path: Path) -> None:
    _fixture(tmp_path)
    rep = _run(tmp_path)
    listed = {i["handoff_path"] for i in rep["no_evidence_batons"]}
    assert not listed & {c["handoff_path"] for c in rep["closable"]}
    assert not listed & {r["handoff_path"] for r in rep["residue"]}


def test_planning_run_is_not_done_evidence(tmp_path: Path) -> None:
    root = _reverse_root(tmp_path)
    _write(root, "state/handoffs/s.md", _baton(extra=f"kind: spinoff\nsizing_object: {_SIZING}\n"))
    _run_record(root, "r1", sizing=_SIZING)
    _write(root, "docs/plans/exec.md", _plan("reviewed"))
    rep = _run(root)
    assert rep["closable"] == []


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _git_root(tmp_path: Path, commits: int = 3) -> tuple[Path, list[str]]:
    _git(tmp_path, "init", "-q")
    shas = []
    for i in range(commits):
        _write(tmp_path, f"f{i}.txt", str(i))
        _git(tmp_path, "add", f"f{i}.txt")
        _git(tmp_path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    return tmp_path, shas


def _ac_baton(lines: list[str], heading: str = "## Acceptance criteria") -> str:
    return _baton() + f"\n{heading}\n" + "\n".join(lines) + "\n"


def test_plan_less_baton_with_all_acs_ticked_and_shas_is_closable(tmp_path: Path) -> None:
    root, shas = _git_root(tmp_path)
    lines = [f"- [x] AC-{i} done (`{s[:12]}`)" for i, s in enumerate(shas)]
    _write(root, "state/handoffs/b.md", _ac_baton(lines))
    rep = _run(root)
    (item,) = rep["closable"]
    assert item["handoff_path"] == "state/handoffs/b.md"
    assert item["ship_ref"] == shas[-1]
    assert item["evidence"][0]["class"] == "ac_list" and item["evidence"][0]["done"] is True
    assert rep["no_evidence"] == 0 and rep["residue"] == []


@pytest.mark.parametrize("case", ["partial", "sha_less", "unresolvable", "no_ac_list", "unrelated_box"])
def test_ac_list_negative_cases_stay_no_evidence(tmp_path: Path, case: str) -> None:
    root, shas = _git_root(tmp_path, commits=2)
    a, b = shas[0][:12], shas[1][:12]
    if case == "partial":
        body = _ac_baton([f"- [x] AC-1 ({a})", f"- [ ] AC-2 ({b})"])
    elif case == "sha_less":
        body = _ac_baton([f"- [x] AC-1 ({a})", "- [x] AC-2 done without a commit"])
    elif case == "unresolvable":
        body = _ac_baton([f"- [x] AC-1 ({a})", "- [x] AC-2 (deadbe1)"])
    elif case == "no_ac_list":
        body = _baton() + "\n## Notes\nnothing ticked\n"
    else:
        body = _baton() + f"\n## Todo\n- [x] tidy up ({a})\n"
    _write(root, "state/handoffs/b.md", body)
    rep = _run(root)
    assert rep["closable"] == [] and rep["residue"] == []
    (item,) = rep["no_evidence_batons"]
    assert item["handoff_path"] == "state/handoffs/b.md" and item["reason"] == "nothing_cited"


def test_ac_list_does_not_override_a_cited_plan(tmp_path: Path) -> None:
    root, shas = _git_root(tmp_path, commits=1)
    _write(root, "docs/plans/live.md", "---\nstatus: reviewed\n---\n")
    _write(root, "state/handoffs/b.md", _baton(extra="governing_plan: docs/plans/live.md\n")
           + f"\n## Acceptance criteria\n- [x] AC-1 ({shas[0][:12]})\n")
    rep = _run(root)
    assert rep["closable"] == []
    assert [i["reason"] for i in rep["no_evidence_batons"]] == ["targets_not_done"]


@pytest.mark.parametrize("packed", [False, True])
def test_ac_list_sha_resolution_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packed: bool) -> None:
    root, shas = _git_root(tmp_path)
    if packed:
        _git(root, "gc", "-q", "--prune=now")
    _write(root, "state/handoffs/a.md", _ac_baton([f"- [x] AC-{i} ({s[:12]})" for i, s in enumerate(shas)]))
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    rep = _run(root)
    assert [c["ship_ref"] for c in rep["closable"]] == [shas[-1]] and calls == []
