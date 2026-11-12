"""workflow.revise accept_partial: a recorded PARTIAL row stops holding its dependents on the refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import ask_contract as ac
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_accept_partial
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

ENTRY = {"chunk": "X", "reason": "soft byte target missed, measured total is enough"}

_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  const _gateOwed = {}, _chunkReason = {};
  let _halted = null;
  const _rowPlan = {};
  const _planHeld = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  const calls = [];
  const run = (id, reply) => async () => { calls.push(id); return reply ?? ('DONE: ' + id); };
  const _rows = {};
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ calls, heldBy: _heldBy, reason: _heldDependentsReason() }));
})();
"""

_FAN = (
    "_rows.X = _runRow('X', [], null, run('X', %s));"
    "_rows.W = _runRow('W', [], null, run('W', %s));"
    "_rows.Y = _runRow('Y', [_rows.X], null, run('Y'));"
    "_rows.Z = _runRow('Z', [_rows.W], null, run('Z'));"
)


def _go(accepted, x_reply, w_reply="PARTIAL: other"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    helper = emit._run_row_helper_js(accepted_seed=accepted)
    rows = _FAN % (json.dumps(x_reply), json.dumps(w_reply))
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": helper, "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_revise_records_the_entry_and_replies_it(repo):
    _bound(repo, "run-one", ask="first ask")
    before = _rows(repo)
    reply = rl._handler({"run_id": "run-one", "accept_partial": [ENTRY]}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None and reply["accept_partial"] == [ENTRY]
    assert run_accept_partial.read([rl.run_dir(repo, "run-one")]) == {"X": ENTRY["reason"]}
    assert _rows(repo) == before


@pytest.mark.parametrize("bad", [
    [{"chunk": "", "reason": "r"}],
    [{"chunk": "X", "reason": "  "}],
    [{"chunk": "bad id", "reason": "r"}],
    ["X"],
])
def test_a_bad_entry_is_refused_and_writes_nothing(repo, bad):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "accept_partial": bad}, repo_root=repo)
    assert "error" in reply
    assert not (rl.run_dir(repo, "run-one") / run_accept_partial.ACCEPT_PARTIAL_FILE).exists()


def test_accept_partial_needs_an_emitted_run(repo):
    reply = rl._handler({"run_id": "never-started", "accept_partial": [ENTRY]}, repo_root=repo)
    assert "error" in reply


def test_refired_emit_seeds_the_accepted_chunk_and_releases_its_dependent(repo):
    _bound(repo, "run-one", ask="first ask")
    rl.revise(repo, "run-one", accept_partial=[ENTRY])
    seed = emit._accepted_seed(None, repo, "run-one")
    assert seed == {"X": ENTRY["reason"]}
    out = _go(seed, "PARTIAL: half done")
    assert "Y" in out["calls"] and "Y" not in out["heldBy"]


PLAN_TEXT = "---\ntitle: t\nsizing_object: " + "state/sizings/2026-10-01-reuse.yaml" + "\n---\n# plan\n"
HALTED = "warp-2026-10-01-reuse-20261112T000000-aaaaaa"


def _halted_run(repo, run_id, resumed_from=None):
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

    rl.start(repo, run_id)
    rl.bind(repo, run_id, "state/sizings/2026-10-01-reuse.yaml", ask_text="the ask")
    write_halt_marker(record_dir(repo, run_id), halted="dependents_unstarted", sizing_rel="state/sizings/2026-10-01-reuse.yaml")
    return run_id


def test_the_seed_reads_the_halted_run_the_plan_sizing_resumes(repo):
    _halted_run(repo, HALTED)
    rl.revise(repo, HALTED, accept_partial=[ENTRY])
    assert emit._accepted_seed(None, repo, None) == {}
    assert emit._accepted_seed(None, repo, None, plan_text=PLAN_TEXT) == {"X": ENTRY["reason"]}


def test_the_seed_follows_the_resumed_from_chain_nearest_wins(repo):
    older = "warp-2026-10-01-reuse-20261111T000000-bbbbbb"
    _halted_run(repo, older)
    rl.revise(repo, older, accept_partial=[ENTRY, {"chunk": "W", "reason": "old reason"}])
    _halted_run(repo, HALTED)
    from coordinator_core.ops.dispatch_emit import requirements_ledger as _rl

    run = _rl.read_run(_rl.run_dir(repo, HALTED))
    run[_rl.RESUMED_FROM_FIELD] = older
    (_rl.run_dir(repo, HALTED) / "run.json").write_text(json.dumps(run), encoding="utf-8")
    rl.revise(repo, HALTED, accept_partial=[{"chunk": "W", "reason": "new reason"}])
    assert emit._accepted_seed(None, repo, None, plan_text=PLAN_TEXT) == {"X": ENTRY["reason"], "W": "new reason"}


