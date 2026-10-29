"""compose_execute_review(review_groups=...): per-group pipelined slice review and per-prefix re-verify."""

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.review_mint.execute_review import (
    GROUP_PREP_HALT,
    REVIEW_GROUPS_PHASE,
    compose_execute_review,
    prep_slice_id_for,
)
from coordinator_core.ops.review_mint.post_exec_tail import ReviewGroup, group_slice_prefix
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_GROUPS = [
    ReviewGroup(index=0, rows=("C1",), paths=("a/one.py", "a/two.py"), quiescence_rows=("C1", "C3")),
    ReviewGroup(index=1, rows=("C2",), paths=("b/three.py",), quiescence_rows=("C2",)),
]


def _compose(**extra):
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py", "a/two.py", "b/three.py"],
        prompt_head="BRIEF",
        run_key="k",
        executor_rows=3,
        reverify=True,
    )
    kwargs.update(extra)
    return compose_execute_review(review, **kwargs)


def _by_title(phases):
    return {title: block for title, block in phases}


def test_none_keeps_output_byte_identical():
    assert _compose(review_groups=None) == _compose()


def test_groups_phase_leads_and_each_group_awaits_only_its_quiescence_rows():
    phases = _compose(review_groups=_GROUPS)
    assert [t for t, _ in phases] == [REVIEW_GROUPS_PHASE, "Review prep", "Review wave", "Review integration", "Review re-verify"]
    block = _by_title(phases)[REVIEW_GROUPS_PHASE]
    assert re.search(r"_reviewGroups\[0\] = \(async \(\) => \{ await Promise\.all\(\[_rows\['C1'\], _rows\['C3'\]\]\)", block)
    assert re.search(r"_reviewGroups\[1\] = \(async \(\) => \{ await Promise\.all\(\[_rows\['C2'\]\]\)", block)
    assert "Object.values(_rows)" not in block


def test_group_prep_is_worktree_mode_with_group_paths_and_prefix():
    block = _by_title(_compose(review_groups=_GROUPS))[REVIEW_GROUPS_PHASE]
    assert '"worktree":true' in block
    assert f'"slice_prefix":"{group_slice_prefix("k", 0, "a" * 40)}"' in block
    assert f'"slice_prefix":"{group_slice_prefix("k", 1, "a" * 40)}"' in block
    assert '"paths":["a/one.py", "a/two.py"]' in block
    assert '"paths":["b/three.py"]' in block
    assert "<every declared path>" not in block


def test_whole_diff_agents_stay_in_the_post_barrier_wave_and_slice_agents_leave_it():
    by = _by_title(_compose(review_groups=_GROUPS))
    assert "overengineering-reviewer" in by["Review wave"]
    assert "overengineering-reviewer" not in by[REVIEW_GROUPS_PHASE]
    assert "coordinator:code-reviewer" in by[REVIEW_GROUPS_PHASE]
    assert "agentType: 'coordinator:code-reviewer'" not in by["Review wave"]
    assert "Slice: " not in by["Review wave"]


def test_review_wave_is_group_reviews_then_whole_diff_results():
    wave = _by_title(_compose(review_groups=_GROUPS))["Review wave"]
    assert "const _wholeWave = await parallel(" in wave
    assert "const _reviewWave = [..._groupReviews.flat(), ..._wholeWave];" in wave


def test_prep_awaits_groups_halts_on_group_error_and_merges_group_slices():
    prep = _by_title(_compose(review_groups=_GROUPS))["Review prep"]
    assert prep.index("await Promise.all(_reviewGroups)") < prep.index("const _reviewPrep = await")
    assert GROUP_PREP_HALT in prep
    assert "_reviewPrep.slices = _groupSliceList" in prep
    assert "_verifyOnly" in prep


