"""A resumed ask whose review phase re-runs reviews only the halt's blocked slices; a fresh run reviews all."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import (
    _RESUME_REVIEW_SLICE_FILTER_JS,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _ratio_kw
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_THREE = [
    {"slice_id": "s1", "verdict": "CLEAN", "findings": 0, "applied": 0},
    {"slice_id": "s2", "verdict": "BLOCKED", "findings": 3, "applied": 1},
    {"slice_id": "s3", "verdict": "BLOCKED", "findings": 2, "applied": 2},
]


def _compose():
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **_ratio_kw(),
    )


def _filter(phase_verdicts, *, reused=False):
    """Evaluate the emitted filter expression under node against a gate's resume_phases verdicts."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = (
        f"const _reviewReused = {json.dumps(reused)}; "
        f"const _phaseVerdict = (n) => ({json.dumps(phase_verdicts)})[n] ?? null; "
        f"console.log(JSON.stringify({_RESUME_REVIEW_SLICE_FILTER_JS}));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def test_the_composed_wave_hands_the_filter_to_every_slice_reviewer_and_caps_the_count():
    script = _compose()
    assert _RESUME_REVIEW_SLICE_FILTER_JS in script
    assert "review_slice_ids: " in script
    assert "Review only the listed slice ids of your group" in script
    assert "? " + _RESUME_REVIEW_SLICE_FILTER_JS + ".length : Infinity" in script


def test_resumed_run_with_one_blocked_slice_filters_to_that_slice():
    assert _filter({"review": {"wave": _THREE}}) == ["s2"]


def test_a_merged_group_id_splits_into_its_member_slices():
    wave = [{"slice_id": "s4+s5", "verdict": "BLOCKED", "findings": 2, "applied": 0}]
    assert _filter({"review": {"wave": wave}}) == ["s4", "s5"]


def test_fresh_run_filters_nothing_so_every_prep_slice_is_reviewed():
    assert _filter({}) is None


def test_a_reused_review_or_an_all_clean_prior_wave_filters_nothing():
    assert _filter({"review": {"wave": _THREE}}, reused=True) is None
    assert _filter({"review": {"wave": [_THREE[0], _THREE[2]]}}) is None


_WAVE_START = "  const _reviewOuterAgent = agent;"
_WAVE_END = "  if (_reviewSeed.length && Array.isArray(_reviewWave))"

# Plays the review-wave agent: partitions the three prep slices by group_index/group_count the way
# the composed out-of-group check expects, then honours the prompt's review_slice_ids line.
_WAVE_HARNESS = """
const _PREP_IDS = ['s1', 's2', 's3'];
const _calls = [];
const log = () => {};
const phase = () => {};
const parallel = (thunks) => Promise.all(thunks.map((t) => t()));
const _agentDefsClause = () => '';
let _halted = null, _xsTail = false, _noReviewPaths = false, _waveNotRun = [], _reviewPrep = null, _reviewWave = null;
let _deliveryVerdict = null, _deliveryNotRun = null, _reviewIntegration = null, _integrationNotRun = false, _reviewReverify = null;
let _footprintReviewed = false;
const _prepLeg = '', _codeReviewOwed = false;
const _planRel = 'p.md', _sizingRel = 's.yaml', _SESSION_ID = 'sid', _deliveryNotes = null, _reviewPaths = ['a.py'];
const _manifest = { rows: Array.from({ length: 9 }, (_, i) => ({ id: 'R' + i })) };
const _gate = %(gate)s;
const _reusePhase = (n) => !_halted && Array.isArray(_gate.resume_phases?.reuse) && _gate.resume_phases.reuse.includes(n);
const _phaseVerdict = (n) => _gate.resume_phases?.verdicts?.[n] ?? null;
%(consts)s
const agent = async (prompt, opts) => {
  if (opts.label !== 'review:coordinator:code-reviewer') return null;
  const i = Number(/group_index: (\\d+)/.exec(prompt)[1]), n = Number(/group_count: (\\d+)/.exec(prompt)[1]);
  const only = /review_slice_ids: (\\[[^\\n]*\\])/.exec(prompt);
  const g = Math.max(1, Math.min(_PREP_IDS.length, n));
  let at = 0; for (let k = 0; k < i && k < g; k++) at += Math.floor(_PREP_IDS.length / g) + (k < _PREP_IDS.length %% g ? 1 : 0);
  const size = i < g ? Math.floor(_PREP_IDS.length / g) + (i < _PREP_IDS.length %% g ? 1 : 0) : 0;
  let ids = _PREP_IDS.slice(at, at + size);
  if (only) ids = ids.filter((id) => JSON.parse(only[1]).includes(id));
  _calls.push({ i, n, ids, only: only ? JSON.parse(only[1]) : null });
  return {
    prep: { slices: ids.map((id) => ({ id, files: ['a.py'], diff_path: id + '.diff', sidecar_path: id + '.md', contract_blocks: 0 })), error: null, verdict: 'ok', product_files: 1 },
    review: { slice_id: ids.join('+'), verdict: 'CLEAN', findings: 0, applied: 0, sidecar_path: ids.join('+') + '.md' },
  };
};
(async () => {
%(region)s
console.log(JSON.stringify({ calls: _calls, wave: _reviewWave.map((w) => w.slice_id), halted: _halted && _halted.halted }));
})();
"""


def _run_wave(gate: dict) -> dict:
    """Run the composed review-wave region under node with a stub reviewer agent; return what it did."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    consts = "\n".join(re.findall(r"^\s*const _PF\d+ = .*;$", script, flags=re.M))
    start = script.index(_WAVE_START)
    end_at = script.index(_WAVE_END)
    region = script[start: script.index("\n", end_at)]
    program = _WAVE_HARNESS % {"gate": json.dumps(gate), "consts": consts, "region": region}
    out = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _resumed_gate():
    return {
        "arm": "s",
        "resume_phases": {"reuse": ["size"], "rerun": ["review"], "verdicts": {"review": {"wave": _THREE}}},
    }


def test_resumed_wave_spawns_one_reviewer_told_to_review_only_the_blocked_slice():
    got = _run_wave(_resumed_gate())
    assert got["halted"] is None
    assert got["calls"] == [{"i": 0, "n": 1, "ids": ["s2"], "only": ["s2"]}]
    assert sorted(got["wave"]) == ["s1", "s2", "s3"]


def test_fresh_wave_spawns_a_reviewer_per_group_and_reviews_every_slice():
    got = _run_wave({"arm": "s"})
    assert got["halted"] is None
    assert [c["only"] for c in got["calls"]] == [None, None, None]
    assert sorted(i for c in got["calls"] for i in c["ids"]) == ["s1", "s2", "s3"]
    assert sorted(got["wave"]) == ["s1", "s2", "s3"]
