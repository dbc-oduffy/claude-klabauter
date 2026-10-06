"""`_sizing_baton_sections` appends the problem-alignment paragraph for a `shape` sizing.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_shape_baton_section.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cdn_shape_section_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_shape_section_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()
_EVIDENCE = "dogfood report observed the emitted receipt"
_PARA = _cli._SHAPE_ALIGNMENT_PARAGRAPH


def _meta(route: str, evidence: str) -> dict:
    return {
        "route": route,
        "premise": {"evidence": evidence},
        "exit_criterion": {"statement": "the baton carries the evidence"},
    }


def test_shape_with_evidence_has_both_parts_evidence_first():
    spec, acceptance, _ = _cli._sizing_baton_sections(_meta("shape", _EVIDENCE))
    assert spec == f"{_EVIDENCE}\n\n{_PARA}"
    assert acceptance == ["the baton carries the evidence"]


def test_shape_with_placeholder_evidence_is_paragraph_alone():
    spec, _, _ = _cli._sizing_baton_sections(_meta("shape", "TBD"))
    assert spec == _PARA
    assert _PARA.startswith("Sized `shape`: the problem is not yet converged")


def test_plan_sizing_is_unchanged():
    spec, acceptance, _ = _cli._sizing_baton_sections(_meta("plan", _EVIDENCE))
    assert spec == _EVIDENCE
    assert acceptance == ["the baton carries the evidence"]
    assert _cli._sizing_baton_sections(_meta("plan", "TBD"))[0] is None
