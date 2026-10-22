"""workflow.revise `claim`: a ruling becomes a register row, claimed on the plan with its pin and
recorded on the plan's ledger-halt pointer, in one call; every refusal writes nothing."""

from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    CLAIM_PARAM, LedgerHalt, ledger_halt_path, read_ledger_halt, write_ledger_halt,
)

RUN = "ask-20261022T000000-abc123"
REL = "state/sizings/2026-10-22-ruling.yaml"
PLAN = "docs/plans/2026-10-22-ruling.md"
RULING = "G-EM: also cover the Windows path"


def _repo(tmp_path, *, plan_edge=True, claims_sizing=REL, crlf=False, halt=True, bind=True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    if plan_edge:
        sizing["plan"] = PLAN
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {
        "title": "p",
        "register_claims": {"sizing": claims_sizing, "rows": ["R1"], "pins": {"R1": rr.row_pin("build the op")}},
    }
    text = "---\n" + yaml.safe_dump(fm) + "---\n# p\n"
    (tmp_path / PLAN).write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    rl.start(tmp_path, RUN)
    if bind:
        rl.bind(tmp_path, RUN, REL)
    if halt:
        pointer = ledger_halt_path(tmp_path, PLAN)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        write_ledger_halt(pointer, LedgerHalt(run_id=RUN, plan_rel=PLAN, sizing_rel=REL, blocking=("R1",)))
    return tmp_path


def _claim(root, **extra):
    return rl._handler({"run_id": RUN, "verbatim": RULING, "from": "g-em", CLAIM_PARAM: True, **extra}, repo_root=root)


def _bytes(root):
    return (root / REL).read_bytes(), (root / PLAN).read_bytes()


def test_claim_mints_claims_pins_and_rules_in_one_call(tmp_path):
    root = _repo(tmp_path)
    out = _claim(root)
    assert out["ok"] is True and out["item"] == "R2" and out["claimed"] == ["R2"]
    assert "unclaimed_rows" not in out and "warning" not in out
    assert out.get("next", {}).get("kind") != "revise-plan"
    register = rr.read_register((root / REL).read_text(encoding="utf-8"))
    row = [r for r in register.rows if r[rr.ROW_ID] == "R2"][0]
    assert row[rr.ROW_STATUS] == rr.STATUS_OPEN
    assert row[rr.ROW_ANCHOR].startswith("revision@") and row[rr.ROW_ANCHOR].endswith(" from g-em")
    plan_fm = (root / PLAN).read_text(encoding="utf-8").split("---")[1]
    sizing_rel, claimed = rr.plan_claims(plan_fm)
    assert sizing_rel == REL and set(claimed) == {"R1", "R2"}
    assert rr.row_pin(RULING) in plan_fm
    halt = read_ledger_halt(ledger_halt_path(root, PLAN))
    assert halt.ruled == ("R2",) and halt.blocking == ("R1",)


def test_claim_keeps_crlf_in_the_plan(tmp_path):
    root = _repo(tmp_path, crlf=True)
    assert _claim(root)["claimed"] == ["R2"]
    raw = (root / PLAN).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_claim_without_a_halt_pointer_writes_none(tmp_path):
    root = _repo(tmp_path, halt=False)
    assert _claim(root)["claimed"] == ["R2"]
    assert not ledger_halt_path(root, PLAN).exists()


def test_two_claims_accumulate_on_the_pointer(tmp_path):
    root = _repo(tmp_path)
    _claim(root)
    rl._handler({"run_id": RUN, "verbatim": "second ruling", CLAIM_PARAM: True}, repo_root=root)
    assert read_ledger_halt(ledger_halt_path(root, PLAN)).ruled == ("R2", "R3")


def _refused_writes_nothing(root, **extra):
    before = _bytes(root)
    pointer = ledger_halt_path(root, PLAN)
    halt_before = pointer.read_bytes() if pointer.exists() else None
    params = {"run_id": RUN, "verbatim": RULING, CLAIM_PARAM: True, **extra}
    if extra.get("verbatim") is None and "verbatim" in extra:
        params.pop("verbatim")
    out = rl._handler(params, repo_root=root)
    assert "error" in out
    assert _bytes(root) == before
    assert (pointer.read_bytes() if pointer.exists() else None) == halt_before
    return out


def test_claim_without_verbatim_is_refused(tmp_path):
    out = _refused_writes_nothing(_repo(tmp_path), verbatim=None, note="n")
    assert "verbatim" in out["error"]


def test_claim_on_an_unbound_run_is_refused(tmp_path):
    out = _refused_writes_nothing(_repo(tmp_path, bind=False))
    assert "bound" in out["error"]


def test_claim_with_no_plan_edge_is_refused(tmp_path):
    out = _refused_writes_nothing(_repo(tmp_path, plan_edge=False))
    assert "plan" in out["error"]


def test_claim_when_the_plan_claims_another_sizing_is_refused(tmp_path):
    out = _refused_writes_nothing(_repo(tmp_path, claims_sizing="state/sizings/other.yaml"))
    assert "register_claims" in out["error"]


def test_claim_with_out_of_scope_is_refused(tmp_path):
    out = _refused_writes_nothing(_repo(tmp_path), out_of_scope=["R1"])
    assert "out_of_scope" in out["error"]


def test_without_claim_the_row_stays_unclaimed(tmp_path):
    root = _repo(tmp_path)
    out = rl._handler({"run_id": RUN, "verbatim": RULING}, repo_root=root)
    assert out["unclaimed_rows"] == ["R2"] and "claimed" not in out
    assert read_ledger_halt(ledger_halt_path(root, PLAN)).ruled == ()