def test_a_plan_without_a_sizing_or_a_halted_run_seeds_nothing(repo):
    assert emit._accepted_seed(None, repo, None, plan_text=PLAN_TEXT) == {}
    assert emit._accepted_seed(None, repo, None, plan_text="# no frontmatter") == {}


def test_a_partial_not_named_still_holds_its_dependent(repo):
    _bound(repo, "run-one", ask="first ask")
    rl.revise(repo, "run-one", accept_partial=[ENTRY])
    out = _go(emit._accepted_seed(None, repo, "run-one"), "PARTIAL: half done")
    assert "Z" not in out["calls"] and out["heldBy"] == {"Z": "W"}
    assert out["reason"].startswith("dependents unstarted: Z (held by W)")


def test_without_a_record_the_partial_holds():
    out = _go(emit._accepted_seed(None, None, None), "PARTIAL: half done")
    assert "Y" not in out["calls"] and out["heldBy"]["Y"] == "X"


@pytest.mark.parametrize("reply", ["BLOCKED: cannot start", "no status line at all"])
def test_acceptance_never_releases_a_blocked_or_failed_row(reply):
    out = _go({"X": "r"}, reply)
    assert "Y" not in out["calls"] and out["heldBy"]["Y"] == "X"


def test_the_halt_detail_names_the_accept_partial_form():
    env = ac.dependents_unstarted_refire_next_action(
        "dependents unstarted: Y (held by X)", run_id="ask-1", sizing_rel="state/sizings/s.yaml"
    )
    assert env["detail"].startswith("dependents unstarted: Y (held by X)")
    assert "workflow.revise run_id=ask-1 accept_partial=" in env["detail"]


def test_the_shipped_held_halt_names_the_accept_partial_form_and_keeps_the_reason():
    from coordinator_core.ops.dispatch_emit import halt_refire as hr

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    refire = hr.refire_next_action_js(json.dumps("fire"), "_halted.next_action", "_halted.halted", "'why'")
    script = (
        "const _runId = 'ask-9', _sizingRel = 's.yaml', _needsDownstream = {};\n"
        "let _halted = 'dependents unstarted: Z (held by W)', _haltedByHold = true;\n"
        + emit._held_halt_record_js() + "\n" + refire + "\n_refireNextAction();\n"
        "console.log(JSON.stringify({ h: _halted, text: String(_halted) }));"
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.splitlines()[-1])
    assert out["h"]["halted"] == ac.HALT_DEPENDENTS_UNSTARTED and out["h"]["reason"] == out["text"]
    assert out["h"]["next_action"]["detail"].startswith("dependents unstarted: Z (held by W)")
    assert ac.ACCEPT_PARTIAL_FORM.format(run_id="ask-9") in out["h"]["next_action"]["detail"]
    # a non-hold string halt is left a bare string
    bare = subprocess.run(
        [node, "-e", "const _runId='r'; let _halted='stop rule', _haltedByHold=false;\n" + emit._held_halt_record_js()
         + "\nconsole.log(typeof _halted);"],
        capture_output=True, text=True, timeout=60, **no_console_creationflags(),
    )
    assert bare.stdout.strip() == "string"


def test_partial_status_re_is_narrower_than_the_non_done_re():
    assert emit._PARTIAL_STATUS_JS_RE != emit._NON_DONE_STATUS_JS_RE
    assert "BLOCKED" not in emit._PARTIAL_STATUS_JS_RE


def test_compose_script_seeds_the_accepted_chunk_from_the_run_record(repo):
    _bound(repo, "run-one", ask="first ask")
    rl.revise(repo, "run-one", accept_partial=[ENTRY])
    row = WaveRow(id="X", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[], body="")
    script = emit.compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md",
        repo_root=repo, widen_run_dir=rl.run_dir(repo, "run-one"), **REVIEW_KW
    )
    assert f"const _acceptPartial = {json.dumps({'X': ENTRY['reason']})};" in script
    assert "const _DEP_PARTIAL_RE = " + emit._PARTIAL_STATUS_JS_RE + ";" in script
