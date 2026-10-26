"""dispatch.objective_check: deterministic falsifier run, pin/ruler guards, acceptance axes, append-only loop."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import objective_check as chk
from coordinator_core.ops.dispatch_emit import objective_contract as oc

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "run-1"
SCRIPT = "tools/falsifier.py"


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags())
    return out.stdout


def _falsifier(a_pass: bool = True, b_pass: bool = True, *, drop_b: bool = False, garbage: bool = False,
               sleep: float = 0.0, command_b: str = "") -> str:
    lines = [{"axis": "a", "kind": "measure", "command": "x", "base": 9, "now": 1, "threshold": 2,
              "comparator": "<=", "pass": a_pass, "evidence": "e"}]
    if not drop_b:
        lines.append({"axis": "b", "kind": "behaviour", "command": command_b, "base": None, "now": None,
                      "threshold": None, "comparator": "bool", "pass": b_pass, "evidence": ""})
    failing = [ln["axis"] for ln in lines if not ln["pass"]]
    if drop_b:
        failing.append("b")
    summary = {"summary": True, "pass": not failing, "failing": failing}
    out = [json.dumps(ln) for ln in lines] + ([json.dumps(summary)] if not garbage else ["not json"])
    return ("import sys, time\n" f"time.sleep({sleep})\n"
            f"print({chr(10).join(out)!r})\n" f"sys.exit({0 if not failing and not garbage else 1})\n")


def _make(tmp_path: Path, script_text: str, *, pin: bool = True, plan_rows: str = "") -> Path:
    _git(tmp_path, "init", "-q")
    script = tmp_path / SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text(script_text, encoding="utf-8")
    run_dir = tmp_path / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / oc.BASE_FILE).write_text("{}", encoding="utf-8")
    obj = oc.Objective(verbatim="v", falsifier_script=SCRIPT, axes=[oc.Axis(id="a"), oc.Axis(id="b")],
                       falsifier_sha256=hashlib.sha256(script_text.encode()).hexdigest() if pin else None,
                       pinned_at=oc.PINNED_AT_SIZING if pin else None)
    (run_dir / oc.OBJECTIVE_FILE).write_text(json.dumps(obj.to_json()), encoding="utf-8")
    if plan_rows:
        plan = tmp_path / "plan.md"
        plan.write_text("## Tasks\n\n```yaml plan-tasks\n" + plan_rows + "```\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return run_dir


def _call(tmp_path: Path, **extra) -> dict:
    params = {"run_id": RUN, "wave": 1, "iteration": 0, "spend_usd": 1.5, "diff_range": "", "plan_path": ""}
    params.update(extra)
    return chk._handler(params, repo_root=tmp_path)


def _loop(run_dir: Path) -> list:
    return [json.loads(x) for x in (run_dir / oc.LOOP_FILE).read_text(encoding="utf-8").splitlines()]


def test_pass_records_a_check_line(tmp_path: Path) -> None:
    run_dir = _make(tmp_path, _falsifier())
    res = _call(tmp_path, check_cost_usd=0.0, check_llm_calls=0)
    assert res["status"] == "met" and res["failing"] == []
    (line,) = _loop(run_dir)
    assert line["kind"] == oc.LINE_CHECK and line["wave"] == 1 and line["spend_usd"] == 1.5
    assert line["check_cost_usd"] == 0.0 and line["check_llm_calls"] == 0
    assert oc.close_verdict(_loop(run_dir))["status"] == oc.STATUS_DONE


def test_fail_returns_raw_failing_lines(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(a_pass=False))
    res = _call(tmp_path)
    assert res["status"] == "not_met"
    assert [f["axis"] for f in res["failing"]] == ["a"] and res["failing"][0]["pass"] is False


def test_garbage_line_fails_every_axis(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(garbage=True))
    res = _call(tmp_path)
    assert res["status"] == "not_met" and {a["axis"] for a in res["failing"]} == {"a", "b"}
    assert all(a["reason"] for a in res["failing"])


def test_missing_axis_is_not_met(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(drop_b=True))
    res = _call(tmp_path)
    assert [f["axis"] for f in res["failing"]] == ["b"]
    assert res["failing"][0]["reason"] == oc.REASON_MISSING_AXIS


def test_timeout_fails_every_axis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make(tmp_path, _falsifier(sleep=5))
    monkeypatch.setattr(oc, "FALSIFIER_TIMEOUT_S", 0.5)
    res = _call(tmp_path)
    assert res["status"] == "not_met"
    assert {a["reason"] for a in res["failing"]} == {oc.REASON_TIMEOUT}


def test_loop_file_is_append_only(tmp_path: Path) -> None:
    run_dir = _make(tmp_path, _falsifier())
    _call(tmp_path)
    first = (run_dir / oc.LOOP_FILE).read_text(encoding="utf-8")
    _call(tmp_path, wave=2)
    second = (run_dir / oc.LOOP_FILE).read_text(encoding="utf-8")
    assert second.startswith(first) and len(_loop(run_dir)) == 2


def test_unpinned_is_not_met_and_never_runs(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(), pin=False)
    res = _call(tmp_path)
    assert {a["reason"] for a in res["failing"]} == {oc.REASON_UNPINNED}


def test_sha_drift_is_ruler_changed(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier())
    (tmp_path / SCRIPT).write_text(_falsifier(a_pass=False), encoding="utf-8")
    res = _call(tmp_path)
    assert res["status"] == "not_met"
    assert {a["reason"] for a in res["failing"]} == {oc.REASON_RULER_CHANGED}


@pytest.mark.parametrize("touched", [SCRIPT, f"state/warp/{RUN}/{oc.BASE_FILE}"])
def test_diff_touching_the_ruler_is_ruler_changed(tmp_path: Path, touched: str) -> None:
    _make(tmp_path, _falsifier())
    (tmp_path / touched).write_text((tmp_path / touched).read_text() + "\n# x\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "touch ruler")
    res = _call(tmp_path, diff_range="HEAD~1..HEAD")
    assert res["status"] == "not_met"
    assert {a["reason"] for a in res["failing"]} == {oc.REASON_RULER_CHANGED}


def test_diff_not_touching_the_ruler_runs_the_script(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier())
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "other.txt")
    _git(tmp_path, "commit", "-q", "-m", "other")
    assert _call(tmp_path, diff_range="HEAD~1..HEAD")["status"] == "met"


def test_haiku_verdict_passes_only_a_null_command_behaviour_axis(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(b_pass=False))
    assert _call(tmp_path, haiku_axes={"b": {"pass": True, "evidence": "ok"}}, check_llm_calls=1)["status"] == "met"
    run_dir = tmp_path / "state" / "warp" / RUN
    assert _loop(run_dir)[0]["check_llm_calls"] == 1


def test_haiku_cannot_pass_an_axis_that_has_a_command(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(b_pass=False, command_b="do-it"))
    res = _call(tmp_path, haiku_axes={"b": True})
    assert [f["axis"] for f in res["failing"]] == ["b"]


_RED = (
    "- id: R34\n  title: t\n  acceptance: 'python -c \"import sys; sys.exit(1)\"'\n"
    "- id: R40\n  title: t\n  acceptance: '`python -c \"import sys; sys.exit(2)\"`'\n"
    "- id: OK\n  title: t\n  acceptance: 'python -c \"pass\"'\n"
    "- id: NONE\n  title: t\n"
)


def test_plan_acceptance_commands_are_axes_and_red_ones_fail(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier(), plan_rows=_RED)
    res = _call(tmp_path, plan_path="plan.md")
    assert res["status"] == "not_met"
    assert sorted(f["axis"] for f in res["failing"]) == ["acceptance:R34", "acceptance:R40"]
    ok = next(a for a in res["axes"] if a["axis"] == "acceptance:OK")
    assert ok["pass"] is True and ok["kind"] == oc.KIND_BEHAVIOUR
    assert not any(a["axis"] == "acceptance:NONE" for a in res["axes"])


def test_acceptance_timeout_is_not_met(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = "- id: SLOW\n  title: t\n  acceptance: 'python -c \"import time; time.sleep(5)\"'\n"
    _make(tmp_path, _falsifier(), plan_rows=rows)
    monkeypatch.setattr(oc, "FALSIFIER_TIMEOUT_S", 1.0)
    res = _call(tmp_path, plan_path="plan.md")
    assert [f["reason"] for f in res["failing"]] == [oc.REASON_TIMEOUT]


def test_engine_overhead_excludes_the_child_and_stays_under_budget(tmp_path: Path) -> None:
    _make(tmp_path, _falsifier())
    start = time.process_time()
    assert _call(tmp_path)["status"] == "met"
    assert time.process_time() - start < 0.5
