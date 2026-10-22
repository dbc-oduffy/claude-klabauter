"""mise_residue: batching bounds, census classification, residue args shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.roadmap import census, mise_residue as mr


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    monkeypatch.setattr(census, "tracked_targets", lambda root, cands: frozenset({"tools/ok.py"}))


def _rows(n):
    return [{"plan": f"docs/plans/p{i}.md"} for i in range(n)]


@pytest.mark.parametrize("n,sizes", [(0, []), (1, [1]), (8, [8]), (9, [8, 1]), (17, [8, 8, 1])])
def test_batch_bounds(n, sizes):
    batches = mr.batch_plans(_rows(n))
    assert [len(b["plans"]) for b in batches] == sizes
    assert [b["id"] for b in batches] == [f"b{i + 1}" for i in range(len(sizes))]


def test_batch_each_plan_once_in_order():
    rows = _rows(10) + [{"plan": "docs/plans/p3.md"}]
    flat = [r["plan"] for b in mr.batch_plans(rows) for r in b["plans"]]
    assert flat == [f"docs/plans/p{i}.md" for i in range(10)]


@pytest.mark.parametrize(
    "command",
    ["node x.js", "cd a && npx tsc", "gh pr list | wc -l", "claude -p hi",
     "echo $(date) && node y.js", "python -c 'print(1)' && node y.js"],
)
def test_leftover_heads(command):
    assert mr.classify_census(command, census.screen(command) or "") == "leftover"


@pytest.mark.parametrize(
    "command",
    ["python -c 'print(1)'", "python3 -c 'import x' | wc -l", "python -m pytest tests/test_x.py",
     "pytest -q tests/test_x.py", "cd a && python -m pytest t.py"],
)
def test_python_c_and_pytest_entries_are_step0(command):
    refusal = census.screen(command, Path("."), frozenset()) or ""
    assert mr.classify_census(command, refusal) == "step0"


def test_untracked_python_target_is_leftover():
    command = "python tools/missing.py"
    refusal = census.screen(command, Path("."), frozenset())
    assert mr.classify_census(command, refusal) == "leftover"


@pytest.mark.parametrize(
    "command", ["echo $(date)", "for f in a b; do cat $f; done", "grep x f > out.txt"]
)
def test_rewrite_refusals(command):
    refusal = census.screen(command)
    assert refusal
    assert mr.classify_census(command, refusal) == "rewrite"


PLAN = """---
title: t
census:
  - question: q0
    command: echo $(date)
    result: "1"
  - question: q1
    command: node check.js
    result: "1"
  - question: q2
    command: grep -c x f
    result: "1"
