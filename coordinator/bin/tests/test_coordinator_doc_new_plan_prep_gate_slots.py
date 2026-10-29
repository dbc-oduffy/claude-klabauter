"""The plan scaffold seeds the slots plan.prep_gate requires: live `anchor_premises`,
`register_claims` only with a sizing register, and per-row `register_rows` and `body`.

Loaded by file path: `coordinator-doc-new` is an extensionless polyglot entrypoint.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_prep_gate_slots.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import yaml

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_SIZING_REL = "state/sizings/2026-10-01-fixture.yaml"
_KW = dict(
    title="Slots plan", branch="main", author="tester",
    plan_id="pln-slots-abc123", deliverable_id="dlv-fixture-abc123", sizing_object=_SIZING_REL,
)
_BASE = (
    "schema: sizing-object\nname: n\nintent: i\nestimate:\n  tshirt: M\n  provisional: true\n"
    "route: plan\ndetents: []\nfork: null\nxl_exit: null\nstatus: sized\n"
    "premise:\n  provenance: not-applicable\n  evidence: fixture\n"
    'deliverable_id: "dlv-fixture-abc123"\n'
)
_REGISTER = (
    "requirement_register:\n  rows:\n"
    "    - {id: R1, source_text: x, status: open}\n"
    "    - {id: R2, source_text: y, status: open}\n"
)


def _cli():
    loader = importlib.machinery.SourceFileLoader("cdn_prep_gate_slots_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_prep_gate_slots_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _scaffold(tmp_path, monkeypatch, sizing_text: str) -> str:
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(sizing_text, encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    return _cli()._scaffold_plan(**_KW, sizing_repo_root=str(tmp_path))


def _fm(text: str) -> dict:
    return yaml.safe_load(text.split("---")[1])


def _rows(text: str) -> list[dict]:
    return yaml.safe_load(re.search(r"```yaml plan-tasks\n(.*?)```", text, re.DOTALL).group(1))


def test_with_register_seeds_every_slot(tmp_path, monkeypatch):
    out = _scaffold(tmp_path, monkeypatch, _BASE + _REGISTER)
    fm = _fm(out)
    assert fm["anchor_premises"] == []
    assert fm["register_claims"]["rows"] == ["R1", "R2"]
    rows = _rows(out)
    assert rows
    for row in rows:
        assert row["register_rows"] == ["R1", "R2"]
        assert row["body"].strip()


def test_without_register_declares_empty_rows_and_no_claims(tmp_path, monkeypatch):
    out = _scaffold(tmp_path, monkeypatch, _BASE)
    fm = _fm(out)
    assert fm["anchor_premises"] == []
    assert "register_claims" not in fm
    for row in _rows(out):
        assert "register_rows" not in row  # schema minItems: 1 forbids a live []
        assert row["body"].strip()


def test_scaffold_without_sizing_still_carries_anchor_premises():
    out = _cli()._scaffold_plan(title="t", branch="main", author="a")
    assert _fm(out)["anchor_premises"] == []
    assert all("register_rows" not in r for r in _rows(out))
