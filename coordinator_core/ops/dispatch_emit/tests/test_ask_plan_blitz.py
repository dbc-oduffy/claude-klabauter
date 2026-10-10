"""ask_plan_blitz: the wrapped stage is one async function, no top-level return, phases returned."""

import pytest

from coordinator_core.ops.dispatch_emit import ask_plan_blitz as apb

STUB = """/* header { not meta } */
export const meta = {
  name: 'plan-blitz',
  description: 'a } brace in a string',
  phases: [
    { title: 'Size', detail: 'x' },
    { title: "Plan", detail: 'it\\'s' },
  ],
}
const parsedArgs = (typeof args === 'string') ? JSON.parse(args) : args
// comment with }
return { ok: parsedArgs.n }
"""


def test_wrap_defines_one_function_and_returns_phases():
    text, phases = apb.wrap_stage(STUB)
    assert phases == ["Size", "Plan"]
    assert text.startswith("async function planBlitz(args) {")
    assert text.count("async function") == 1
    assert "export const meta" not in text
    assert text.rstrip().endswith("}")
    assert text.count("return") == 1 and text.index("return") > text.index("async function")
    assert "return { ok: parsedArgs.n }" in text


SHADOW = "const agent = (p, o) => _askAgent(_EM_BRIEF + p, o);"


def test_without_kwarg_output_is_unchanged():
    text, _ = apb.wrap_stage(STUB)
    assert "_askAgent" not in text
    assert text == apb.wrap_stage(STUB, agent_prefix_var=None)[0]


def test_shadow_is_first_statement():
    text, phases = apb.wrap_stage(STUB, agent_prefix_var="_EM_BRIEF")
    assert phases == ["Size", "Plan"]
    first = text.split("\n", 2)[1]
    assert first == SHADOW


def test_top_level_agent_binding_refuses():
    for decl in ("const agent = 1", "let agent", "function agent() {}", "async function agent() {}"):
        with pytest.raises(apb.AskPlanBlitzRefused, match="agent"):
            apb.wrap_stage(STUB + decl + "\n", agent_prefix_var="_EM_BRIEF")
    apb.wrap_stage(STUB + "const agentType = 1\n  const agent = 2\n", agent_prefix_var="_EM_BRIEF")


def test_real_plan_blitz_shadow():
    try:
        text = apb.load_plan_blitz_text()
    except apb.AskPlanBlitzRefused:
        pytest.skip("plugin root unresolved")
    wrapped, _ = apb.wrap_stage(text, agent_prefix_var="_EM_BRIEF")
    assert wrapped.split("\n", 2)[1] == SHADOW
    assert wrapped.count("async function planBlitz") == 1 and wrapped.rstrip().endswith("}")


def test_no_meta_refuses():
    with pytest.raises(apb.AskPlanBlitzRefused):
        apb.wrap_stage("const x = 1\n")


def test_load_refuses_by_name_when_missing(tmp_path):
    with pytest.raises(apb.AskPlanBlitzRefused, match="plan-blitz.mjs"):
        apb.load_plan_blitz_text(str(tmp_path))
    with pytest.raises(apb.AskPlanBlitzRefused, match="did not resolve"):
        apb.load_plan_blitz_text("")


def test_load_reads_stub(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "plan-blitz.mjs").write_text(STUB, encoding="utf-8")
    assert apb.load_plan_blitz_text(str(tmp_path)) == STUB


def test_real_plan_blitz_wraps_when_resolvable():
    try:
        text = apb.load_plan_blitz_text()
    except apb.AskPlanBlitzRefused:
        pytest.skip("plugin root unresolved")
    _, phases = apb.wrap_stage(text)
    assert phases[0] == "Size" and len(phases) >= 5
