"""
coordinator_core.ops.tests.test_strang_port_checklist_gate_reference — drift
guard for the strang-01 port checklist's gate step.

Purpose: `docs/reference/strang-port-checklist.md` § step 4 names the CLI
(`coordinator/bin/gate-validate-invocable`) and the op key
(`gate.validate_invocable`) as the strang-01 port template's gate step
(docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C8). The doc is
prose, not code — nothing re-checks it if the CLI is renamed or the op key
changes out from under it. This test is that check: it asserts the checklist
still names a CLI path that exists on disk and an op key that matches the
live pinned literal, so a rename silently breaks the doc instead of silently
stranding it.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C8.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.gate_validate_invocable import OP_KEY

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHECKLIST = _REPO_ROOT / "docs" / "reference" / "strang-port-checklist.md"
_CLI_RELATIVE_PATH = "coordinator/bin/gate-validate-invocable"


def _checklist_text() -> str:
    assert _CHECKLIST.exists(), f"strang-01 port checklist missing: {_CHECKLIST}"
    return _CHECKLIST.read_text(encoding="utf-8")


def test_checklist_names_the_live_op_key() -> None:
    text = _checklist_text()
    assert OP_KEY in text, (
        f"strang-port-checklist.md does not cite the live op key {OP_KEY!r} "
        "(gate_validate_invocable.OP_KEY) — the doc has drifted from the "
        "pinned literal it must cite verbatim"
    )


def test_checklist_names_an_existing_cli_path() -> None:
    text = _checklist_text()
    assert _CLI_RELATIVE_PATH in text, (
        f"strang-port-checklist.md does not cite {_CLI_RELATIVE_PATH!r} — "
        "the gate step's CLI reference has drifted"
    )
    cli_path = _REPO_ROOT / _CLI_RELATIVE_PATH
    assert cli_path.exists(), (
        f"strang-port-checklist.md cites {_CLI_RELATIVE_PATH!r} but no file "
        f"exists at {cli_path} — the CLI this checklist step names has moved "
        "or been renamed out from under the doc"
    )
