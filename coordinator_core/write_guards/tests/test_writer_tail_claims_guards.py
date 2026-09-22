"""
coordinator_core.write_guards.tests.test_writer_tail_claims_guards — AC5 for
C6 (migration batch B: coordinator_core/hooks and coordinator_core/bash_
guards to-fix writers onto the seam, with their entry paths) — the
write_guards leg.

One behavioural test per (entry-path class x seam entry point) pair this
batch migrated, driven through its REAL entry, asserting the claim lands.
Watched RED with the seam call swapped back for the raw primitive it
replaced (``validate_frontmatter_schema_deny._capture_guard_forensics``'s
``out_path.write_text(...)``): that shape never calls ``declare_write``, so
the touch record stays empty even though the forensics file itself is
written.

Entry-path class covered here: "bare process main (hook subprocess)" — the
package default C3's census recorded for every ``coordinator_core/write_
guards/`` module. This batch closes that gap by wrapping ``write_guards/
engine.py::evaluate_payload_json`` ONCE in ``cli_entry.
recording_declared_writes()`` — the one entry both ``__main__.main()`` and
the out-of-repo PreToolUse dispatchers converge on — rather than opening a
collection per guard site (D4). `_capture_guard_forensics` itself is called
directly here (not through the full deny-path `check()`, which needs a real
DoE-claude sibling checkout to reach — see the module's own test suite's
skip condition) — matching C5's own precedent of driving the migrated
function's real call, not a synthetic stand-in.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C6.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import cli_entry
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.write_guards import validate_frontmatter_schema_deny as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION_ENV_VAR = "COORDINATOR_SESSION_ID"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_creationflags()
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_creationflags())
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, **no_console_creationflags())
    return repo


def _touch_record_text(session_dir) -> str:
    from coordinator_core.session import scope

    sink = Path(session_dir) / scope._TOUCH_RECORD_FILENAME
    lines, _degraded = scope._read_touch_record_as_legacy_lines(sink)
    return "\n".join(lines)


def test_capture_guard_forensics_claims_its_dump_through_the_seam(tmp_path, monkeypatch):
    """entry-path class: bare process main (hook subprocess, wrapped once at
    write_guards/engine.py::evaluate_payload_json) x seam entry point:
    replace_text. Drives `_capture_guard_forensics` (the real migrated
    write) inside the same `cli_entry.recording_declared_writes()` scope
    `evaluate_payload_json` now opens, and asserts the claim lands in the
    touch record -- not merely that the forensics file exists."""
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    repo = _make_repo(tmp_path)
    sid = "sid-c6-forensics"
    core.init(sid, cwd=str(repo))
    monkeypatch.setenv(_SESSION_ENV_VAR, sid)

    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "state" / "handoffs" / "x.md")}, "cwd": str(repo)}
    forensics = {"doe_root": None, "matched_schema_name": "handoff", "schemas_dir": str(guard._VENDORED_SCHEMAS_DIR)}

    with cli_entry.recording_declared_writes(cwd=str(repo)):
        guard._capture_guard_forensics(payload, forensics, capture_reason="deny", deny_reason="test")

    forensics_dir = repo / "state" / "scratch" / "write-guard-forensics"
    dumps = list(forensics_dir.glob("validate_frontmatter_schema_deny-*.json"))
    assert len(dumps) == 1, f"expected exactly one capture file, found {dumps}"

    touch_content = _touch_record_text(core.session_dir(sid, cwd=str(repo)))
    assert dumps[0].name in touch_content, (
        "validate_frontmatter_schema_deny._capture_guard_forensics's write "
        "must be declared through the seam "
        "(session/claimed_write.py::replace_text), not a raw write_text() "
        "that leaves the touch record empty"
    )
