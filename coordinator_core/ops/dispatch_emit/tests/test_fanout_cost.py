"""Pins the fanout_cost grouping rule, meta/reply rendering and group helpers."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import check_meta_pure_literal, extract_meta_block
from coordinator_core.ops.dispatch_emit import fanout_cost as f
from coordinator_core.ops.dispatch_emit.pipeline_compose import _check_fanout_group
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.win_portability import no_console_creationflags

META = "export const meta = {\n  name: 'x',\n  description: 'a {brace} d',\n  phases: ['Run'],\n};\n"


def _units(n, key="k", size=100):
    return [f.Unit(f"u{i}", key, size) for i in range(n)]


def test_six_small_units_group_two_per_agent_in_order():
    groups = f.propose_groups(_units(6))
    assert groups == (("u0", "u1"), ("u2", "u3"), ("u4", "u5"))


def test_odd_count_leaves_a_trailing_solo():
    assert f.propose_groups(_units(5))[-1] == ("u4",)


def test_fewer_than_three_units_never_group():
    assert f.propose_groups(_units(2)) == (("u0",), ("u1",))
    assert f.propose_groups([]) == ()


def test_split_mode_is_one_group_per_unit():
    assert f.propose_groups(_units(6), "split") == tuple((f"u{i}",) for i in range(6))


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        f.propose_groups(_units(3), "bogus")


def test_units_group_only_with_equal_key_neighbours():
    units = [f.Unit("a", "x", 1), f.Unit("b", "y", 1), f.Unit("c", "y", 1), f.Unit("d", "x", 1)]
    assert f.propose_groups(units) == (("a",), ("b", "c"), ("d",))


def test_large_unit_stays_solo_and_does_not_pair():
    units = _units(3) + [f.Unit("big", "k", f.LARGE_UNIT_BYTES + 1)] + _units(2)
    groups = f.propose_groups(units)
    assert ("big",) in groups
    assert groups[0] == ("u0", "u1")


def test_pair_past_context_cap_stays_split():
    half = f.GROUP_CONTEXT_CAP_BYTES // 2 + 1
    assert half <= f.LARGE_UNIT_BYTES
    units = [f.Unit(f"u{i}", "k", half) for i in range(4)]
    assert f.propose_groups(units) == (("u0",), ("u1",), ("u2",), ("u3",))


def test_every_unit_lands_in_exactly_one_group():
    units = _units(7) + [f.Unit("odd", "z", 5)]
    flat = [i for g in f.propose_groups(units) for i in g]
    assert flat == [u.id for u in units]


def test_cost_total_and_basis_validation():
    cost = f.FanoutCost(6, 3, "exact", groups=(("a", "b"),))
    assert cost.startup_tokens_total == 3 * f.STARTUP_TOKENS_PER_AGENT
    assert cost.meta_dict()["groups"] == [["a", "b"]]
    assert "groups" not in f.FanoutCost(1, 1, "exact").meta_dict()
    with pytest.raises(ValueError):
        f.FanoutCost(1, 1, "guess")


def test_expected_basis_is_accepted_and_rendered():
    assert "expected" in f.BASES
    cost = f.FanoutCost(4, 9, "expected")
    assert cost.meta_dict()["basis"] == "expected"
    assert cost.startup_tokens_total == 9 * f.STARTUP_TOKENS_PER_AGENT
    with pytest.raises(ValueError):
        f.FanoutCost(4, 9, "estimated")


def test_row_output_tokens_emitted_only_when_set():
    assert "rowOutputTokensTotal" not in f.FanoutCost(4, 9, "expected").meta_dict()
    assert f.FanoutCost(4, 9, "expected", row_output_tokens=120).meta_dict()["rowOutputTokensTotal"] == 120
    assert f.FanoutCost(4, 9, "expected", row_output_tokens=0).meta_dict()["rowOutputTokensTotal"] == 0


def test_default_budget_tokens_is_a_positive_int_outside_the_frozen_vocab():
    from coordinator_core.contract import grind_vocab as gv

    assert isinstance(gv.DEFAULT_BUDGET_TOKENS, int) and gv.DEFAULT_BUDGET_TOKENS == 2_000_000
    assert "DEFAULT_BUDGET_TOKENS" not in gv.KNOB_NAMES


def test_with_fanout_is_a_pure_literal_and_reads_back():
    cost = f.FanoutCost(6, 3, "lower-bound", groups=(("u0", "u1"), ("u2",)))
    out = f.with_fanout(META, cost)
    block = extract_meta_block(out)
    assert block is not None and check_meta_pure_literal(block) == []
    assert f.read_fanout(out) == cost.meta_dict()
    assert f.read_fanout(out)["expectedAgents"] == 3
    assert out.startswith(META.split("};")[0].rstrip(",").rstrip())


def test_with_fanout_adds_comma_after_a_last_entry_without_one():
    out = f.with_fanout("export const meta = {\n  name: 'x'\n};\n", f.FanoutCost(1, 1, "exact"))
    assert "name: 'x',\n  fanout:" in out
    assert f.read_fanout(out)["units"] == 1


def test_with_fanout_inside_a_full_script_leaves_the_rest_alone():
    script = META + "\nconst a = {x: 1};\nreturn 1;\n"
    out = f.with_fanout(script, f.FanoutCost(3, 3, "exact"))
    assert out.endswith("\nconst a = {x: 1};\nreturn 1;\n")


def test_with_fanout_refuses_missing_meta_and_double_write_and_impure_ids():
    with pytest.raises(ValueError):
        f.with_fanout("const a = 1;", f.FanoutCost(1, 1, "exact"))
    once = f.with_fanout(META, f.FanoutCost(1, 1, "exact"))
    with pytest.raises(ValueError):
        f.with_fanout(once, f.FanoutCost(1, 1, "exact"))
    with pytest.raises(ValueError):
        f.with_fanout(META, f.FanoutCost(1, 1, "exact", groups=(("run(x)",),)))


def test_read_fanout_is_none_without_an_entry_or_meta():
    assert f.read_fanout(META) is None
    assert f.read_fanout("nothing here") is None


def test_group_prompt_names_members_in_order_and_wraps_each_prompt():
    expr = f.group_prompt(["p0", "p1"], ["a", "b"])
    assert expr.index("Assignment a") < expr.index("(p0)") < expr.index("Assignment b") < expr.index("(p1)")
    assert "<ID>: <STATUS>" in expr and "a, b" in expr
    with pytest.raises(ValueError):
        f.group_prompt(["p0"], ["a", "b"])


def test_pipeline_compose_refuses_unknown_fanout_group():
    _check_fanout_group("auto")
    _check_fanout_group("split")
    with pytest.raises(PipelineEmitRefused):
        _check_fanout_group("bogus")


def test_agent_call_re_matches_a_dispatch_site_not_a_member_call():
    text = "await agent(p);\nconst x = _rows.agent(p);\nmyagent(p);\n  agent(q)"
    assert len(f.AGENT_CALL_RE.findall(text)) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_member_result_helper_returns_each_members_own_line_or_null():
    probe = (
        f.GROUP_MEMBER_RESULT_JS
        + "const r = 'work\\nC1: DONE ok\\nC10: PARTIAL half';"
        + "console.log(JSON.stringify([_memberResult(r,'C1'),_memberResult(r,'C10'),_memberResult(r,'C2'),_memberResult(null,'C1')]));"
    )
    out = subprocess.run(
        [shutil.which("node"), "-"], input=probe, capture_output=True, text=True, timeout=30,
        **no_console_creationflags(),
    )
    assert out.stdout.strip() == '["DONE: ok","PARTIAL: half",null,null]'


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_member_result_credits_decorated_and_inline_status_lines_each_to_own_row():
    r = (
        "work done\\n**C0: DONE** ran 51 tests pass\\n- S1: PARTIAL half\\n"
        "* `T1`: BLOCKED nope\\nC1 notes: DONE-ish\\nC10: DONE ten\\nfinal: Z9: DONE inline"
    )
    probe = (
        f.GROUP_MEMBER_RESULT_JS
        + f"const r = '{r}';"
        + "console.log(JSON.stringify(['C0','S1','T1','C1','C10','Z9','Q'].map(i=>_memberResult(r,i))));"
    )
    out = subprocess.run(
        [shutil.which("node"), "-"], input=probe, capture_output=True, text=True, timeout=30,
        **no_console_creationflags(),
    )
    assert out.stdout.strip() == (
        '["DONE: ran 51 tests pass","PARTIAL: half","BLOCKED: nope",null'
        ',"DONE: ten","DONE: inline",null]'
    )


_HQ07_REPLY = (
    "C0: DONE `_manifest_from_json` no longer coerces `ttl_s` through `int()`, so `true` and `1.5` are now "
    "refused. I added a test for that in `test_main.py`, and 53 tests pass across `test_contract`, `test_main`, "
    "`test_bundle` and `test_launch`. Report: `.coordinator-local/subagent-share/dispatch-reports/"
    "2026-10-19-gpu-placement-ledger-runpod-job-shape/C0.md`.\n"
    "S1: DONE `job-shape.md` already held both shapes and the TTL-expiry finding. I added the missing term-trap "
    "paragraph distinguishing pod TTL from the GPU-tenant lease `ttl_s`. I did not re-check the doc's other "
    "file:line cites. Report: `.coordinator-local/subagent-share/dispatch-reports/"
    "2026-10-19-gpu-placement-ledger-runpod-job-shape/S1.md`.\n\n"
    "Beyond brief: I briefly wrote a scratch file `scratch/c0_append.txt` through Bash, outside my footprint, "
    "and deleted it straight away.\n"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_hq07_reply_classifies_both_rows_done_through_the_real_run_row_regexes():
    from coordinator_core.ops.dispatch_emit import emit

    probe = (
        f.GROUP_MEMBER_RESULT_JS
        + emit._MEMBER_STATUS_JS
        + f"const r = {json.dumps(_HQ07_REPLY)};"
        + f"const any = {emit._ANY_STATUS_JS_RE}, nonDone = {emit._NON_DONE_STATUS_JS_RE};"
        + "const cls = (res) => { const t = JSON.stringify(res ?? null);"
        + " return nonDone.test(t) ? 'nondone' : any.test(t) ? 'done' : 'unanswered'; };"
        + "console.log(JSON.stringify(['C0','S1','T1'].map(i=>cls(_memberStatus(r,i)))));"
    )
    out = subprocess.run(
        [shutil.which("node"), "-"], input=probe, capture_output=True, text=True, timeout=30,
        **no_console_creationflags(),
    )
    assert out.stdout.strip() == '["done","done","unanswered"]'
