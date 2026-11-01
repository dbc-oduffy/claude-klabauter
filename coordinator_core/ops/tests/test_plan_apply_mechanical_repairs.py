"""
coordinator_core.ops.tests.test_plan_apply_mechanical_repairs — the "plan.apply_mechanical_repairs" op.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_plan_apply_mechanical_repairs.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.plan_apply_mechanical_repairs as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_SIZING_REL = "state/sizings/20260101-a.yaml"
_PLAN_REL = "docs/plans/2026-01-01-a.md"
_SIZING = (
    "schema: sizing-object\n"
    "requirement_register:\n"
    "  sources:\n"
    "  - doc: docs/x.md\n"
    "    ref: abc\n"
    "  rows:\n"
    "  - id: R1\n"
    "    source_text: first wording\n"
    "    surface: api\n"
    "    status: open\n"
    "  - id: R2\n"
    "    source_text: second wording\n"
    "    surface: api\n"
    "    status: open\n"
)
_SPINE = (
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: t\n"
    "  reads_at_head: [src/old/path.py]\n"
    "  writes: [src/a.py]\n"
    "```\n"
)


def _plan(claims: str, body: str = "") -> str:
    return (
        "---\ntitle: t\n"
        f"{claims}"
        "other: keep\n---\n\n# Body\n\n"
        f"{body}Reads src/old/path.py and src/old/path.py.bak.\n\n{_SPINE}"
    )


_CLAIMS_R1 = (
    "register_claims:\n"
    f"  sizing: {_SIZING_REL}\n"
    '  rows: ["R1"]\n'
    f'  pins: {{"R1": "{rr.row_pin("first wording")}"}}\n'
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(_SIZING, encoding="utf-8", newline="\n")
    return tmp_path


def _seed(repo: Path, text: str, newline: str = "\n") -> Path:
    plan = repo / _PLAN_REL
    plan.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return plan


def _run(repo: Path, repairs, **params) -> dict:
    params.setdefault("plan", _PLAN_REL)
    return mod._handler({**params, "repairs": repairs}, repo_root=repo / ".git")


def _fm(plan: Path) -> dict:
    return yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])


def _citation(old="src/old/path.py", new="src/new/path.py") -> dict:
    return {"kind": "premise-citation-corrected", "target": old, "instruction": f"replace {old} with {new}"}


def test_registration_triple() -> None:
    assert OP_CLASSIFICATION["plan.apply_mechanical_repairs"].name == "MUTATING"
    assert OP_MODULE_MAP["plan.apply_mechanical_repairs"] == mod.__name__


def test_citation_replaced_in_body_and_spine_but_not_a_longer_path(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [_citation()])
    assert out["exit_code"] == 0 and out["ok"] is True and out["skipped"] == []
    assert [a["kind"] for a in out["applied"]] == ["premise-citation-corrected"]
    text = plan.read_text(encoding="utf-8")
    assert "reads_at_head: [src/new/path.py]" in text
    assert "Reads src/new/path.py and src/old/path.py.bak." in text


def test_citation_absent_is_skipped_and_bytes_untouched(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    before = plan.read_bytes()
    out = _run(repo, [_citation(old="nowhere.py", new="x.py")])
    assert out["applied"] == [] and "not found" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_register_row_added_by_revise_is_claimed_and_pinned(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [{"kind": "register-rows-unclaimed", "target": "REGISTER", "instruction": "claim"}])
    assert [a["kind"] for a in out["applied"]] == ["register-rows-unclaimed"]
    claims = _fm(plan)["register_claims"]
    assert claims["rows"] == ["R1", "R2"]
    register = rr.read_register(_SIZING)
    assert claims["pins"] == rr.register_pins(register, ["R1", "R2"])
    assert _fm(plan)["other"] == "keep"


def test_register_pin_missing_adds_pins_when_the_plan_has_none(repo):
    claims = f"register_claims:\n  sizing: {_SIZING_REL}\n" '  rows: ["R1", "R2"]\n'
    plan = _seed(repo, _plan(claims))
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert len(out["applied"]) == 1
    register = rr.read_register(_SIZING)
    assert _fm(plan)["register_claims"]["pins"] == rr.register_pins(register, ["R1", "R2"])


def test_register_repair_on_complete_claims_and_pins_is_skipped_not_applied(repo):
    register = rr.read_register(_SIZING)
    pins = rr.register_pins(register, ["R1", "R2"])
    claims = (
        f"register_claims:\n  sizing: {_SIZING_REL}\n"
        '  rows: ["R1", "R2"]\n'
        f"  pins: {pins!r}\n"
    )
    plan = _seed(repo, _plan(claims))
    before = plan.read_bytes()
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert out["applied"] == [] and "already complete" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_register_repair_refuses_a_drifted_pin_and_leaves_the_plan(repo):
    stale = _CLAIMS_R1.replace(rr.row_pin("first wording"), rr.row_pin("older wording"))
    plan = _seed(repo, _plan(stale))
    before = plan.read_bytes()
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert out["applied"] == [] and "plan.repin_register" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_falsifier_row_appended_to_the_spine(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    repair = {"kind": "uncheckable-to-falsifier-row", "target": "A1", "instruction": "the cache is warm"}
    out = _run(repo, [repair])
    assert len(out["applied"]) == 1
    text = plan.read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n")[1].split("```")[0]
    rows = yaml.safe_load(block)
    assert [r["id"] for r in rows] == ["C1", "F1"]
    assert rows[1]["performer"] == "em" and rows[1]["writes"] == []
    assert "the cache is warm" in rows[1]["acceptance"]
    again = _run(repo, [repair])
    assert again["applied"] == [] and "already present" in again["skipped"][0]["reason"]


def test_unknown_kind_is_skipped_and_the_rest_still_lands(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [{"kind": "mystery", "target": "x", "instruction": "y"}, _citation()])
    assert [s["kind"] for s in out["skipped"]] == ["mystery"]
    assert "unknown repair kind" in out["skipped"][0]["reason"]
    assert len(out["applied"]) == 1
    assert "src/new/path.py" in plan.read_text(encoding="utf-8")


def test_crlf_is_kept(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1), newline="\r\n")
    out = _run(repo, [_citation(), {"kind": "register-rows-unclaimed", "target": "R", "instruction": "c"}])
    assert len(out["applied"]) == 2
    raw = plan.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")


@pytest.mark.parametrize(
    "params",
    [
        {"repairs": []},
        {"plan": _PLAN_REL},
        {"plan": _PLAN_REL, "repairs": [], "extra": 1},
        {"plan": "docs/other/x.md", "repairs": []},
        {"plan": "docs/plans/absent.md", "repairs": []},
        {"plan": "../outside.md", "repairs": []},
    ],
)
def test_bad_params_refuse(repo, params):
    _seed(repo, _plan(_CLAIMS_R1))
    out = mod._handler(params, repo_root=repo / ".git")
    assert out["exit_code"] == 1 and out["ok"] is False and out["error"]
