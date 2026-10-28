"""A refire of a halted run stages every open row the plan holds now, rows added after the halt included:
the gate stages them itself, or releases the run's stage receipt so the next `dispatch.ask_stage` does;
it never replays a manifest that omits them."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage, ask_stage_receipt, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    PLAN,
    SIZING,
    halted,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _write
from coordinator_core.ops.dispatch_emit.tests.test_plain_resume_refire_stages_current_rows import (
    PLAN_REL as PLAIN_PLAN,
    _plan as _plain_plan,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261027T000000-bbbbbb"
_ADDED = "- id: C2\n  title: added after the halt\n  disposition: open\n  writes:\n    - b.py\n  body: |\n    b\n"


def _add_row(root) -> None:
    text = (root / PLAN).read_text(encoding="utf-8")
    assert text.count("```") == 2
    _write(root, PLAN, text.replace("```\n", _ADDED + "```\n", 1))


def _staged_ids(root, reply) -> list:
    handle = reply.get("manifest_handle")
    assert handle, reply
    return sorted(r["id"] for r in reply_sidecar.read_reply(root, handle)["rows"])


def test_ledger_halt_refire_stages_a_row_added_after_the_halt(halted):  # noqa: F811
    _add_row(halted)
    out = _handler({"sizing_path": SIZING, "run_id": "refire1", "writes": ["b.py"]}, repo_root=halted)
    assert out["halt"] is None, out
    assert "C2" in _staged_ids(halted, out)


def test_stage_after_the_refire_gate_is_not_a_replay_of_the_pre_halt_manifest(halted):  # noqa: F811
    params = {"run_id": "refire2", "plan_path": PLAN, "writes": ["b.py"]}
    first = ask_stage._handler(params, repo_root=halted)
    assert "C2" not in _staged_ids(halted, first)

    _add_row(halted)
    out = _handler({"sizing_path": SIZING, "run_id": "refire2", "writes": ["b.py"]}, repo_root=halted)
    assert out["halt"] is None, out
    second = ask_stage._handler(params, repo_root=halted)
    assert "C2" in _staged_ids(halted, second), second


@pytest.fixture
def m_repo(_bare_repo, monkeypatch):
    (_bare_repo / ".git").mkdir()
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a, **k: False)
    return _bare_repo


def _script_params():
    return {"run_id": RUN, "plan_path": PLAIN_PLAN, "writes": ["pkg/C1.py"], "commit_sizing_path": REL}


def _m_fire(repo):
    return _handler({"sizing_path": REL, "run_id": RUN, "writes": ["pkg/C1.py"]}, repo)


def test_unready_resume_gate_releases_a_receipt_whose_manifest_omits_an_open_row(m_repo):
    _put(m_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAIN_PLAN)
    _plain_plan(m_repo, {"C1": "open"})
    assert _staged_ids(m_repo, ask_stage._handler(_script_params(), m_repo)) == ["C1"]

    _plain_plan(m_repo, {"C1": "open", "C2": "open"})
    out = _m_fire(m_repo)
    assert out["halt"] is None and out["restage_rows"] == ["C2"], out
    assert _staged_ids(m_repo, ask_stage._handler(_script_params(), m_repo)) == ["C1", "C2"]


def test_unready_resume_gate_leaves_a_receipt_whose_manifest_covers_every_open_row(m_repo):
    _put(m_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAIN_PLAN)
    _plain_plan(m_repo, {"C1": "open"})
    ask_stage._handler(_script_params(), m_repo)

    out = _m_fire(m_repo)
    assert out["halt"] is None and "restage_rows" not in out, out
    replay = ask_stage._handler(_script_params(), m_repo)
    assert replay.get(ask_stage_receipt.ALREADY_STAGED_KEY) is True


def _widen_c1(repo) -> None:
    path = repo / PLAIN_PLAN
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("    - pkg/C1.py\n", "    - pkg/C1.py\n    - pkg/extra.py\n", 1), encoding="utf-8", newline="\n")


def _row_writes(root, reply) -> list:
    rows = reply_sidecar.read_reply(root, reply["manifest_handle"])["rows"]
    return sorted(w for r in rows if r["id"] == "C1" for w in r["writes"])


def test_unready_resume_gate_releases_a_receipt_whose_row_writes_omit_a_widened_plan_write(m_repo):
    _put(m_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAIN_PLAN)
    _plain_plan(m_repo, {"C1": "open"})
    assert "pkg/extra.py" not in _row_writes(m_repo, ask_stage._handler(_script_params(), m_repo))

    _widen_c1(m_repo)
    out = _m_fire(m_repo)
    assert out["halt"] is None and out["restage_rows"] == ["C1"], out
    restaged = ask_stage._handler(_script_params(), m_repo)
    assert restaged.get(ask_stage_receipt.ALREADY_STAGED_KEY) is not True
    assert "pkg/extra.py" in _row_writes(m_repo, restaged)
