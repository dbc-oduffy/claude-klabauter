"""The production bare ask continues an in-run M+ pick by chaining into the {sizing} script; plan-blitz is never inlined."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_brief import EmBrief
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import _CEILING
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

_HALT = "does not embed the plan-blitz stage"
_DEFINES_BLITZ = "async function planBlitz("
_CHAIN = "await workflow({ scriptPath: _chainPath })"

# Resolved at import, before the per-test home quarantine hides the real plugin install.
_PLUGIN_ROOT = resolve_plugin_root()


def _production(monkeypatch, **over) -> str:
    if not _PLUGIN_ROOT:
        pytest.skip("plugin root did not resolve")
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    kw = dict(repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None, **REVIEW_KW)
    kw.update(over)
    return compose_ask_script(**kw)


def test_bare_ask_at_default_arguments_chains_without_inlining_plan_blitz(monkeypatch):
    script = _production(monkeypatch)
    size = len(script.encode("utf-8"))
    print(
        f"bare-ask bytes: {size}; planBlitz defined: {'yes' if _DEFINES_BLITZ in script else 'no'}; "
        f"chain present: {'yes' if _CHAIN in script else 'no'}; refire-halt occurrences: {script.count(_HALT)}"
    )
    assert _DEFINES_BLITZ not in script
    assert _CHAIN in script
    assert script.count(_HALT) == 0
    assert size <= _CEILING


def test_bare_ask_with_an_em_brief_chains(monkeypatch):
    script = _production(monkeypatch, em_brief=EmBrief(text="a brief", source="inline", context=()))
    assert _DEFINES_BLITZ not in script and _CHAIN in script
    assert script.count(_HALT) == 0


def test_intake_sliced_ask_chains(monkeypatch):
    monkeypatch.setattr(ask_compose, "_intake_sizing", lambda repo_root, rel: {"estimate": None})
    script = _production(monkeypatch, prompt=None, sizing_rel="state/sizings/x.yaml")
    assert _DEFINES_BLITZ not in script and _CHAIN in script
    assert script.count(_HALT) == 0


def test_plan_blitz_size_never_enters_the_bare_script(monkeypatch):
    assert _production(monkeypatch, plan_blitz_text="x" * 279005) == _production(monkeypatch, plan_blitz_text="y")

