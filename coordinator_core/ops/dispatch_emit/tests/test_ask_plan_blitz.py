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
