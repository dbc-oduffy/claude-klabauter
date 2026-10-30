"""A stop_rule halt names the stopping row and its reply in the reason and in returns.json."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, row_returns
from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import kept_work as k
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OPEN = "  let _stoppedRows = [];\n"
_CLOSE = "stopped_rows: _stoppedRows }; }\n"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def _wrap(script: str) -> str:
    start = script.index(_OPEN)
    return script[start : script.index(_CLOSE, start) + len(_CLOSE)]


def _run_wrap(wrap: str, stopped: list, halted, rows: dict, by_hold: bool = False, consumed: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = (
        "(async () => {\n"
        f"const _stoppedBy = {json.dumps(stopped)}; const _haltedByHold = {json.dumps(by_hold)};\n"
        f"let _halted = {json.dumps(halted)}; let _consumedReturns = {json.dumps(consumed or {})};\n"
        f"const _rows = {{}}; for (const [k, v] of Object.entries({json.dumps(rows)})) _rows[k] = Promise.resolve(v);\n"
        "const _sizingRel = 's.yaml', _runId = 'r';\n"
        + wrap
        + "\nconsole.log(JSON.stringify({ halted: _halted, consumed: _consumedReturns }));\n})();"
    )
    proc = subprocess.run(
        [node, "-e", prog], capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


def test_the_reason_names_the_row_and_its_reply_and_the_entry_joins_the_returns(monkeypatch):
    out = _run_wrap(_wrap(_script(monkeypatch)), ["C1"], "C1", {"C1": "BLOCKED: C1 needs the seam\nSTOP-RULE-FIRED: x"})
    h = out["halted"]
    assert h["halted"] == "stop_rule" and h["stopped_rows"] == ["C1"]
    assert h["reason"].startswith("stop rule fired on C1: BLOCKED: C1 needs the seam STOP-RULE-FIRED: x")
    assert out["consumed"] == {"C1": "BLOCKED: C1 needs the seam\nSTOP-RULE-FIRED: x"}


def test_a_row_that_returned_nothing_says_so_in_both(monkeypatch):
    out = _run_wrap(_wrap(_script(monkeypatch)), ["C1"], "C1", {"C1": None})
    assert out["halted"]["reason"] == "stop rule fired on C1: returned nothing"
    assert out["consumed"] == {"C1": "returned nothing"}


def test_the_reply_in_the_reason_is_truncated_and_a_consumed_entry_is_kept(monkeypatch):
    out = _run_wrap(
        _wrap(_script(monkeypatch)), ["C1"], "C1", {"C1": "x" * 5000}, consumed={"C1": "DONE: kept"}
    )
    assert len(out["halted"]["reason"]) < 700
    assert out["consumed"] == {"C1": "DONE: kept"}


def test_a_held_dependents_halt_keeps_its_reason_and_adds_no_return(monkeypatch):
    out = _run_wrap(
        _wrap(_script(monkeypatch)), ["C1"], "dependents unstarted: Y (held by X)", {"C1": "STOP"}, by_hold=True
    )
    assert out["halted"]["halted"] == "dependents_unstarted"
    assert out["halted"]["reason"] == "dependents unstarted: Y (held by X)"
    assert out["halted"]["stopped_rows"] == [] and out["consumed"] == {}


def test_the_consumed_returns_fill_site_stays_single(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("_consumedReturns[") == 1
    assert script.index(_OPEN) > script.index("await Promise.all(_verifications);")


def test_the_record_writes_the_stopped_row_return_but_does_not_keep_it_as_finished(tmp_path):
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "rec").mkdir()
    halted = {"halted": "stop_rule", "reason": "stop rule fired on C1: BLOCKED", "stopped_rows": ["C1"]}
    paths: list = []
    hrc._record_kept(
        tmp_path, "rec", {}, {}, halted, paths, consumed_returns={"C1": "BLOCKED: seam", "C2": "DONE: ok"}
    )
    got = row_returns.read(tmp_path / "rec")
    assert got["C1"]["text"] == "BLOCKED: seam" and got["C2"]["text"] == "DONE: ok"
    kept = k.read(tmp_path / "rec")
    assert "C1" not in kept["chunks"] and "C2" in kept["chunks"]


def test_without_the_stopped_marker_a_returned_row_is_kept_as_finished(tmp_path):
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "rec").mkdir()
    hrc._record_kept(tmp_path, "rec", {}, {}, {"halted": "usage_limit"}, [], consumed_returns={"C1": "DONE: ok"})
    assert "C1" in k.read(tmp_path / "rec")["chunks"]
