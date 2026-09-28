from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.session.core import resolve_session_id

_LEGACY_SCRIPT = (
    "// header\n"
    "  const identResults = await agent('do work', "
    "{ label: 'work:C1', phase: 'Commit wave 1\\'s work' });\n"
    "  if (!identResults || !/^COMMIT-LANDED/m.test(String(identResults))) {\n"
    "    throw new Error('halt');\n"
    "  }\n"
)


def _make_script_and_receipt(tmp_path: Path) -> Path:
    script_path = tmp_path / "run.workflow.mjs"
    script_path.write_text(_LEGACY_SCRIPT, encoding="utf-8")
    session_id = resolve_session_id() or ""
    (tmp_path / "run.workflow.mjs.emitted.json").write_text(
        json.dumps(
            {"sha256": "stale", "session_id": session_id, "emitted_at": "x", "plan": None}
        ),
        encoding="utf-8",
    )
    return script_path


def test_mark_landed_cli_splices_and_restamps(tmp_path: Path):
    script_path = _make_script_and_receipt(tmp_path)
    rc = cli_module.main(
        ["--mark-landed", "Commit wave 1's work", "--sha", "cafef00d", str(script_path)]
    )
    assert rc == cli_module.EXIT_OK
    assert 'const identResults = "COMMIT-LANDED cafef00d";' in script_path.read_text(
        encoding="utf-8"
    )


def test_mark_landed_requires_sha(tmp_path: Path):
    script_path = _make_script_and_receipt(tmp_path)
    rc = cli_module.main(["--mark-landed", "Commit wave 1's work", str(script_path)])
    assert rc == cli_module.EXIT_USAGE


def test_mark_landed_exclusive_of_plan(tmp_path: Path):
    script_path = _make_script_and_receipt(tmp_path)
    rc = cli_module.main(
        [
            "--mark-landed", "Commit wave 1's work",
            "--sha", "cafef00d",
            "--plan", "some-plan.md",
            str(script_path),
        ]
    )
    assert rc == cli_module.EXIT_USAGE


def test_mark_landed_unknown_phase_is_data_error(tmp_path: Path):
    script_path = _make_script_and_receipt(tmp_path)
    rc = cli_module.main(
        ["--mark-landed", "no such phase", "--sha", "cafef00d", str(script_path)]
    )
    assert rc == cli_module.EXIT_DATA_ERROR