---
body
"""


def test_census_entries_reports_only_unscreenable():
    got = mr.census_entries(PLAN, Path("."), frozenset())
    assert [(e["index"], e["class"]) for e in got] == [(0, "rewrite"), (1, "leftover")]


def test_census_entries_without_census_key():
    assert mr.census_entries("---\ntitle: t\n---\nbody\n", Path("."), frozenset()) == []


def _args(rows, reply=None, texts=None):
    return mr.residue_args(
        rows, reply, repo_root=Path("repo"), run_id="20261019T1630Z-abcd1234",
        gate_command="mise-prep-gate --repo-root repo", residue_agent_type=mr.RESIDUE_AGENT_TYPE,
        residue_model="sonnet", apm_model="opus",
        read_text=lambda root, plan: (texts or {}).get(plan, ""),
    )


def test_residue_args_shape_roundtrips_json():
    rows = [
        {"plan": "a.md", "verdict": "NOT-PREPPED", "failing": ["CENSUS/census-unscreenable"],
         "detail": ["CENSUS: x"], "outcome": "NOT-PREPPED",
         "seam": [{"class": "capabilities-undeclared", "blocking": True, "plan": "a.md"},
                  {"class": "unpromised-export", "blocking": True}]},
        {"plan": "ok.md", "verdict": "PREPPED", "failing": [], "detail": [], "seam": [],
         "outcome": "already-certified"},
    ]
    reply = {"skipped_cycles": [{"class": "seam-fix-cycle", "plans": ["b", "a"], "path": "p", "detail": "d"}]}
    args = _args(rows, reply, {"a.md": PLAN})
    assert json.loads(json.dumps(args)) == args
    assert args["schema"] == "mise-prep-residue-args/4"
    assert args["residue"] == {"agentType": "coordinator:residue-author", "model": "sonnet"}
    assert args["apm"] == {"agentType": "coordinator:apm", "model": "opus"}
    (batch,) = args["batches"]
    (member,) = batch["plans"]
    assert member["plan"] == "a.md"
    assert member["bar"][0] == "bar: CENSUS/census-unscreenable"
    assert len(member["seam"]) == 1 and member["seam"][0].startswith("seam: capabilities-undeclared")
    assert [(c["index"], c["class"]) for c in member["census"]] == [(0, "rewrite")]
    assert member["spine"] == [
        "spine-repair: CENSUS/census-unscreenable — " + mr.SPINE_REPAIR_CLASSES["CENSUS/census-unscreenable"]
    ]
    kinds = [i["kind"] for i in args["apmItems"]]
    assert kinds == ["census-leftover", "seam-fix-cycle"]
    assert args["apmItems"][0]["index"] == 1
    assert mr.has_work(args)


def test_empty_when_all_certified():
    rows = [{"plan": "ok.md", "verdict": "PREPPED", "failing": [], "outcome": "stamped", "seam": []}]
    args = _args(rows)
    assert args["batches"] == [] and args["apmItems"] == []
    assert not mr.has_work(args)


def test_prepped_row_with_seam_and_census_yields_no_batch():
    rows = [{"plan": "a.md", "verdict": "PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True, "path": "x"}]}]
    args = _args(rows, texts={"a.md": PLAN})
    assert args["batches"] == []
    assert args["apmItems"] == []


def test_prepped_seam_refused_row_owing_capabilities_batches():
    rows = [{"plan": "a.md", "verdict": "PREPPED", "failing": [], "detail": [],
             "outcome": "SEAM-REFUSED",
             "seam": [{"class": "capabilities-undeclared", "blocking": True}]}]
    args = _args(rows, texts={"a.md": PLAN})
    (batch,) = args["batches"]
    (member,) = batch["plans"]
    assert member["seam"][0].startswith("seam: capabilities-undeclared")
    assert args["apmItems"] == []
    assert mr.has_work(args)


def test_has_work_false_for_prepped_seam_only_rows():
    rows = [{"plan": "a.md", "verdict": "PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True}]}]
    assert not mr.has_work(_args(rows))


def test_non_prepped_row_with_only_seam_still_batches():
    rows = [{"plan": "a.md", "verdict": "NOT-PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True}]}]
    (batch,) = _args(rows)["batches"]
    assert [m["plan"] for m in batch["plans"]] == ["a.md"]
    member = batch["plans"][0]
    assert member["seam"] == []
    assert member["seamRefLines"] == 1


def _coll(plan, path, cp, **kw):
    return {"class": "writes-collision", "blocking": True, "plan": plan, "path": path,
            "counterpart_plan": cp, **kw}


def _short(plan, seam):
    return {"plan": plan, "verdict": "NOT-PREPPED", "failing": [], "detail": [],
            "outcome": "NOT-PREPPED", "seam": seam}


def test_two_certified_plan_collision_is_absent():
    rows = [
        {"plan": "docs/plans/a.md", "verdict": "PREPPED", "failing": [], "outcome": "stamped",
         "seam": [_coll("docs/plans/a.md", "x.py", "docs/plans/b.md")]},
        {"plan": "docs/plans/b.md", "verdict": "PREPPED", "failing": [], "outcome": "NOT-PREPPED",
         "seam": [_coll("docs/plans/b.md", "x.py", "docs/plans/a.md")]},
    ]
    assert mr.seam_ref_texts(rows, "r1") == {}
    assert _args(rows)["batches"] == []


def test_mixed_pair_yields_no_seamref_for_the_certified_plan():
    rows = [
        {"plan": "docs/plans/a.md", "verdict": "PREPPED", "failing": [], "outcome": "stamped",
         "seam": [_coll("docs/plans/a.md", "x.py", "docs/plans/b.md")]},
        _short("docs/plans/b.md", [_coll("docs/plans/b.md", "x.py", "docs/plans/a.md")]),
    ]
    run = "20261019T1630Z-abcd1234"
    texts = mr.seam_ref_texts(rows, run)
    assert set(texts) == {f"state/mise-workflows/{run}.seam/b.txt"}
    members = [m for b in _args(rows)["batches"] for m in b["plans"]]
    assert [m["plan"] for m in members] == ["docs/plans/b.md"]
    assert not any("a.txt" in str(m.get("seamRef")) for m in members)


def test_two_uncertified_plans_each_keep_their_seamref():
    rows = [
        _short("docs/plans/a.md", [_coll("docs/plans/a.md", "x.py", "docs/plans/b.md")]),
        _short("docs/plans/b.md", [_coll("docs/plans/b.md", "x.py", "docs/plans/a.md")]),
    ]
    run = "20261019T1630Z-abcd1234"
    texts = mr.seam_ref_texts(rows, run)
    assert set(texts) == {f"state/mise-workflows/{run}.seam/a.txt",
                          f"state/mise-workflows/{run}.seam/b.txt"}
    members = [m for b in _args(rows)["batches"] for m in b["plans"]]
    assert {m["seamRef"] for m in members} == set(texts)


def test_counterpart_copy_is_not_in_members_file_and_records_merge():
    rows = [_short("docs/plans/a.md", [
        _coll("docs/plans/a.md", "x.py", "docs/plans/b.md"),
        _coll("docs/plans/a.md", "x.py", "docs/plans/c.md"),
        _coll("docs/plans/a.md", "x.py", "docs/plans/d.md"),
        _coll("docs/plans/z.md", "y.py", "docs/plans/a.md"),
    ])]
    texts = mr.seam_ref_texts(rows, "r1")
    (path, text), = texts.items()
    assert path == "state/mise-workflows/r1.seam/a.txt"
    assert text.endswith("\n") and len(text.splitlines()) == 1
    assert "y.py" not in text and "path=x.py" in text
    assert "also docs/plans/c.md, docs/plans/d.md" in text


def test_args_and_texts_agree_and_no_inline_collision():
    rows = [
        _short("docs/plans/a.md", [_coll("docs/plans/a.md", "x.py", "docs/plans/b.md"),
                                   _coll("docs/plans/a.md", "w.py", "docs/plans/b.md"),
                                   {"class": "capabilities-undeclared", "blocking": True}]),
        _short("docs/plans/b.md", [_coll("docs/plans/b.md", "x.py", "docs/plans/a.md")]),
    ]
    args = _args(rows)
    texts = mr.seam_ref_texts(rows, "20261019T1630Z-abcd1234")
    members = [m for b in args["batches"] for m in b["plans"]]
    assert {m["seamRef"] for m in members} == set(texts)
    for m in members:
        assert m["seamRefLines"] == len(texts[m["seamRef"]].splitlines())
        assert not any(s.startswith("seam: writes-collision") for s in m["seam"])
    assert [len(m["seam"]) for m in members] == [1, 0]
    assert [m["seamRefLines"] for m in members] == [2, 1]


def test_stem_collision_raises():
    rows = [_short("docs/plans/a.md", [_coll("docs/plans/a.md", "x", "q")]),
            _short("other/a.md", [_coll("other/a.md", "x", "q")])]
    with pytest.raises(ValueError):
        mr.seam_ref_texts(rows, "r1")


def test_apm_only_when_cycle_alone():
    args = _args([], {"skipped_cycles": [{"plans": ["b", "a"], "path": "p", "detail": "d"}]})
    assert args["batches"] == [] and len(args["apmItems"]) == 1
    assert mr.has_work(args)


def test_nine_plans_make_two_batches():
    rows = [
        {"plan": f"p{i}.md", "verdict": "NOT-PREPPED", "failing": ["X/y"], "detail": [],
         "outcome": "NOT-PREPPED", "seam": []}
        for i in range(9)
    ]
    args = _args(rows)
    assert [len(b["plans"]) for b in args["batches"]] == [8, 1]


_ROW = {"plan": "a.md", "verdict": "NOT-PREPPED", "failing": [], "detail": [], "outcome": "NOT-PREPPED", "seam": []}
_CYCLE = {"plans": ["b", "a"], "path": "p", "detail": "d"}


def _ruling(item, sha, **kw):
    return {"item": item, "ruling": "r", "run_id": "x", "on": "2026-10-22", "subject_sha": sha, **kw}


def _with_rulings(entries):
    return PLAN.replace("\n---\nbody", "\n" + mr.render_pm_ruling(entries) + "---\nbody")


def test_schema_op_and_args_keys():
    assert mr.SCHEMA == "mise-prep-residue-args/4"
    assert mr.RULINGS_OP == "plan.record_residue_rulings"
    args = _args([_ROW], {"skipped_cycles": [_CYCLE]}, {"a.md": PLAN})
    assert args["rulingsCommand"] == "mise-prep-run --repo-root repo --apply-rulings"
    census_item, cycle_item = args["apmItems"]
    assert census_item["id"] == "census[1]" == mr.ruling_item_id(census_item)
    assert census_item["subjectSha"] == mr.subject_sha(census_item)
    assert cycle_item["id"] == "cycle:p" == mr.ruling_item_id(cycle_item)
    assert cycle_item["subjectSha"] == mr.subject_sha({"kind": "seam-fix-cycle", "plans": ["a", "b"]})


def test_ruled_census_item_is_not_re_raised_but_stale_one_is():
    sha = mr.subject_sha({"kind": "census-leftover", "command": "node check.js"})
    assert _args([_ROW], None, {"a.md": _with_rulings([_ruling("census[1]", sha)])})["apmItems"] == []
    stale = _with_rulings([_ruling("census[1]", "000000000000")])
    assert [i["id"] for i in _args([_ROW], None, {"a.md": stale})["apmItems"]] == ["census[1]"]


def test_ruled_cycle_is_dropped_when_any_plan_carries_it():
    sha = mr.subject_sha({"kind": "seam-fix-cycle", "plans": ["a", "b"]})
    texts = {"b": _with_rulings([_ruling("cycle:p", sha)])}
    assert _args([], {"skipped_cycles": [_CYCLE]}, texts)["apmItems"] == []
    stale = {"b": _with_rulings([_ruling("cycle:p", "000000000000")])}
    assert len(_args([], {"skipped_cycles": [_CYCLE]}, stale)["apmItems"]) == 1


def test_render_pm_ruling_round_trips_through_parse_frontmatter():
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    entry = _ruling("census[3]", "abc123def456", ruling="it's \"q\" \\ x: y\nline2", pm_only=True,
                    ruled_by="coordinator:apm")
    text = "---\n" + mr.render_pm_ruling([entry]) + "---\nb\n"
    (got,) = parse_frontmatter(text)["frontmatter"]["pm_ruling"]
    assert got["on"] == "2026-10-22"
    assert got["pm_only"] is True
    assert got["item"] == "census[3]"
    assert got["ruling"] == "it's \"q\" \\ x: y line2"
    assert got["subject_sha"] == "abc123def456"
    assert got["ruled_by"] == "coordinator:apm"


STEP0_PLAN = """---
title: t
census:
  - question: q0
    command: python -c 'print(1)'
    result: "1"
  - question: q1
    command: node check.js
    result: "1"
