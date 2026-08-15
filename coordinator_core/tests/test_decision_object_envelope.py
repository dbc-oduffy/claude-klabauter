"""
coordinator_core.tests.test_decision_object_envelope — conformance suite for
the decision-object envelope + judgment-point constructors.

Domain-independent (no pickup/gate-specific fixtures): asserts the 8-key
envelope shape, the `_emit` fail-loud chokepoint, and the offer-never-verdict
asymmetry between `build_judgment_point` and `build_untrusted_gate_judgment_point`.

Also asserts `coordinator_core.contract.decision_object` stays stdlib-light —
importing it must not pull in pydantic, asyncio, or the `cockpit_schema` tree.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md (Wave 1,
chunk W1-A2). [DEAD-CITATION: plan file never committed to this repo] Conformance target: DoE-claude's
`schemas/decision-object.schema.json` (DR-047) is the schema-of-record; this
suite asserts this package's key set matches it by name (8 canonical keys).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys

import pytest

from coordinator_core.contract.decision_object import (
    ENVELOPE_KEYS,
    DecisionObjectError,
    ExitCodeBase,
    build_envelope,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
    emit,
)
from coordinator_core.contract.decision_object.envelope import extend_exit_codes

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_envelope_keys_is_exactly_the_8_canonical_keys():
    assert set(ENVELOPE_KEYS) == {
        "artifact",
        "preflight",
        "gates",
        "directives",
        "judgment_points",
        "decisions",
        "narration",
        "next_move",
    }
    assert len(ENVELOPE_KEYS) == 8


def test_build_envelope_produces_exactly_the_8_keys_with_defaults():
    envelope = build_envelope(artifact={"path": "x"})
    assert set(envelope.keys()) == set(ENVELOPE_KEYS)
    assert envelope["artifact"] == {"path": "x"}
    assert envelope["directives"] == []
    assert envelope["judgment_points"] == []


def test_emit_accepts_a_well_formed_envelope():
    envelope = build_envelope()
    result = emit(envelope)
    assert result is envelope


def test_emit_rejects_missing_key():
    malformed = build_envelope()
    del malformed["next_move"]
    with pytest.raises(DecisionObjectError, match="missing"):
        emit(malformed)


def test_emit_rejects_extra_key():
    malformed = build_envelope()
    malformed["unexpected_extra_key"] = "surprise"
    with pytest.raises(DecisionObjectError, match="extra"):
        emit(malformed)


def test_emit_rejects_non_mapping():
    with pytest.raises(DecisionObjectError):
        emit(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_exit_code_base_success_is_zero():
    assert ExitCodeBase.SUCCESS == 0


def test_extend_exit_codes_anchors_success_at_zero_and_adds_custom_members():
    PickupExitCode = extend_exit_codes("PickupExitCode", BLOCKED=1, STALE_CLAIM=2)
    assert PickupExitCode.SUCCESS == 0
    assert PickupExitCode.BLOCKED == 1
    assert PickupExitCode.STALE_CLAIM == 2


def test_build_judgment_point_requires_recommendation_positional():
    sig = inspect.signature(build_judgment_point)
    first_param = next(iter(sig.parameters.values()))
    assert first_param.name == "recommendation"
    assert first_param.default is inspect.Parameter.empty
    assert first_param.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def test_build_judgment_point_carries_the_given_recommendation():
    # Review: code-reviewer -- cross-slice correction (AC-13). The canonical
    # `recommendation` shape is an object `{disposition, rationale}` | None,
    # not a bare string -- see `pickup_assemble.__init__`'s
    # `Optional[dict[str, str]]` signature and the DoE schema-of-record.
    point = build_judgment_point(
        {"disposition": "proceed", "rationale": "no competing claim found"},
        id="jcc",
        question="Stand down?",
        dispositions=[{"value": "proceed", "resolves": ["d2"]}],
        evidence="gates.competing_claim",
        reason="insufficient-evidence",
    )
    assert point["recommendation"] == {
        "disposition": "proceed",
        "rationale": "no competing claim found",
    }
    assert point["id"] == "jcc"
    assert set(point.keys()) == {
        "id",
        "question",
        "dispositions",
        "evidence",
        "reason",
        "recommendation",
        "revalidate_at_dispatch",
        "round_trip",
    }


def test_build_judgment_point_rejects_a_bare_string_recommendation():
    with pytest.raises(ValueError):
        build_judgment_point(
            "proceed",
            id="jcc",
            question="Stand down?",
            dispositions=[{"value": "proceed", "resolves": ["d2"]}],
            evidence="gates.competing_claim",
            reason="insufficient-evidence",
        )


def test_build_judgment_point_rejects_recommendation_with_extra_or_missing_keys():
    with pytest.raises(ValueError):
        build_judgment_point(
            {"disposition": "proceed"},
            id="jcc",
            question="Stand down?",
            dispositions=[{"value": "proceed", "resolves": ["d2"]}],
            evidence="gates.competing_claim",
            reason="insufficient-evidence",
        )
    with pytest.raises(ValueError):
        build_judgment_point(
            {"disposition": "proceed", "rationale": "x", "confidence": 9},
            id="jcc",
            question="Stand down?",
            dispositions=[{"value": "proceed", "resolves": ["d2"]}],
            evidence="gates.competing_claim",
            reason="insufficient-evidence",
        )


def test_build_judgment_point_accepts_none_recommendation():
    point = build_judgment_point(
        None,
        id="jcc",
        question="Stand down?",
        dispositions=[{"value": "proceed", "resolves": ["d2"]}],
        evidence="gates.competing_claim",
        reason="insufficient-evidence",
    )
    assert point["recommendation"] is None


def test_build_untrusted_gate_judgment_point_has_no_recommendation_parameter():
    sig = inspect.signature(build_untrusted_gate_judgment_point)
    assert "recommendation" not in sig.parameters


def test_build_untrusted_gate_judgment_point_rejects_recommendation_kwarg():
    with pytest.raises(TypeError):
        build_untrusted_gate_judgment_point(
            id="jcc",
            question="Stand down?",
            dispositions=[{"value": "proceed", "resolves": []}],
            evidence="gates.competing_claim",
            reason="insufficient-evidence",
            recommendation="proceed",  # type: ignore[call-arg]
        )


def test_build_untrusted_gate_judgment_point_always_emits_recommendation_none():
    point = build_untrusted_gate_judgment_point(
        id="jcc",
        question="Stand down?",
        dispositions=[{"value": "proceed", "resolves": ["d2"]}],
        evidence="gates.competing_claim",
        reason="insufficient-evidence",
    )
    assert point["recommendation"] is None


def test_decision_object_subpackage_import_does_not_pull_in_forbidden_modules():
    """Import the package fresh in a subprocess and assert no forbidden
    module (`pydantic`, `coordinator_core.contract.cockpit_schema`) ever
    lands in that subprocess's `sys.modules`.

    Review: code-reviewer -- Finding 2. The prior in-process `sys.modules`
    loop asserted `not A or not B` where A ("name starts with a forbidden
    prefix") and B ("name starts with `coordinator_core.contract.
    decision_object`") describe two disjoint prefixes -- no module's dotted
    name can start with both, so the assertion could never fail regardless
    of what actually got imported. A subprocess is used (not just a fresh
    loop in this process) because modules already imported by earlier tests
    in this same process would pollute `sys.modules` regardless of what
    this package itself imports.
    """
    script = (
        "import sys\n"
        "import coordinator_core.contract.decision_object\n"
        "forbidden = ('pydantic', 'coordinator_core.contract.cockpit_schema')\n"
        "hits = [n for n in sys.modules if any(n.startswith(p) for p in forbidden)]\n"
        "assert not hits, f'unexpected coupling: {hits}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_decision_object_subpackage_source_files_have_no_forbidden_import_lines():
    import coordinator_core.contract.decision_object as decision_object_pkg

    module_file = inspect.getsourcefile(decision_object_pkg)
    assert module_file is not None
    source_files = [
        "coordinator_core/contract/decision_object/__init__.py",
        "coordinator_core/contract/decision_object/envelope.py",
        "coordinator_core/contract/decision_object/judgment.py",
    ]
    for path in source_files:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines:
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for forbidden in ("pydantic", "asyncio", "cockpit_schema"):
                assert forbidden not in stripped, (
                    f"{path} has forbidden import: {stripped!r}"
                )
