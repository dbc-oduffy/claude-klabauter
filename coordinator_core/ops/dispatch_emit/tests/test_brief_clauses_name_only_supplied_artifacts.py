"""Every command a brief clause names resolves: its module exists, its flags are ones the module parses, and
every sha is a literal in the brief or the instruction to read HEAD's. No clause names an export the worker must
hand-make."""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit

SHA = "c" * 40
_CMD = re.compile(r"python -m (coordinator_core(?:\.\w+)+)([^`]*)`")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")


def _clauses() -> dict:
    seam = emit.SeamLeg((), {}, (), {}, ())
    seam_js = "\n".join(
        emit._seam_leg_js(
            seam, None, plan_path=None, run_base_sha=SHA, js=emit._js_string_literal,
            head="", repo_flag="", sidecar_js="''", repo_root=None,
        )
    )
    return {
        "foreign_red": emit._foreign_red_clause(),
        "arm": emit._isolated_arm_build_clause(),
        "baseline_context": emit._baseline_context_clause(SHA, ["coordinator_core/x.py"]),
        "seam": seam_js,
    }


@pytest.mark.parametrize("name", ["foreign_red", "arm", "baseline_context", "seam"])
def test_every_command_a_clause_names_resolves(name):
    text = _clauses()[name]
    cmds = _CMD.findall(text)
    assert cmds, f"{name} names no engine command"
    for module, rest in cmds:
        spec = importlib.util.find_spec(module)
        assert spec and spec.origin, f"{name}: {module} is not an engine module"
        source = Path(spec.origin).read_text(encoding="utf-8")
        assert "__main__" in source, f"{name}: {module} has no command entry"
        for flag in _FLAG.findall(rest):
            assert f'"{flag}"' in source, f"{name}: {module} does not parse {flag}"
        for sha in re.findall(r"--(?:base-)?sha (<[^>]*>|\S+)", rest):
            assert sha == SHA or sha == "<HEAD's 40-hex sha from git rev-parse HEAD>", (name, sha)


@pytest.mark.parametrize("name", ["foreign_red", "arm", "baseline_context", "seam"])
def test_no_clause_requires_an_export_the_worker_must_supply(name):
    text = _clauses()[name]
    assert "--base-export" not in text and "<run-base export>" not in text and "run-base export" not in text
