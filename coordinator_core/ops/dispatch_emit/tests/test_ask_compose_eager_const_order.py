"""Every top-level `const _PF… = <expr>;` in a composed ask script reads only `_PF` names declared above it.

A const evaluates eagerly, so a reference to a later `_PF` throws `Cannot access '_PFn' before
initialization` and the whole run dies before its first agent (the `_share_sidecar_dir` alias once sat
above the marker while the fragment hoist declared its literal below it).
"""

import re

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_byte_budget import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import _PLUGIN_ROOT, _padded_plan_blitz_text

_DECL = re.compile(r"^const (_PF\w*) = (.*);$")
_REF = re.compile(r"\b_PF\w*\b")


def _forward_reads(script: str) -> list[str]:
    declared: set[str] = set()
    bad = []
    for n, line in enumerate(script.splitlines(), 1):
        m = _DECL.match(line)
        if not m:
            continue
        expr = re.sub(r"'(?:[^'\\]|\\.)*'", "''", m.group(2))  # names inside string literals are text
        bad += [f"line {n}: {m.group(1)} reads {r}" for r in _REF.findall(expr) if r not in declared]
        declared.add(m.group(1))
    return bad


def test_bare_arm_consts_read_only_earlier_consts(monkeypatch):
    assert _forward_reads(_compose(monkeypatch, arm=None)) == []


def test_m_plus_arm_consts_read_only_earlier_consts(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)  # the padded blitz reads plan-blitz.mjs from it
    script =_compose(monkeypatch, arm="m_plus", plan_blitz_text=_padded_plan_blitz_text())
    assert _forward_reads(script) == []


def test_a_shared_sidecar_dir_alias_value_is_out_of_the_fragment_hoists_reach(monkeypatch):
    monkeypatch.setattr(ask_compose._prompt_fragments, "HOIST_ENABLED", True)
    pre = ".coordinator-local/workflow-sidecars/run/stage/"
    lines = [f"x{i} = 'sidecar_dir: {pre}leaf{i}';" for i in range(3)]
    script = "head;\n" + ask_compose.ASK_MANIFEST_MARKER + " m\n" + "\n".join(lines) + "\n"
    out = ask_compose._share_sidecar_dir(script)
    assert f"const _PF9100 = `sidecar_dir: {pre}`;" in out
