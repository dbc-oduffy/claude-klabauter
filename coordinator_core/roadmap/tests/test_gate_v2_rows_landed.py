"""gate_v2: a baton whose non-terminal linked plan has every row landed is coded and shipped_unstamped."""
from coordinator_core.roadmap import gate_v2
from coordinator_core.roadmap.tests.test_gate_v2 import baton, by_id, root  # noqa: F401

_ROW = "- id: {id}\n  title: t\n  change_kind: code-edit\n  surface: a.py\n  disposition: {disp}\n  deferred: false\n"


def _plan(root, name, dispositions, status="reviewed"):
    rows = "".join(_ROW.format(id=f"C{i}", disp=d) for i, d in enumerate(dispositions))
    path = root / "docs/plans" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\ntitle: {name}\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
    )
    return f"docs/plans/{name}.md"


def _coded(sha="abc1234"):
    return f"coded\n  disposition_ref: {sha}"


def test_landed_rows_on_a_reviewed_plan_report_shipped_unstamped_with_coded_sha(root):
    gp = _plan(root, "p", [_coded("aaa1111"), _coded("bbb2222")])
    baton(root, "a", governing_plan=gp)
    report = gate_v2.compute_gate_report(root)
    assert report["shipped_unstamped"] == [{
        "id": "a", "path": "state/handoffs/a.md", "plan_path": gp, "plan_status": "reviewed",
        "basis": "rows_landed", "coded_sha": "bbb2222",
    }]
    assert by_id(report)["a"]["candidate"] is False


def test_an_open_row_keeps_the_baton_a_candidate(root):
    gp = _plan(root, "p", [_coded(), "open"])
    baton(root, "a", governing_plan=gp)
    report = gate_v2.compute_gate_report(root)
    assert report["shipped_unstamped"] == []
    assert by_id(report)["a"]["candidate"] is True


def test_terminal_plan_entry_carries_plan_terminal_basis(root):
    gp = _plan(root, "p", ["open"], status="implemented")
    baton(root, "a", governing_plan=gp)
    entry = gate_v2.compute_gate_report(root)["shipped_unstamped"][0]
    assert entry["basis"] == "plan_terminal" and "coded_sha" not in entry


def test_landed_plan_makes_the_baton_coded_so_its_blockers_do_not_close_it(root):
    gp = _plan(root, "p", [_coded()])
    baton(root, "a", governing_plan=gp, blocked_by=["no-such-record"])
    row = gate_v2.compute_gate_report(root, subject="a")["batons"][0]
    assert row["planning_gate"]["open"] is True and row["execution_gate"]["open"] is True


def test_a_plan_shared_by_two_batons_is_read_once(root, monkeypatch):
    gp = _plan(root, "p", [_coded()])
    baton(root, "a", governing_plan=gp)
    baton(root, "b", governing_plan=gp)
    import coordinator_core.roadmap.baton_rows_landed as brl

    calls = []
    real = brl.rows_landed
    monkeypatch.setattr(brl, "rows_landed", lambda text: calls.append(1) or real(text))
    report = gate_v2.compute_gate_report(root)
    assert [e["id"] for e in report["shipped_unstamped"]] == ["a", "b"]
    assert len(calls) == 1
