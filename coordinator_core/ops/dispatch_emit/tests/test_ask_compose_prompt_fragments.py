"""Hoisted prompt fragments leave every run-time string unchanged and keep the largest ask under the byte ceiling."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_CEILING = int(0.90 * emit._WORKFLOW_SCRIPT_BYTE_CAP)


_STUB_KW = dict(
    wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
    plan_blitz_text="stub",
    roadmap_blitz_text="export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};",
    **REVIEW_KW,
)


def _largest(monkeypatch, *, hoist: bool) -> str:
    """The m_plus arm with every leg: over the installed plan-blitz, roadmap-blitz and review assets when
    they resolve, else over their stubs."""
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", hoist)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(repo_root="REPO", prompt="do a big thing", sizing_rel=None, run_id="run-1", session_id=None, script_path="x.mjs")
    try:
        return ask_compose.compose_ask_script(**kw)
    except AskPlanBlitzRefused:
        return ask_compose.compose_ask_script(**kw, **_STUB_KW)


def test_hoisting_leaves_every_runtime_prompt_identical(monkeypatch):
    plain = _largest(monkeypatch, hoist=False)
    hoisted = _largest(monkeypatch, hoist=True)
    assert "const _PF0 = '" in hoisted and "const _PF" not in plain
    assert prompt_fragments.resolve_fragment_refs(hoisted) == prompt_fragments.fold_literals(plain)


def test_fragment_consts_precede_their_first_use(monkeypatch):
    hoisted = _largest(monkeypatch, hoist=True)
    first_ref = hoisted.index("_PF0 +") if "_PF0 +" in hoisted else hoisted.index("+ _PF0")
    assert hoisted.index("const _PF0 = ") < first_ref


def test_largest_ask_stays_under_ninety_percent_of_the_runner_cap(monkeypatch):
    size = len(_largest(monkeypatch, hoist=True).encode("utf-8"))
    assert size <= _CEILING, f"largest composed ask is {size} bytes; ceiling is {_CEILING} (90% of the runner cap)"


def test_scanner_skips_templates_comments_and_regexes():
    src = "const a = 'x'; // 'no'\nconst r = /'q'/g; const t = `${'in'} 'no'`; const b = 'y';\n"
    spans = prompt_fragments._scan_literals(src)
    assert [src[s:e] for s, e in spans] == ["'x'", "'y'"]


def test_hoist_wraps_a_literal_that_takes_a_method_call(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    sentence = "This sentence is long enough to be hoisted as a shared fragment of the script. "
    src = f"// anchor\nconst a = '{sentence}'.trim();\nconst b = 'z' + '{sentence}';\n"
    out = prompt_fragments.hoist_repeated_fragments(src, anchor="// anchor")
    assert "(_PF0).trim()" in out
    assert prompt_fragments.resolve_fragment_refs(out) == prompt_fragments.fold_literals(src)
