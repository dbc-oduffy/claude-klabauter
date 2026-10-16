"""
coordinator_core.ops.tests.test_plan_repin_register — the "plan.repin_register" op.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_plan_repin_register.py -q
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.plan_repin_register as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.plan_readiness_verdict import readiness_verdict
from coordinator_core.roadmap import prep_gate

_SIZING_REL = "state/sizings/20260101-a.yaml"
_PLAN_REL = "docs/plans/2026-01-01-a.md"
_OK = {"verdict": "OK"}


def _sizing(text_r1: str = "first wording") -> str:
    return (
        "schema: sizing-object\n"
        "requirement_register:\n"
        "  sources:\n"
        "  - doc: docs/x.md\n"
        "    ref: abc\n"
        "  rows:\n"
        "  - id: R1\n"
        f"    source_text: {text_r1}\n"
        "    surface: api\n"
        "    status: open\n"
        "  - id: R2\n"
        "    source_text: second wording\n"
        "    surface: api\n"
        "    status: open\n"
    )


def _plan(pins_line: str, rows: str = '["R1", "R2"]') -> str:
    return (
        "---\n"
        "title: t\n"
        "register_claims:\n"
        f"  sizing: {_SIZING_REL}\n"
        f"  rows: {rows}\n"
        f"{pins_line}"
        "other: keep\n"
        "---\n\n# Body\n"
    )


def _pins_for(*texts: str) -> str:
    pins = {f"R{i + 1}": rr.row_pin(t) for i, t in enumerate(texts)}
    return "  pins: " + yaml.safe_dump(pins, default_flow_style=True).strip() + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    return tmp_path


def _seed(repo: Path, *, current: str = "changed wording", newline: str = "\n") -> Path:
    """A plan pinned to R1's OLD wording while the sizing now carries `current`."""
    (repo / _SIZING_REL).write_text(_sizing(current), encoding="utf-8", newline="\n")
    plan = repo / _PLAN_REL
    text = _plan(_pins_for("first wording", "second wording"))
    plan.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return plan


def _run(repo: Path, **params) -> dict:
    params.setdefault("plan", _PLAN_REL)
    params.setdefault("premise_check", _OK)
    return mod._handler(params, repo_root=repo / ".git")


def _fm(plan: Path) -> dict:
    return yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])


def _gate(repo: Path) -> dict:
    return prep_gate._register(_fm(repo / _PLAN_REL), repo)


def test_registration_quad() -> None:
    assert OP_CLASSIFICATION["plan.repin_register"].name == "MUTATING"
    assert OP_MODULE_MAP["plan.repin_register"] == mod.__name__
    assert _OP_KEY_SCOPE["plan.repin_register"] == "common_dir"


@pytest.mark.parametrize(
    "premise", [{"verdict": "OK"}, {"rows": [{"verdict": "RESOLVES"}, {"verdict": "UNCHECKABLE"}]}]
)
def test_premise_check_passes_but_pin_differs_then_repin_clears_it(repo, premise):
    plan = _seed(repo)
    gate = _gate(repo)
    assert gate["status"] == "DEFECT" and gate["kind"] == rr.REGISTER_PIN_DRIFT_KIND

    pulled = readiness_verdict(
        {"verdict": "NOT-PREPPED", "classes": {"REGISTER": gate}},
        None, [], repo, [{"verdict": "RESOLVES", "citation": "c"}],
    )
    assert pulled["verdict"] == "pulled"
    assert "REGISTER:register-pin-drift" in pulled["reason"]

    out = _run(repo, premise_check=premise)
    assert out["exit_code"] == 0 and out["applied"] is True
    assert [d["id"] for d in out["repinned"]] == ["R1"]
    assert out["repinned"][0]["pinned"] == rr.row_pin("first wording")
    assert out["repinned"][0]["current"] == rr.row_pin("changed wording")
    assert out["plan"] == _PLAN_REL

    assert _gate(repo)["status"] == "PASS"
    fm_text = plan.read_text(encoding="utf-8").split("---\n")[1]
    assert rr.plan_pin_drift(fm_text, repo) == []
    register = rr.read_register((repo / _SIZING_REL).read_text(encoding="utf-8"))
    assert rr.pin_drift(register, rr.plan_pins(fm_text)) == []
    assert "other: keep" in fm_text


def test_second_call_is_an_idempotent_noop(repo):
    plan = _seed(repo)
    assert _run(repo)["applied"] is True
    first = plan.read_bytes()
    out = _run(repo)
    assert out["exit_code"] == 0 and out["applied"] is False and out["repinned"] == []
    assert plan.read_bytes() == first


@pytest.mark.parametrize(
    "premise",
    [
        {"rows": [{"verdict": "RESOLVES"}, {"verdict": "UNRESOLVED"}]},
        {"rows": [{"verdict": "CONTRADICTED"}]},
        {"rows": []},
        {"verdict": "BLOCKED"},
        {"verdict": "WARN", "ledger": "nope/absent.md"},
        {},
    ],
)
def test_non_clear_premise_check_refuses(repo, premise):
    plan = _seed(repo)
    before = plan.read_bytes()
    out = _run(repo, premise_check=premise)
    assert out["exit_code"] == 1 and out["applied"] is False
    assert plan.read_bytes() == before


def test_refusals_leave_bytes_identical(repo):
    plan = _seed(repo)
    before = plan.read_bytes()
    (repo / "docs" / "other.md").write_text(_plan(""), encoding="utf-8")
    cases = [
        {"premise_check": None},
        {"plan": "docs/other.md"},
        {"plan": "../outside.md"},
        {"plan": "docs/plans/absent.md"},
        {"bogus": 1},
    ]
    for extra in cases:
        params = {"plan": _PLAN_REL, "premise_check": _OK, **extra}
        if extra.get("premise_check", 0) is None:
            del params["premise_check"]
        out = mod._handler(params, repo_root=repo / ".git")
        assert out["exit_code"] == 1, extra
    assert plan.read_bytes() == before


def test_claimed_row_dropped_from_register_refuses(repo):
    plan = _seed(repo)
    sizing = repo / _SIZING_REL
    sizing.write_text(_sizing().split("  - id: R2")[0], encoding="utf-8", newline="\n")
    before = plan.read_bytes()
    out = _run(repo)
    assert out["exit_code"] == 1 and "R2" in out["error"]
    assert plan.read_bytes() == before


def test_plan_without_register_claims_refuses(repo):
    plan = repo / _PLAN_REL
    plan.write_text("---\ntitle: t\n---\n\nbody\n", encoding="utf-8", newline="\n")
    before = plan.read_bytes()
    out = _run(repo)
    assert out["exit_code"] == 1 and "register_claims" in out["error"]
    assert plan.read_bytes() == before


def test_crlf_plan_stays_crlf(repo):
    plan = _seed(repo, newline="\r\n")
    assert _run(repo)["applied"] is True
    data = plan.read_bytes()
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert _gate(repo)["status"] == "PASS"


def test_handler_process_time_under_budget(repo):
    _seed(repo)
    start = time.process_time()
    out = _run(repo)
    elapsed = time.process_time() - start
    assert out["applied"] is True
    assert elapsed < 0.2, f"handler process time {elapsed * 1000:.0f}ms"
