"""Parity tests for coordinator_core.install.step_zero_emit.

Replays the NORMATIVE conformance fixture (example-doctrine-repo-side
coordinator/tests/fixtures/step-zero-conformance.json, base64-encoded
expected bytes) against this module's emit_line(), plus direct unit checks
of json_escape()'s five-escape ordering.

Spec backlink: docs/wiki/step-zero-emitter-contract.md
Port of: step_zero_emit.sh (example-doctrine-repo 290997c7, 2026-07-22)
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from coordinator_core.install.step_zero_emit import emit_line, json_escape
from coordinator_core.testing.doe_root import doe_root_and_present


def _find_fixture():
    # example-doctrine-repo sibling repo, resolved via the shared registry-first ladder
    # (coordinator_core.testing.doe_root.doe_root_and_present) rather than a
    # __file__-anchored checkout-depth guess.
    doe_root, present = doe_root_and_present()
    if not present:
        return None
    candidate = os.path.join(
        doe_root, "coordinator", "tests", "fixtures", "step-zero-conformance.json"
    )
    return candidate if os.path.isfile(candidate) else None


_FIXTURE_PATH = _find_fixture()


@pytest.mark.skipif(_FIXTURE_PATH is None, reason="example-doctrine-repo sibling conformance fixture not discoverable on disk")
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


# Hand-transcribed subset of the same fixture (kept inline so this test suite
# does not hard-depend on the example-doctrine-repo sibling repo being checked out alongside
# claude-klabauter -- CI/sandboxed runs of just this repo still get real coverage).
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
    # Backslash MUST be escaped first -- a value containing a literal
    # backslash-quote sequence must not double-escape.
    assert json_escape('\\"') == '\\\\\\"'


def test_json_escape_control_chars():
    assert json_escape("a\rb\nc\td") == "a\\rb\\nc\\td"


def test_json_escape_non_ascii_passes_through_raw():
    # Deliberate boundary: non-ASCII is NOT escaped (unlike json.dumps).
    assert json_escape("café") == "café"


def test_emit_writes_trailing_newline_only(tmp_path):
    from coordinator_core.install.step_zero_emit import emit
    import io

    buf = io.StringIO()
    emit("x", "pass", "advisory", "d", "", stream=buf)
    out = buf.getvalue()
    assert out.endswith("\n")
    assert out.count("\n") == 1
