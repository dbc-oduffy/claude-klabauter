"""A refire of a sizing that cites an existing plan continues that plan, or halts typed; it never re-scaffolds.

Live case: warp run e99a42 refired with `docs/plans/...-2994e8.md` at `status: executing`. The gate relay
returned the op's JSON-RPC envelope (only `arm` and `halt` hoisted), the script read no `resume_plan`,
plan-blitz was handed no plan, scaffolded, and was refused because the sizing already cites the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_gate import HALT_PLAN_NOT_CONTINUABLE, gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

PLAN_REL = "docs/plans/2026-10-01-cited.md"


def _plan(repo, status: str, *, spine: bool = False) -> None:
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "## Notes\n\nno task spine here\n"
    path.write_text(f"---\nstatus: {status}\n---\n\n{body}", encoding="utf-8", newline="\n")


@pytest.fixture
def cited(repo):  # noqa: F811
    (repo / ".git").mkdir()
    _put(repo, tshirt="L", route="plan", plan=PLAN_REL)
    return repo


@pytest.mark.parametrize("status", ["approved", "ready", "executing"])
def test_live_cited_plan_is_continued_in_place(cited, status) -> None:
    _plan(cited, status)

    v = gate(cited, REL)

    assert v.halt is None and v.arm == "m_plus", v.halt
    assert v.resume_plan == PLAN_REL


@pytest.mark.parametrize("status", ["completed", "abandoned"])
def test_ended_cited_plan_halts_typed_naming_path_status_and_next_action(cited, status) -> None:
    _plan(cited, status)

    v = gate(cited, REL)

    assert v.arm is None and v.halt["kind"] == HALT_PLAN_NOT_CONTINUABLE, v.halt
    assert v.halt["plan"] == PLAN_REL and v.halt["plan_status"] == status
    assert PLAN_REL in v.halt["reason"] and v.halt["next_action"]


def test_missing_cited_plan_halts_typed_on_a_refire_only(cited, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import halted_run

    fresh = gate(cited, REL)
    assert fresh.halt is None and fresh.resume_plan is None

    monkeypatch.setattr(halted_run, "halted_ask_run_for", lambda root, sizing_rel: "warp-prior")
    v = gate(cited, REL)

    assert v.halt["kind"] == HALT_PLAN_NOT_CONTINUABLE and v.halt["plan"] == PLAN_REL
    assert v.halt["plan_status"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_gate_relay_envelope_is_unwrapped_to_top_level_keys() -> None:
    envelope = {
        "jsonrpc": "2.0", "id": 1, "arm": "m_plus", "halt": None,
        "result": {"arm": "m_plus", "halt": None, "baton": {"id": "b1"}, "resume_plan": PLAN_REL, "resume_rows": ["G"]},
    }
    js = ask_compose._GATE_UNWRAP_JS + f"\nconsole.log(JSON.stringify(_gateUnwrap({json.dumps(envelope)})));"
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, check=True, timeout=30,
        **no_console_creationflags(),
    )
    got = json.loads(out.stdout)

    assert got["resume_plan"] == PLAN_REL and got["baton"] == {"id": "b1"} and got["arm"] == "m_plus"
    assert "result" not in got and "jsonrpc" not in got


def test_composed_script_unwraps_the_gate_reply() -> None:
    assert "const _gateUnwrap =" in ask_compose._GATE_UNWRAP_JS
    src = open(ask_compose.__file__, encoding="utf-8").read()
    assert 'f"  let _gate = _gateUnwrap({_gate_call});"' in src
