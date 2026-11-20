"""The apply-rulings prompt renders _gate.pending_rulings: an array joins, a string passes through, anything else falls back."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.usefixtures("m_plus_known_arm"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

_FALLBACK = "(unlisted: read the ledger)"


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _emitted_expression() -> str:
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    m = re.search(r"Apply rulings: ' \+ (\(Array\.isArray\(_gate\.pending_rulings\).*?\)\)) \+ '\.", script, re.S)
    assert m, "pending_rulings render expression not emitted"
    return m.group(1)


def _render(expr: str, gate: dict) -> str:
    program = f"const _gate = {json.dumps(gate)};\nconsole.log(JSON.stringify({expr}));\n"
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


@pytest.mark.parametrize(
    ("gate", "want"),
    [
        ({"pending_rulings": ["J1", "J2"]}, "J1, J2"),
        ({"pending_rulings": "J1"}, "J1"),
        ({"pending_rulings": {"J1": True}}, _FALLBACK),
        ({"pending_rulings": []}, _FALLBACK),
        ({"pending_rulings": None}, _FALLBACK),
        ({}, _FALLBACK),
    ],
)
def test_pending_rulings_render(gate, want):
    assert _render(_emitted_expression(), gate) == want
