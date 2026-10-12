"""
coordinator_core/roadmap/tests/test_prep_gate_register.py — the REGISTER class.

Subject: `prep_gate._register`, which refuses a plan whose `register_claims` names
a sizing with no register, a row id the register lacks, or a register whose
deferred/waived row carries no ruling.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg

_SIZING_REL = "state/sizings/2026-10-09-fixture.yaml"

_SPINE = """- id: C1
  title: Ship the thing
  body: Add the SPINE predicate and pin it with a failing-first test.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: [coordinator_core/roadmap/prep_gate.py]
  queue_scope: project
  disposition: open
"""

_RULING = "    ruling: {source: pm, quote: later}\n"


def _register_text(*rows: tuple[str, str, bool]) -> str:
    out = ["title: fixture-sizing", "requirement_register:", "  rows:"]
    for rid, status, ruled in rows:
        out.append(f"  - id: {rid}")
        out.append(f"    status: {status}")
        if ruled:
            out.append(_RULING.rstrip("\n"))
    return "\n".join(out) + "\n"


def _plan(root: Path, claims: str, sizing_object: str = "") -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    fm = [
        "title: fixture",
        "status: draft",
        "created: 2026-10-09",
        "author: fixture-session",
        "census: []",
        "prime_exit_criterion:",
        "  statement: the register resolves",
        f"  derived_from: {_SIZING_REL}",
    ]
    if sizing_object:
        fm.append(f"sizing_object: {sizing_object}")
    if claims:
        fm.append(claims)
    body = ["", "# Fixture", "", "## Tasks", "", "```yaml plan-tasks", _SPINE.strip(), "```", ""]
    path = plans / "2026-10-09-fixture.md"
    path.write_text("---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body), encoding="utf-8")
    return path


def _claims(rows: str = "[R1]", sizing: str = _SIZING_REL) -> str:
    return f"register_claims:\n  sizing: {sizing}\n  rows: {rows}"


def _sizing(root: Path, text: str) -> None:
    path = root / _SIZING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _gate(root: Path, path: Path) -> dict:
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    return pg.gate_plan(root, path)


def test_no_claims_is_not_applicable(tmp_path):
    report = _gate(tmp_path, _plan(tmp_path, ""))
    assert report["classes"]["REGISTER"]["status"] == "PASS"
    assert "not applicable" in report["classes"]["REGISTER"]["detail"]


def test_absent_sizing(tmp_path):
    reg = _gate(tmp_path, _plan(tmp_path, _claims()))["classes"]["REGISTER"]
    assert reg["status"] == "DEFECT" and reg["kind"] == "register-sizing-missing"


def test_sizing_without_register(tmp_path):
    _sizing(tmp_path, "title: fixture-sizing\n")
    reg = _gate(tmp_path, _plan(tmp_path, _claims()))["classes"]["REGISTER"]
    assert reg["kind"] == "register-absent"


def test_unknown_id_named(tmp_path):
    _sizing(tmp_path, _register_text(("R1", "open", False)))
    reg = _gate(tmp_path, _plan(tmp_path, _claims("[R1, R9]")))["classes"]["REGISTER"]
    assert reg["kind"] == "register-row-unknown" and "R9" in reg["detail"]


def test_deferred_without_ruling(tmp_path):
    _sizing(tmp_path, _register_text(("R1", "open", False), ("R2", "deferred", False)))
    reg = _gate(tmp_path, _plan(tmp_path, _claims()))["classes"]["REGISTER"]
    assert reg["kind"] == "register-row-unruled" and "R2" in reg["detail"]


def test_waived_without_ruling(tmp_path):
    _sizing(tmp_path, _register_text(("R1", "open", False), ("R3", "waived", False)))
    reg = _gate(tmp_path, _plan(tmp_path, _claims()))["classes"]["REGISTER"]
    assert reg["kind"] == "register-row-unruled" and "R3" in reg["detail"]


def test_ruled_deferred_passes(tmp_path):
    _sizing(tmp_path, _register_text(("R1", "open", False), ("R2", "deferred", True)))
    reg = _gate(tmp_path, _plan(tmp_path, _claims()))["classes"]["REGISTER"]
    assert reg["status"] == "PASS" and "1 claimed row(s)" in reg["detail"]


def test_malformed_claims_pass_here(tmp_path):
    plan = _plan(tmp_path, "register_claims:\n  sizing: elsewhere/x.yaml\n  rows: [R1]")
    report = _gate(tmp_path, plan)
    assert report["classes"]["REGISTER"]["status"] == "PASS"
    assert "SCHEMA reports it" in report["classes"]["REGISTER"]["detail"]


def test_refusal_carries_register_fix_not_converter(tmp_path):
    report = _gate(tmp_path, _plan(tmp_path, _claims()))
    failing = [k for k, v in report["classes"].items() if v["status"] != "PASS"]
    assert failing == ["REGISTER"]
    msg = report["message"] if "message" in report else pg.refusal_message(
        Path("p.md"), report["verdict"], report["classes"], report["withheld_rows"]
    )
    assert "sizing-assemble --register" in msg
    assert pg._upgrade_fix_line().strip() not in msg


def test_op_reply_carries_register_class(tmp_path):
    from coordinator_core.ops import plan_prep_gate as op

    (tmp_path / ".git").mkdir()
    plan = _plan(tmp_path, _claims())
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    reply = op._handler({"plan": plan.relative_to(tmp_path).as_posix()}, tmp_path / ".git")
    assert reply["classes"]["REGISTER"]["kind"] == "register-sizing-missing"


def _reg(root: Path, plan_claims: str, sizing_text: str, sizing_object: str = _SIZING_REL) -> dict:
    _sizing(root, sizing_text)
    return _gate(root, _plan(root, plan_claims, sizing_object))["classes"]["REGISTER"]


def test_register_bearing_sizing_object_without_claims_refused(tmp_path):
    reg = _reg(tmp_path, "", _register_text(("R1", "open", False)))
    assert reg["status"] == "DEFECT" and reg["kind"] == "register-claims-missing"


def test_rows_omitting_a_register_row_refused_naming_it(tmp_path):
    text = _register_text(("R1", "open", False), ("R2", "open", False))
    reg = _reg(tmp_path, _claims("[R1]"), text)
    assert reg["kind"] == "register-rows-unclaimed" and "R2" in reg["detail"]
    assert "R1" not in reg["detail"].split(":")[-1]


def test_rows_covering_every_register_row_pass(tmp_path):
    text = _register_text(("R1", "open", False), ("R2", "open", False))
    assert _reg(tmp_path, _claims("[R1, R2]"), text)["status"] == "PASS"


def test_non_register_bearing_sizing_object_without_claims_passes(tmp_path):
    reg = _reg(tmp_path, "", "title: fixture-sizing\n")
    assert reg["status"] == "PASS" and "not applicable" in reg["detail"]


def test_unreadable_sizing_object_without_claims_passes(tmp_path):
    report = _gate(tmp_path, _plan(tmp_path, "", _SIZING_REL))
    assert report["classes"]["REGISTER"]["status"] == "PASS"


def test_seed_plan_subset_claims_on_other_root_still_pass(tmp_path):
    root = "state/sizings/2026-10-09-root.yaml"
    path = tmp_path / root
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_register_text(("R1", "open", False), ("R2", "open", False)), encoding="utf-8")
    seed = "state/sizings/2026-10-09-seed.yaml"
    (tmp_path / seed).write_text("title: seed\n", encoding="utf-8")
    plan = _plan(tmp_path, _claims("[R1]", sizing=root), seed)
    assert _gate(tmp_path, plan)["classes"]["REGISTER"]["status"] == "PASS"


def test_register_in_class_order_and_authoring_lines():
    assert pg.CLASS_ORDER[-1] == "REGISTER"
    assert any("sizing-assemble --register" in ln for ln in pg._authoring_fix_lines({"REGISTER"}))
