"""Parity tests for coordinator_core.install.step_zero_emit.

Replays the NORMATIVE conformance fixture (DoE-side
coordinator/tests/fixtures/step-zero-conformance.json, base64-encoded
expected bytes) against this module's emit_line(), plus direct unit checks
of json_escape()'s five-escape ordering.

Spec backlink: coordinator-content-repo coordinator/docs/wiki/hook-best-practices/step-zero-emitter-contract.md
Port of: step_zero_emit.sh (DoE 290997c7, 2026-07-22)
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from coordinator_core.install.step_zero_emit import emit_line, json_escape
from coordinator_core.testing.content_root import content_root_and_present


def _find_fixture():
    content_root, present = content_root_and_present()
    if not present:
        return None
    candidate = os.path.join(
        content_root, "coordinator", "tests", "fixtures", "step-zero-conformance.json"
    )
    return candidate if os.path.isfile(candidate) else None


_FIXTURE_PATH = _find_fixture()


@pytest.mark.skipif(_FIXTURE_PATH is None, reason="DoE sibling conformance fixture not discoverable on disk")
def test_conformance_fixture_byte_parity():
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["cases"], "conformance fixture has no cases"
    for case in data["cases"]:
        inp = case["input"]
        expected = base64.b64decode(case["expected_output_bytes"])
        actual = emit_line(
            inp["name"], inp["status"], inp["severity"], inp["detail"], inp["remediation"]
        ).encode("utf-8")
        assert actual == expected, f"mismatch for case {inp!r}"


_INLINE_CASES = [
    (
        ("python", "pass", "hard", "Python 3.11.5", ""),
        b'{"name":"python","status":"pass","severity":"hard","detail":"Python 3.11.5","remediation":""}\n',
    ),
    (
        ("uv", "warn", "advisory", "uv not found on PATH", "brew install uv"),
        b'{"name":"uv","status":"warn","severity":"advisory","detail":"uv not found on PATH","remediation":"brew install uv"}\n',
    ),
    (
        ("escbug", "warn", "advisory", 'path C:\\temp said "hi"', ""),
        b'{"name":"escbug","status":"warn","severity":"advisory","detail":"path C:\\\\temp said \\"hi\\"","remediation":""}\n',
    ),
    (
        ("clone_auth", "warn", "advisory", "fatal: auth failed\r\nremote: denied\ttoken", "configure gh (gh auth login)"),
        b'{"name":"clone_auth","status":"warn","severity":"advisory","detail":"fatal: auth failed\\r\\nremote: denied\\ttoken","remediation":"configure gh (gh auth login)"}\n',
    ),
    (
        ("crlf", "inconclusive", "advisory", "a\r\nb", ""),
        b'{"name":"crlf","status":"inconclusive","severity":"advisory","detail":"a\\r\\nb","remediation":""}\n',
    ),
]


@pytest.mark.parametrize("args,expected", _INLINE_CASES)
def test_emit_line_inline_cases(args, expected):
    assert emit_line(*args).encode("utf-8") == expected


def test_json_escape_order_backslash_before_quote():
    assert json_escape('\\"') == '\\\\\\"'


def test_json_escape_control_chars():
    assert json_escape("a\rb\nc\td") == "a\\rb\\nc\\td"


def test_json_escape_non_ascii_passes_through_raw():
    assert json_escape("café") == "café"


def test_emit_writes_trailing_newline_only(tmp_path):
    from coordinator_core.install.step_zero_emit import emit
    import io

    buf = io.StringIO()
    emit("x", "pass", "advisory", "d", "", stream=buf)
    out = buf.getvalue()
    assert out.endswith("\n")
    assert out.count("\n") == 1


# --- emit_step_zero exit rule follows the severity labels -------------------

import importlib.util
import sys
from pathlib import Path

_BIN_PROBE = Path(__file__).parent.parent.parent / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("claude_klabauter_doctor_probe_step_zero_exit", _BIN_PROBE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _exit_for(mod, capsys, *rows):
    code = mod.emit_step_zero(list(rows))
    capsys.readouterr()
    return code


def test_step_zero_exit_zero_when_only_advisory_rows_fail(capsys):
    mod = _load_probe()
    adv = mod._ProbeResult("a", mod._BROKEN, "d", "r", required=False)
    ok = mod._ProbeResult("b", mod._PASS, "d", "r", required=True)
    assert _exit_for(mod, capsys, adv, ok) == 0


def test_step_zero_exit_zero_on_hard_inconclusive_row(capsys):
    mod = _load_probe()
    skipped = mod._ProbeResult("c", mod._PASS, "d", "r", required=True, skipped=True)
    assert _exit_for(mod, capsys, skipped) == 0


def test_step_zero_exit_one_on_hard_fail(capsys):
    mod = _load_probe()
    for status in (mod._BROKEN, mod._DEGRADED):
        hard = mod._ProbeResult("d", status, "d", "r", required=True)
        assert _exit_for(mod, capsys, hard) == 1


def test_worktree_bloat_is_not_a_required_probe():
    import tomllib

    manifest = tomllib.loads((_BIN_PROBE.parent / "doctor-probes.toml").read_text(encoding="utf-8"))
    row = next(p for p in manifest["probe"] if p["id"] == "claude-klabauter.worktree.bloat")
    assert row["required"] is False
