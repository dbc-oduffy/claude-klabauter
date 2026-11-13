"""The optional pre-vet leg of the composed grind script.

Absent ``prevet`` the script is the golden, byte for byte. Present, one cheap call per batch classifies
rows and ONE refute-close call per batch closes the candidates; anything the refuter does not confirm,
and any call that fails, returns the row to triage. The JS legs run under node against test doubles for
the two agent calls (a live model is never asserted on).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import _workflow_contract as wc
from coordinator_core.ops.dispatch_emit import fanout_cost
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp

from .conftest import REVIEW_KW
from .test_grind_compose import (
    _FIXTURE_PROFILE_DIR,
    _GOLDEN_PATH,
    _compose,
    _fixture_manifest,
    _fixture_profile,
    _stage_model_option_count,
    _undeclared_assignments,
    grind_only,
)

pytestmark = pytest.mark.complete


def _prevet_profile(model: str = "haiku", batch_size: int = 25):
    return dataclasses.replace(_fixture_profile(), prevet=gp.Prevet(model=model, batch_size=batch_size))


def _prevet_compose(**kw):
    return _compose(profile=_prevet_profile(), **kw)


# ---------------------------------------------------------------------------
# Profile key
# ---------------------------------------------------------------------------


def _profile_dir_with(tmp_path: Path, **extra) -> Path:
    doc = yaml.safe_load((_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"))
    doc.update(extra)
    (tmp_path / "fixture.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


def test_absent_prevet_is_off():
    assert _fixture_profile().prevet is None


def test_prevet_key_loads_with_the_default_batch_size(tmp_path):
    profile = gp.load_profile("fixture", _profile_dir_with(tmp_path, prevet={"model": "haiku"}))
    assert profile.prevet == gp.Prevet(model="haiku", batch_size=25)
    sized = gp.load_profile("fixture", _profile_dir_with(tmp_path, prevet={"model": "haiku", "batch_size": 10}))
    assert sized.prevet.batch_size == 10


@pytest.mark.parametrize(
    "bad",
    [
        "haiku",
        {},
        {"model": ""},
        {"model": "haiku", "batch_size": 0},
        {"model": "haiku", "batch_size": True},
        {"model": "haiku", "batch_size": "25"},
        {"model": "haiku", "extra": 1},
    ],
)
def test_malformed_prevet_is_refused(tmp_path, bad):
    with pytest.raises(gp.ProfileError) as err:
        gp.load_profile("fixture", _profile_dir_with(tmp_path, prevet=bad))
    assert err.value.rule == "profile_field_type"


def test_prevet_without_a_refute_close_node_is_refused():
    graph = {"triage": gp.GraphNode(id="triage", kind="triage")}
    with pytest.raises(gp.ProfileError) as err:
        gp._build_prevet({"model": "haiku"}, graph)
    assert err.value.rule == "prevet_needs_refute_close"
    with pytest.raises(ValueError):
        gc.compose_grind_script(
            _fixture_manifest(),
            dataclasses.replace(
                _fixture_profile(),
                graph={"triage": gp.GraphNode(id="triage", kind="triage")},
                prevet=gp.Prevet(model="haiku"),
            ),
            gp.resolve_appetite(_fixture_profile(), "standard"),
            run_dir=Path("state/queue-grind/fixture/run-1"),
            **REVIEW_KW,
        )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_off_leaves_the_golden_untouched():
    script = _compose()
    assert script == _GOLDEN_PATH.read_text(encoding="utf-8")
    assert "_prevet" not in script and "PREVET" not in script


def test_on_adds_exactly_one_agent_call_site_with_the_profile_model():
    base = grind_only(_compose())
    on = grind_only(_prevet_compose())
    assert len(re.findall(r"\bagent\(", on)) == len(re.findall(r"\bagent\(", base)) + 1
    assert len(re.findall(r"_recordCall\('", on)) == len(re.findall(r"\bagent\(", on))
    assert on.count("label: 'prevet'") == 1
    prevet_call = on[on.index("label: 'prevet'"):]
    assert prevet_call[: prevet_call.index("});")].count("model: 'haiku'") == 1


def test_prevet_model_is_the_profiles_not_the_run_triage_override():
    script = _compose(profile=_prevet_profile(model="sonnet"), stage_models={"triage": "opus"})
    prevet_call = script[script.index("label: 'prevet'"):]
    assert "model: 'sonnet'" in prevet_call[: prevet_call.index("});")]
    assert script.count("model: 'opus'") == _compose(stage_models={"triage": "opus"}).count("model: 'opus'")


def test_on_script_is_clean_and_deterministic():
    script = _prevet_compose()
    assert script == _prevet_compose()
    assert [f for f in wc.run_checks(script) if f.severity.value == "ERROR"] == []
    assert _undeclared_assignments(script) == set()
    on = grind_only(script)
    assert len(re.findall(r"\bagent\(", on)) == _stage_model_option_count(on)


def test_prevet_runs_once_before_the_grind_and_triage_skips_settled_rows():
    script = _prevet_compose()
    assert script.count("await _prevetAll();") == 1
    assert "try {\nawait _prevetAll();\ntry { await runGrind(); }" in script
    triage = script[script.index("async function _triageBatch("):]
    triage = triage[: triage.index("\n}\n")]
    assert "const _tRows = batch.rows.filter((r) => !_rows[r].done && !_rows[r].node);" in triage
    assert "_triageCall(batchState.id, batchState.batchKey, _tRows)" in triage


def test_prompt_carries_the_decision_rules_and_every_class():
    script = _prevet_compose()
    call = script[: script.index("label: 'prevet'")]
    prompt = call[call.rindex("async function _prevetCall("):]
    for rule in gc.PREVET_DECISION_RULES:
        assert rule[:30].replace("'", "\\'") in prompt
    for cls in gc.PREVET_CLASSES:
        assert cls in prompt


def test_fanout_estimate_counts_the_prevet_legs():
    base = fanout_cost.read_fanout(_compose())
    on = fanout_cost.read_fanout(_prevet_compose())
    assert on["expectedAgents"] == base["expectedAgents"] + 2 * -(-len(_fixture_manifest().entries) // 25)


# ---------------------------------------------------------------------------
# Behaviour of the leg, under node, against doubles for the two agent calls
# ---------------------------------------------------------------------------

_HARNESS = """
const BATCHES = [{ id: 'b0', rows: ROWS }];
const QUEUE_GRIND_MANIFEST = { entries: ROWS.map((r) => ({ row_id: r, path: 'q/' + r + '.yaml' })) };
const ROUTING = { 'refute-close': { kind: 'refute-close', edges: { confirmed: 'commit' } }, commit: { kind: 'commit', edges: {} } };
const PREVET_BATCH_SIZE = BATCH_SIZE;
const PREVET_CLOSE_NODE = 'refute-close';
const MAX_AGENT_CALLS = null, BUDGET_TOKENS = null, RESERVE = 0;
let _callCount = 0;
const budget = { spent: () => 0 };
const _startSpent = 0;
const _rows = {};
for (const r of ROWS) { _rows[r] = { rowId: r, path: 'q/' + r + '.yaml', digest: 'd' + r, pmRuling: PM[r] || '', node: null, origin: '', evidence: '', done: false, touchedFiles: [], removedFiles: [], createdFiles: [] }; }
const _handed = [];
const _queued = [];
const _calls = { prevet: [], close: [] };
const _byRefuteOrigin = {};
function _bumpRefuteOrigin(o, k) { (_byRefuteOrigin[o] = _byRefuteOrigin[o] || { confirmed: 0, refuted: 0 })[k] += 1; }
function _normalizeRowRef(items, ref) { const hit = items.find((it) => it.path === ref); return hit ? hit.row_id : ref; }
function _archiveDestination(p) { return 'archive/' + p; }
function followEdge(n, o, row) { const t = ROUTING[n].edges[o]; return { kind: 'node', value: t }; }
function applyRoute(row, id, route) { if (route.kind === 'handback') { row.done = true; } else { row.node = route.value; } }
function _handBack(id, type, reason) { _handed.push({ id, type }); }
function _queueCommit(id) { _queued.push(id); }
function _issue(b, r, fn) { return fn(); }
async function _prevetCall(ids) { _calls.prevet.push(ids); return PREVET_REPLY(ids); }
async function _closeCall(props) { _calls.close.push(props.map((p) => p.row_id)); return CLOSE_REPLY(props); }
"""


def _run_leg(rows, prevet_reply, close_reply, *, batch_size=25, pm=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    js = (
        f"const ROWS = {json.dumps(rows)}; const BATCH_SIZE = {batch_size}; const PM = {json.dumps(pm or {})};\n"
        f"const PREVET_REPLY = {prevet_reply};\nconst CLOSE_REPLY = {close_reply};\n"
        + _HARNESS
        + gc._PREVET_JS
        + "\n(async () => { await _prevetAll();\n"
        "console.log(JSON.stringify({ rows: _rows, handed: _handed, queued: _queued, calls: _calls, origins: _byRefuteOrigin })); })();"
    )
    out = subprocess.run(
        [node, "-"], input=js, capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_MIXED_PREVET = (
    "async (ids) => ({ rows: ["
    "{ row: 'r1', class: 'discard-candidate', has_rule: false, witness: 'only a dated narrative' },"
    "{ row: 'r2', class: 'dedupe-candidate', has_rule: true, witness: 'coordinator/docs/wiki/x.md' },"
    "{ row: 'r3', class: 'keep', has_rule: true, witness: '' }] })"
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_confirmed_candidate_closes_in_one_refute_call_and_refuted_returns_to_triage():
    close = "async (props) => ({ confirmed: [{ row: 'r1', new_path: 'x' }], refuted: [{ row: 'r2', reason: 'live' }] })"
    got = _run_leg(["r1", "r2", "r3"], _MIXED_PREVET, close)
    assert got["calls"] == {"prevet": [["r1", "r2", "r3"]], "close": [["r1", "r2"]]}
    r1, r2, r3 = (got["rows"][k] for k in ("r1", "r2", "r3"))
    assert r1["done"] and r1["origin"] == "prevet:discard" and r1["removedFiles"] == ["q/r1.yaml"]
    assert r1["touchedFiles"] == ["archive/q/r1.yaml"]
    assert got["queued"] == ["r1"]
    assert (r2["done"], r2["node"], r2["origin"], r2["evidence"]) == (False, None, "", "")
    assert (r3["done"], r3["node"]) == (False, None)
    assert got["origins"] == {"prevet:discard": {"confirmed": 1, "refuted": 0}, "prevet:dedupe": {"confirmed": 0, "refuted": 1}}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_unresolved_stale_and_failed_close_never_lose_a_row():
    unresolved = _run_leg(["r1", "r2", "r3"], _MIXED_PREVET, "async (props) => ({})")
    assert all(not r["done"] and r["node"] is None for r in unresolved["rows"].values())
    assert unresolved["queued"] == []
    stale = _run_leg(["r1", "r2", "r3"], _MIXED_PREVET, "async (props) => ({ stale: ['r1'], refuted: [{ row: 'r2', reason: 'x' }] })")
    assert stale["handed"] == [{"id": "r1", "type": "manifest-stale"}]
    assert stale["rows"]["r2"]["node"] is None
    thrown = _run_leg(["r1", "r2", "r3"], _MIXED_PREVET, "async (props) => { throw new Error('boom'); }")
    assert all(not r["done"] and r["node"] is None and r["origin"] == "" for r in thrown["rows"].values())
    assert thrown["queued"] == [] and thrown["handed"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_failed_prevet_call_leaves_every_row_for_triage():
    got = _run_leg(["r1", "r2"], "async (ids) => { throw new Error('down'); }", "async (p) => ({})")
    assert all(not r["done"] and r["node"] is None for r in got["rows"].values())
    assert got["calls"]["close"] == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_one_prevet_call_per_batch_and_a_pm_ruled_row_is_never_prevetted():
    rows = [f"r{i}" for i in range(5)]
    got = _run_leg(rows, "async (ids) => ({ rows: [] })", "async (p) => ({})", batch_size=2, pm={"r1": "ruled"})
    assert got["calls"]["prevet"] == [["r0", "r2"], ["r3", "r4"]]
    assert got["calls"]["close"] == []


# ---------------------------------------------------------------------------
# Calibration: lessons already applied must never come out as discard candidates
# ---------------------------------------------------------------------------

_APPLIED_LESSONS = {
    "an-explicit-pathspec-is-not-peer-safe": {
        "status": "applied",
        "scope": "project",
        "title": "an explicit pathspec does not make a commit peer-safe",
        "body": "Use commit_scoped: it picks the mechanism from observed index/worktree divergence.",
        "how_to_apply": "Never rely on a bare pathspec; call git_native.commit_scoped.",
    },
    "inherited-agent-summaries-are-claims": {
        "status": "resolved",
        "scope": "project",
        "title": "inherited agent summaries are claims",
        "body": "Re-verify a predecessor's summary before building on it.",
        "how_to_apply": "Re-run the cited command before relying on its result.",
    },
    "memo-sent-needs-a-delivery-receipt": {
        "status": "closed",
        "scope": "project",
        "title": "a memo-sent claim needs a delivery receipt",
        "body": "Name the receipt path when reporting a memo sent.",
        "how_to_apply": "Quote the receipt file in the report.",
    },
}


def _applied_fixture(tmp_path: Path) -> dict[str, dict]:
    out = {}
    for name, doc in _APPLIED_LESSONS.items():
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        out[name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def test_the_prompt_contract_keeps_applied_rule_bearing_rows():
    rules = " ".join(gc.PREVET_DECISION_RULES)
    assert "keep unless" in rules
    assert "applied, resolved or closed status" in rules and "pm_ruling" in rules and "any doubt" in rules
    assert "has_rule=false" in rules and "historical detail" in rules


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_applied_lesson_becomes_a_discard_candidate_even_on_a_sloppy_reply(tmp_path):
    rows = _applied_fixture(tmp_path)
    ids = sorted(rows)
    replies = [
        {"row": name, "class": "keep", "has_rule": bool(rows[name].get("how_to_apply")), "witness": ""}
        for name in ids
    ]
    honest = _run_leg(ids, "async (ids) => (" + json.dumps({"rows": replies}) + ")", "async (p) => ({})")
    assert honest["calls"]["close"] == []
    sloppy_cases = [
        {"class": "discard-candidate", "has_rule": True, "witness": "looks old"},
        {"class": "discard-candidate", "witness": "looks old"},
        {"class": "discard-candidate", "has_rule": False, "witness": "  "},
        {"class": "dedupe-candidate", "has_rule": True, "witness": "no path here"},
        {"class": "dedupe-candidate", "has_rule": True},
        {"class": "archive-it", "has_rule": False, "witness": "x/y"},
        {},
    ]
    for case in sloppy_cases:
        reply = {"rows": [{"row": n, **case} for n in ids]}
        got = _run_leg(ids, "async (ids) => (" + json.dumps(reply) + ")", "async (p) => ({})")
        assert got["calls"]["close"] == [], case
        assert all(r["node"] is None and not r["done"] for r in got["rows"].values()), case
