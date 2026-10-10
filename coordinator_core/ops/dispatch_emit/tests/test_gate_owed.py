"""The Python matcher and the emitted JS regex literal agree on one fixture table."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import gate_owed

# (reply text, expected kind or None)
CASES = [
    ("PARTIAL: r.md\ngate-blocker: guard-denied: pnpm typecheck (heavy-admission)", "guard-denied"),
    ("PARTIAL: r.md\ngate-blocker: outside-footprint: peer WIP", "outside-footprint"),
    ("PARTIAL: r.md\n**gate-blocker**: `guard-denied`: x", "guard-denied"),
    ("PARTIAL: r.md\n**gate-blocker:** guard-denied: x", "guard-denied"),
    ("PARTIAL: r.md\n`gate-blocker`: outside-footprint: x", "outside-footprint"),
    ("PARTIAL: r.md\n> gate-blocker: guard-denied: x", "guard-denied"),
    ("PARTIAL: r.md\nGate-Blocker: Guard-Denied: x", "guard-denied"),
    ("gate-blocker: guard-denied: first line", "guard-denied"),
    ("PARTIAL: r.md\nthe gate-blocker: guard-denied: appeared mid-line", None),
    ("PARTIAL: r.md\ngate-blocker: made-up-kind: x", None),
    ("PARTIAL: r.md\ngate-blocker: guard-deniedish: x", None),
    ("PARTIAL: r.md", None),
]


def _kind_py(text):
    m = gate_owed.match(text)
    return m[0] if m else None


def _kind_js(text):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const re = {gate_owed.GATE_OWED_JS_RE};"
        "const s = JSON.stringify(" + json.dumps(text) + ");"
        "const m = re.exec(s); process.stdout.write(JSON.stringify(m ? [m[1].toLowerCase(), m[2]] : null));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("text,kind", CASES)
def test_python_matcher(text, kind):
    assert _kind_py(text) == kind


@pytest.mark.spawns_process
@pytest.mark.parametrize("text,kind", CASES)
def test_js_literal_agrees_with_python(text, kind):
    got = _kind_js(text)
    assert (got[0] if got else None) == kind


@pytest.mark.spawns_process
def test_js_captures_rest_without_escape_tail():
    got = _kind_js('PARTIAL: r.md\ngate-blocker: guard-denied: pnpm "x" (g)\nmore')
    assert got == ["guard-denied", ': pnpm \\"x\\" (g)']


def test_report_line_round_trips():
    line = gate_owed.report_line(gate_owed.KIND_GUARD_DENIED, "cmd (guard)")
    assert gate_owed.match(line) == ("guard-denied", ": cmd (guard)")


def test_report_line_rejects_unknown_kind():
    with pytest.raises(ValueError):
        gate_owed.report_line("nope", "x")


def test_kind_filter_skips_other_kinds():
    text = "gate-blocker: guard-denied: a\ngate-blocker: outside-footprint: b"
    assert gate_owed.match(text, gate_owed.KIND_OUTSIDE_FOOTPRINT) == ("outside-footprint", ": b")
    assert gate_owed.match("gate-blocker: guard-denied: a", gate_owed.KIND_OUTSIDE_FOOTPRINT) is None


def test_names():
    assert gate_owed.GATE_OWED_VAR == "_gateOwed"