def test_reverify_names_one_list_call_per_prefix_and_the_union():
    reverify = _by_title(_compose(review_groups=_GROUPS))["Review re-verify"]
    main = prep_slice_id_for("docs/plans/example.md", "a" * 40, "k")
    for prefix in (group_slice_prefix("k", 0, "a" * 40), group_slice_prefix("k", 1, "a" * 40), main):
        assert f'review.reverify \\\'{{"base":"{"a" * 40}","slice_prefix":"{prefix}","run_key":"k"}}' in reverify
    assert "union of their `edited` and `created` lists" in reverify
    assert "_groupReviews[0]" in reverify and "_groupReviews[1]" in reverify and "_wholeWave" in reverify


def _step(reverify: str, n: int) -> str:
    return reverify.split(f"\\n{n}. ", 1)[1].split(f"\\n{n + 1}. ", 1)[0] if n < 4 else reverify.split("\\n4. ", 1)[1]


def test_revert_runs_for_every_prefix_with_the_same_failing_set_and_no_ownership_condition():
    reverify = _by_title(_compose(review_groups=_GROUPS))["Review re-verify"]
    main = prep_slice_id_for("docs/plans/example.md", "a" * 40, "k")
    step4 = _step(reverify, 4)
    assert "for EVERY prefix" in step4 and "same failing set" in step4
    assert "owns" not in reverify and "that owns" not in step4 and "implicated by that prefix" not in reverify
    assert step4.count("<all failing node ids>") == 1
    probe = _step(reverify, 3)
    assert "once for every prefix" in probe and "<all failing node ids>" in probe
    assert re.search(r"union of the `reverted`, `pre_existing`, `kept` and `owned_red` lists", step4)
    assert "unattributed only when every prefix reports it so" in step4
    for prefix in (group_slice_prefix("k", 0, "a" * 40), group_slice_prefix("k", 1, "a" * 40), main):
        assert prefix in reverify


def test_a_flat_prompt_has_no_group_revert_text():
    reverify = _by_title(_compose())["Review re-verify"]
    assert "for EVERY prefix" not in reverify and "once for every prefix" not in reverify


def test_the_rerun_set_is_widened_by_the_verify_scope_map_in_group_mode_only():
    targets = {"a/one.py": ["tests/test_one.py"], "b/three.py": ["tests/test_three.py", "tests/test_x.py"]}
    grouped = _by_title(_compose(review_groups=_GROUPS, verify_scope_targets=targets))["Review re-verify"]
    step2 = _step(grouped, 2)
    assert "tests/test_one.py" in step2 and "tests/test_three.py" in step2 and "tests/test_x.py" in step2
    assert "tripwires" in step2 and "import an edited module" in step2
    assert _by_title(_compose(review_groups=_GROUPS))["Review re-verify"] != grouped
    assert _compose(verify_scope_targets=targets) == _compose()


def test_no_groups_reverify_is_unchanged():
    assert _by_title(_compose())["Review re-verify"] == _by_title(_compose(review_groups=None))["Review re-verify"]
    assert "_groupReviews" not in _by_title(_compose())["Review re-verify"]


def test_groups_without_paths_are_skipped_and_prefix_keeps_the_group_index():
    groups = [
        ReviewGroup(index=0, rows=("V",), paths=(), quiescence_rows=("V",)),
        ReviewGroup(index=1, rows=("C2",), paths=("b/three.py",), quiescence_rows=("C2",)),
    ]
    phases = _compose(review_groups=groups)
    block = _by_title(phases)[REVIEW_GROUPS_PHASE]
    assert "_reviewGroups[1]" not in block
    assert '"slice_prefix":"k.g1-' in block and '"slice_prefix":"k.g0' not in block


def _group_prefixes(**extra):
    block = _by_title(_compose(review_groups=_GROUPS, **extra))[REVIEW_GROUPS_PHASE]
    return set(re.findall(r'"slice_prefix":"(k\.g\d[^"]*)"', block))


def test_a_re_emit_over_a_different_range_gets_fresh_group_slice_ids_and_the_same_range_reuses_them():
    first = _group_prefixes()
    assert first and all(p not in ("k.g0", "k.g1") for p in first)
    assert _group_prefixes() == first
    assert _group_prefixes(run_base_sha="b" * 40).isdisjoint(first)
    marked = _group_prefixes(run_base_sha="<fire-time marker>", slice_identity_sha="a" * 40)
    assert marked == first


