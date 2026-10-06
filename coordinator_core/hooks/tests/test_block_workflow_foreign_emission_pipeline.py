"""The foreign-emission hook admits a real dispatch.emit pipeline-route emission.

Nothing is stubbed below the op: the structured fixture manifest is loaded, composed, written,
and receipted, then judged by the hook handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import block_workflow_foreign_emission as bwfe
from coordinator_core.ops.dispatch_emit import op as op_module

_CONTENT_ROOT = (
    Path(__file__).resolve().parents[2]
    / "ops" / "dispatch_emit" / "tests" / "fixtures" / "pipeline_structured"
)
_SESSION = "sess-emitter"


@pytest.fixture
def emitted(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: _CONTENT_ROOT)
    out = tmp_path / "pipeline.workflow.mjs"
    (tmp_path / "brief.md").write_text("research the fixture subjects", encoding="utf-8")
    reply = op_module._dispatch_emit(
        {
            "pipeline": "structured",
            "brief": "brief.md",
            "subjects": [
                {"subject": k, "verifiers": [{"role": "v-a", "topic": "a", "name": "A"}]}
                for k in ("alpha-subject", "beta-subject")],
            "target_root": str(tmp_path),
            "output_path": str(out),
            "session_id": _SESSION,
        }
    )
    assert reply["receipt"] == str(out) + ".emitted.json"
    assert out.is_file()
    return out


def _call(script: Path, session_id: str) -> dict:
    return bwfe._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "session_id": session_id,
            "cwd": str(script.parent),
        }
    )


def test_emitting_session_is_admitted(emitted):
    assert _call(emitted, _SESSION) == {}


def test_other_session_is_denied_for_session_mismatch(emitted):
    hso = _call(emitted, "sess-other")["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_appended_byte_is_denied_for_sha_mismatch(emitted):
    with emitted.open("ab") as fh:
        fh.write(b"\n")
    hso = _call(emitted, _SESSION)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "sha" in hso["permissionDecisionReason"].lower()
