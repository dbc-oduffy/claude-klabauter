from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.mark_landed import (
    NoEmbeddedCommitPhaseError,
    PhaseNotFoundError,
    mark_landed,
    mark_landed_and_restamp,
)

_LEGACY_SCRIPT = """// header
  const wave1Results = await agent('do the work', { phase: 'Wave 1: do it' });
  const identResults = await agent(`Some ${dynamic} template ${literal}
with a newline`, { label: 'work:C1', phase: 'Commit wave 1\\'s work. Pathspec: [x]. ids: C1' });
  if (!identResults || !/^COMMIT-LANDED/m.test(String(identResults))) {
    throw new Error('halt: wave 1 did not land');
  }
  const wave2Results = await agent('next thing', { phase: 'Wave 2: next' });
"""

_DAG_SCRIPT_WITH_MARKER = (
    "// coordinator:terminal-commit-request v1 "
    '{"version":1,"chunks":[{"id":"C1","title":"t","paths":["a.py"],'
    '"prefixes":[],"report":"r.md"}],"deliverable_id":null,'
    '"session_id":null,"repo_root":null,"plan_path":null}\n'
)


def test_splices_only_the_named_phase_up_to_its_halt_check():
    out = mark_landed(_LEGACY_SCRIPT, "Commit wave 1's work", "deadbeef1")
    assert 'const identResults = "COMMIT-LANDED deadbeef1";' in out
    # the untouched sibling phase survives verbatim
    assert "const wave2Results = await agent('next thing'" in out
    assert "const wave1Results = await agent('do the work'" in out
    # the halt-check line is preserved after the splice
    assert "if (!identResults ||" in out
    # no leftover template-literal fragment from the spliced-out call
    assert "with a newline" not in out


def test_unknown_phase_title_raises():
    with pytest.raises(PhaseNotFoundError):
        mark_landed(_LEGACY_SCRIPT, "Commit wave 99's work", "deadbeef1")


def test_dag_shape_with_terminal_marker_refuses_not_a_silent_noop():
    with pytest.raises(NoEmbeddedCommitPhaseError):
        mark_landed(_DAG_SCRIPT_WITH_MARKER, "anything", "deadbeef1")


def test_mark_landed_and_restamp_writes_file_and_updates_receipt(tmp_path: Path):
    script_path = tmp_path / "run.workflow.mjs"
    script_path.write_text(_LEGACY_SCRIPT, encoding="utf-8")
    receipt_path = tmp_path / "run.workflow.mjs.emitted.json"
    receipt_path.write_text(
        '{"sha256": "stale", "session_id": "sess-123", "emitted_at": "x", "plan": null}',
        encoding="utf-8",
    )

    receipt = mark_landed_and_restamp(script_path, "Commit wave 1's work", "deadbeef1", "sess-123")

    new_text = script_path.read_text(encoding="utf-8")
    assert 'const identResults = "COMMIT-LANDED deadbeef1";' in new_text
    assert receipt["session_id"] == "sess-123"
    assert receipt["sha256"] != "stale"
