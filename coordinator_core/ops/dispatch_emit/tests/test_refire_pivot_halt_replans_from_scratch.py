"""A refire of a run halted on a PIVOT verdict replans from scratch: the gate carries `pivot`, the composed
ledger-replan prompt branches on it, and `ask.stage` retires the superseded open rows beside the new ones."""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate
from coordinator_core.ops.dispatch_emit.ask_compose import (
    _LEDGER_REPLAN_ADD_ONLY_CLAUSE,
    _LEDGER_REPLAN_PIVOT_CLAUSE,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_PIVOT_FIELD,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as stage_handler
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import _row
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "ask-20261124T154449-1dabff"
REFIRE_RUN = "warp-2026-11-24-refire-20261124T160000-aaaaaa"
REGISTER = {"sources": [{"doc": "ask", "ref": "ask"}], "rows": [{"id": "R1", "source_text": "r", "surface": "internal", "status": "open"}]}
SUPERSEDED = ["C1", "C2", "C3", "C4", "C5"]
NEW_ROWS = ["C8", "C9", "C10"]
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _pulled(verdict: str) -> dict:
    return {"pulled": [{"reviews": [{"reviewer": "sid", "verdict": verdict}]}]}


PIVOT_BLITZ = {"blitz": _pulled("PIVOT")}
PIVOT_MARKER_ONLY = {RESUME_LEDGER_PIVOT_FIELD: True}
BLOCKED_BLITZ = {"blitz": _pulled("BLOCKED")}


def _incident(repo, extra: dict) -> None:
    """The incident shape: C0 landed by the halt commit, C1-C5 open, the halted run's record carrying `extra`."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C0", "open")] + [(c, "open") for c in SUPERSEDED])
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
                      blocking_items=[{"id": "R1", "remaining": ["replan"], "evidence": "pivot"}])
    halted = record / "halted.json"
    body = json.loads(halted.read_text(encoding="utf-8"))
    halted.write_text(json.dumps({**body, **extra}), encoding="utf-8")
    (record / "manifest.json").write_text(json.dumps({"rows": [{"id": c} for c in ["C0"] + SUPERSEDED]}), encoding="utf-8")
    _git(repo, "add", "-A")
    message = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", f"{message}\nLands-Chunks: C0\n")
    write_ledger_halt(
        ledger_halt_path(resolve_git_common_dir(repo), PLAN_REL), LedgerHalt(HALTED_RUN, PLAN_REL, REL, ("R1",))
    )


def _resume(repo) -> dict:
    return ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)["resume_ledger"]


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


_JS_TOKEN = re.compile(r"'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`|(\w+)")


def _value(script: str, expr: str) -> str:
    """The string a script expression evaluates to: a `+`-chain of quoted literals and hoisted constants."""
    parts = []
    for single, tick, name in _JS_TOKEN.findall(expr):
        if name:
            decl = re.search(rf"^const {re.escape(name)} = (.*);$", script, re.MULTILINE)
            assert decl, f"{name} is not declared in the composed script"
            parts.append(_value(script, decl.group(1)))
        else:
            parts.append(re.sub(r"\\(.)", r"\1", single or tick))
    return "".join(parts)


def _selected_clauses(script: str) -> tuple[str, str]:
    """The (pivot, add-only) prompt clauses the ledger-replan leg picks between on the resume ledger's pivot field."""
    operand = r"(\w+|'(?:[^'\\]|\\.)*')"
    ternary = re.search(rf"\(_gate\.resume_ledger\?\.pivot === true \? {operand} : {operand}\)", script)
    assert ternary, "the replan prompt does not select a clause on the resume ledger's pivot field"
    return _value(script, ternary.group(1)), _value(script, ternary.group(2))


def _rewrite_spine(repo, rows: list[tuple[str, str]]) -> None:
    body = "".join(_row(rid, f"  disposition: {disp}\n") for rid, disp in rows)
    (repo / PLAN_REL).write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def _stage(repo, resume: dict, only_rows: list[str]) -> dict:
    """The params the composed ledger-replan leg builds: `pivot_resume` rides exactly when the ledger carries pivot."""
    params = {
        "run_id": REFIRE_RUN, "plan_path": PLAN_REL, "ledger_resume": True, "only_rows": only_rows,
        **({"pivot_resume": True} if resume.get(RESUME_LEDGER_PIVOT_FIELD) is True else {}),
    }
    return read_stage_reply(repo, stage_handler(params, repo_root=repo))


@pytest.mark.parametrize("extra", [PIVOT_BLITZ, PIVOT_MARKER_ONLY], ids=["blitz-pulled", "top-level-marker"])
def test_a_pivot_halted_refire_stages_only_the_new_rows_and_retires_the_superseded(repo, monkeypatch, extra):  # noqa: F811
    _incident(repo, extra)
    resume = _resume(repo)
    assert resume[RESUME_LEDGER_PIVOT_FIELD] is True

    script = _script(monkeypatch)
    assert _selected_clauses(script) == (_LEDGER_REPLAN_PIVOT_CLAUSE, _LEDGER_REPLAN_ADD_ONLY_CLAUSE)
    assert "...(_gate.resume_ledger?.pivot === true ? { pivot_resume: true } : {})" in script

    _rewrite_spine(repo, [("C0", "coded")] + [(c, "open") for c in NEW_ROWS])
    reply = _stage(repo, resume, NEW_ROWS + SUPERSEDED)
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == set(NEW_ROWS)
    assert reply["retired_resume_rows"] == SUPERSEDED


def test_the_superseded_rows_are_refused_as_unknown_without_the_pivot_flag(repo):  # noqa: F811
    _incident(repo, PIVOT_BLITZ)
    _rewrite_spine(repo, [("C0", "coded")] + [(c, "open") for c in NEW_ROWS])
    reply = _stage(repo, {}, NEW_ROWS + SUPERSEDED)
    assert "C1" in reply["error"]


def test_a_blocked_review_keeps_the_add_only_replan_and_stages_the_open_rows_beside_the_new_ones(repo, monkeypatch):  # noqa: F811
    _incident(repo, BLOCKED_BLITZ)
    resume = _resume(repo)
    assert RESUME_LEDGER_PIVOT_FIELD not in resume

    script = _script(monkeypatch)
    assert _selected_clauses(script)[1] == _LEDGER_REPLAN_ADD_ONLY_CLAUSE

    _rewrite_spine(repo, [("C0", "coded")] + [(c, "open") for c in SUPERSEDED + NEW_ROWS])
    reply = _stage(repo, resume, SUPERSEDED + NEW_ROWS)
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == set(SUPERSEDED + NEW_ROWS)
    assert reply["retired_resume_rows"] == []