def test_all_groups_empty_degrades_to_the_plain_output():
    groups = [ReviewGroup(index=0, rows=("V",), paths=(), quiescence_rows=("V",))]
    assert _compose(review_groups=groups) == _compose()


@pytest.mark.parametrize(
    "extra",
    [{"prep_in_wave": True, "executor_rows_js": "3"}, {"review_terminal_sha": "b" * 40}, {"slice_key_js": "x", "run_key": None}],
)
def test_incompatible_modes_raise(extra):
    kwargs = dict(extra)
    if "executor_rows_js" in kwargs:
        kwargs["executor_rows"] = None
    with pytest.raises(ValueError):
        _compose(review_groups=_GROUPS, **kwargs)


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


def _run_node(script: str) -> str:
    return subprocess.run(
        # stdin, not `-e`: the composed blocks overflow the Windows command-line limit (WinError 206).
        [_node(), "-"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_every_block_parses_as_a_function_body():
    body = "\n".join(block for _, block in _compose(review_groups=_GROUPS))
    out = _run_node("try { new Function('return (async () => {' + process.argv[1] + '})'); console.log('ok'); } catch (e) { console.log(e.message); }".replace("process.argv[1]", json.dumps(body)))
    assert out.strip() == "ok"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_group_review_starts_when_only_its_own_rows_land():
    block = _by_title(_compose(review_groups=_GROUPS))[REVIEW_GROUPS_PHASE]
    script = (
        "(async () => {\n"
        "const phase = () => {}; const parallel = (fns) => Promise.all(fns.map(f => f()));\n"
        "const starts = [];\n"
        "let _halted = null; let _haltedByHold = false;\n"
        "const agent = async (p) => { starts.push(/g0|a\\/one/.test(p) ? 'g0' : 'g1'); "
        "return { prep: { slices: [] } }; };\n"
        "let land1, land2, land3; const _rows = { C1: new Promise(r => { land1 = r; }), "
        "C2: new Promise(r => { land2 = r; }), C3: new Promise(r => { land3 = r; }) };\n"
        + block
        + "\nconst tick = () => new Promise(r => setTimeout(r, 20));\n"
        "land1(); land2(); await tick();\n"
        "const afterRows12 = starts.slice();\n"
        "land3(); await Promise.all(_reviewGroups);\n"
        "console.log(JSON.stringify({ afterRows12: [...new Set(afterRows12)], all: [...new Set(starts)] }));\n"
        "})();"
    )
    out = json.loads(_run_node(script))
    assert out["afterRows12"] == ["g1"]
    assert sorted(out["all"]) == ["g0", "g1"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "decl, expected",
    [
        ("let _halted = 'C2'; let _haltedByHold = false;", []),
        ("let _halted = 'held'; let _haltedByHold = true;", ["g0", "g1"]),
        ("let _halted = null; let _haltedByHold = false;", ["g0", "g1"]),
    ],
)
def test_a_stop_rule_halt_starts_no_group_review_but_a_hold_halt_does(decl, expected):
    block = _by_title(_compose(review_groups=_GROUPS))[REVIEW_GROUPS_PHASE]
    script = (
        "(async () => {\n"
        "const phase = () => {}; const parallel = (fns) => Promise.all(fns.map(f => f()));\n"
        "const starts = [];\n"
        f"{decl}\n"
        "const agent = async (p) => { starts.push(/g0|a\\/one/.test(p) ? 'g0' : 'g1'); "
        "return { prep: { slices: [] } }; };\n"
        "const _rows = { C1: Promise.resolve(), C2: Promise.resolve(), C3: Promise.resolve() };\n"
        + block
        + "\nawait Promise.all(_reviewGroups);\n"
        "console.log(JSON.stringify([...new Set(starts)].sort()));\n"
        "})();"
    )
    assert json.loads(_run_node(script)) == expected