---
body
"""


def test_step0_entry_lands_in_census_not_apm_items():
    rows = [{**_ROW, "failing": ["CENSUS/census-unscreenable"]}]
    args = _args(rows, None, {"a.md": STEP0_PLAN})
    (member,) = args["batches"][0]["plans"]
    assert [(c["index"], c["class"]) for c in member["census"]] == [(0, "step0")]
    assert [i["index"] for i in args["apmItems"]] == [1]


def test_node_only_census_stays_leftover_and_member_spine_is_empty():
    rows = [{**_ROW, "failing": ["EXECUTOR_COMMIT/x"]}]
    args = _args(rows, None, {"a.md": PLAN})
    (member,) = args["batches"][0]["plans"]
    assert member["spine"] == []
    assert [i["index"] for i in args["apmItems"]] == [1]


def test_spine_repairs_keys_only_the_five_classes_in_input_order():
    failing = ["SPINE/InvalidChangeKindError", "SPINE/writes-undeclared", "PRIME_EXIT/x",
               "TEST_RUNNER_OUT_OF_ROLE/test-runner-out-of-role-row"]
    got = mr.spine_repairs(failing)
    assert [g.split(" — ")[0] for g in got] == [
        "spine-repair: SPINE/InvalidChangeKindError",
        "spine-repair: TEST_RUNNER_OUT_OF_ROLE/test-runner-out-of-role-row",
    ]
    assert mr.spine_repairs([]) == []


# Step 0: the class strings the gate really produces (mise-prep-run._failing_classes shape).
_FM = ("census: []\ncapabilities: []\nprime_exit_criterion:\n  statement: s\n"
       "  derived_from: state/sizings/x.yaml\n")
_CENSUS_FM = _FM.replace("census: []", "census:\n  - question: q\n    command: python -c 'print(1)'\n    result: '1'")
_ROW_T = "- id: C0\n  title: t\n  change_kind: {ck}\n  surface: src/a.py\n  writes: {w}\n  {x}disposition: open\n  body: |\n    {b}\n"
_MEMO_T = ("- id: M1\n  title: t\n  change_kind: doc-edit\n  surface: {s}\n  writes: []\n"
           "  kind: memo-send\n  disposition: open\n  body: |\n    Send.\n")


def _gate_classes(tmp_path, fm, spine):
    from coordinator_core.roadmap import prep_gate
    from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _write_plan

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    gate = prep_gate.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=fm, spine=spine))
    return [f"{n}/{b.get('kind') or b.get('status')}" for n, b in gate["classes"].items() if b.get("status") != "PASS"]


_RUNNER = "agent_type: coordinator:test-runner\n  "


@pytest.mark.parametrize(
    "fm,spine,expected",
    [
        (_CENSUS_FM, _ROW_T.format(ck="code-edit", w="[src/a.py]", x="", b="do"),
         "CENSUS/census-unscreenable"),
        (_FM, _MEMO_T.format(s="docs/x.md"), "SPINE/MemoRowNoReceiptError"),
        (_FM, _MEMO_T.format(s=".coordinator-local/memo-outbox/t.md"), "SPINE/MemoRowNoReceiverError"),
        (_FM, _ROW_T.format(ck="bogus-kind", w="[src/a.py]", x="", b="do"), "SPINE/InvalidChangeKindError"),
        (_FM, _ROW_T.format(ck="verification", w="[]", x=_RUNNER, b="Run `python -m pytest tests/`."),
         "TEST_RUNNER_OUT_OF_ROLE/test-runner-out-of-role-row"),
    ],
)
def test_gate_class_strings_are_keyed(tmp_path, fm, spine, expected):
    assert _gate_classes(tmp_path, fm, spine) == [expected]
    assert expected in mr.SPINE_REPAIR_CLASSES


def test_unquoted_on_key_yields_no_failing_class(tmp_path):
    spine = _ROW_T.format(ck="code-edit", w="[src/a.py]", x="on: push\n  ", b="do")
    assert _gate_classes(tmp_path, _FM, spine) == []


@pytest.mark.parametrize("text", ["no frontmatter", "---\npm_ruling: nope\n---\n", "---\npm_ruling:\n  - 3\n---\n"])
def test_ruled_items_empty_when_absent_or_malformed(text):
    assert mr.ruled_items(text) == frozenset()
