"""A resume ledger of kind unclaimed-rows carries no `blocking`; the emitted script must not read it unguarded."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script(sizing_rel=None) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt=None if sizing_rel else "add a thing", sizing_rel=sizing_rel, run_id="run-1",
        session_id=None, wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )


_READ = re.compile(r"(?:_gate\.resume_ledger|_rl0?|_resumeLedger)\.blocking\b")


@pytest.mark.parametrize("sizing_rel", [None, "docs/plans/x.sizing.md"])
def test_every_resume_ledger_blocking_read_is_guarded(sizing_rel):
    script = _script(sizing_rel=sizing_rel)
    reads = list(_READ.finditer(script))
    assert reads
    bare = [script[m.start() : m.end() + 30] for m in reads if not script[m.end() : m.end() + 6] == " ?? []"]
    assert not bare, bare


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_joining_a_ledger_without_blocking_does_not_throw():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _script()
    joins = re.findall(r"\(_gate\.resume_ledger\.blocking \?\? \[\]\)\.join\(', '\)", script)
    assert joins
    expr = joins[0].replace("_gate.resume_ledger", "ledger")
    proc = subprocess.run(
        [node, "-e", f"const ledger = {{ kind: 'unclaimed-rows' }}; console.log(JSON.stringify({expr}));"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == '""'
