"""Replay e2e for halt self-correction: the composed ask script under node with recorded agent replies, its
commit params handed to `dispatch.land_run` on a tmp_path git repo.

Shapes: example-game-repo f1d188 (a review wave BLOCKED on an owned failing test), example-game-repo b4dcfa (ledger rows
not_met with concrete `remaining`, every id rulable), claude-klabauter c04f17 (reds outside the run's write set). The
recorded shapes are inline dicts; no live run record is read and no live agent runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, land_run
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.self_correction import (
    FOREIGN_FIELD,
    HALT_FIX_LABEL,
    HALT_REVERIFY_LABEL,
    NO_PROGRESS,
    SELF_CORRECTION_CAP,
    SELF_CORRECTION_FIELD,
)
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_ledger_recovery_loop as loop
from coordinator_core.ops.dispatch_emit.tests import test_ask_run_product_commit as base
from coordinator_core.ops.dispatch_emit.tests import test_ledger_recovery_replays_e2e as led
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

if shutil.which("node") is None:
    pytest.skip("node not on PATH", allow_module_level=True)

_SIZING = "state/sizings/x.yaml"
_PLAN = "docs/plans/2026-10-26-replays.md"
_TOKEN = "0123456789abcdef0123456789abcdef"
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_GATE = {"arm": "m_plus", "halt": None, "baton": None, "tshirt": "M", "route": "spec-dispatch"}
_REVIEW_LABEL = "review:coordinator:code-reviewer"
_REVERIFY_LABEL = "review:coordinator:test-runner"

_A, _A_TEST, _B = "pkg/a.py", "tests/test_a.py", "pkg/b.py"
_OWNED = f"{_A_TEST}::test_x"
_OWNED_Y, _OWNED_Z = f"{_A_TEST}::test_y", f"{_A_TEST}::test_z"
_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": [_A, _A_TEST], "wave": 0, "deps": [], "register_rows": ["R1"]},
    {"id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": [_B], "wave": 0, "deps": [], "register_rows": ["R1"]},
]
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": _ROWS, "review_declared_paths": [_A, _A_TEST, _B],
    "test_targets": [], "marker_path": "m", "head_sha": "c" * 40,
}
_MET = {"id": "R1", "status": "met", "wired": True}
_DONE = {"status": "DONE", "paths": [_A], "reason": "fixed", "needs_paths": []}

_NODE_PROGRAM = (
    WORKFLOW_FAKE_JS
    + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const counts = {};
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  counts[label] = (counts[label] ?? 0) + 1;
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  const r = cfg.replies[label];
  if (Array.isArray(r)) return r[Math.min(counts[label] - 1, r.length - 1)];
  if (r !== undefined) return r;
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', 'log', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow, () => {})
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""
)


def _script(monkeypatch, disk: dict) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    ready = {"planPath": _PLAN, "readyOpReply": stage_reply(disk)}
    blitz = f"  async function planBlitz(args) {{\n    return {{ ready: [{json.dumps(ready)}] }};\n  }}"
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (blitz, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _fresh_run(monkeypatch, replies: dict, disk: dict | None = None) -> dict:
    """A fresh run (plan-blitz ready, rows, review wave, verify, commit) with every leg stubbed by label."""
    disk = disk or _DISK
    script = _script(monkeypatch, disk)
    cfg = {
        "token": _TOKEN, "commit": _LANDED, **readers_cfg(),
        "replies": {
            "gate": _GATE, "gate-stage": _GATE, "verify": {"items": [_MET]},
            **{f"work:{r['id']}": f"DONE: {r['id']}" for r in disk["rows"]}, **replies,
        },
    }
    proc = subprocess.run(
        [shutil.which("node"), "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8",
        timeout=90, env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _count(out: dict, label: str) -> int:
    return _labels(out).count(label)


def _wave(verdict: str, findings: int, applied: int, files: list[str]) -> dict:
    """The reviewer's reply: one prep slice over `files` and its verdict."""
    return {
        "prep": {"slices": [{"id": "s1", "files": files, "diff_path": "s1.diff", "sidecar_path": base.WAVE_SIDECAR,
                             "contract_blocks": 0}], "error": None, "verdict": "ok", "product_files": len(files)},
        "review": {"slice_id": "s1", "verdict": verdict, "findings": findings, "applied": applied,
                   "sidecar_path": base.WAVE_SIDECAR},
    }


def _record(failing: list[str], **kw) -> dict:
    """A review re-verify record: `failing` ids with their one-line messages."""
    rec = {
        "status_before": "fail", "status_after": "fail" if failing else "pass", "edited": [], "failing": failing,
        "failing_detail": [{"node_id": n, "message": f"AssertionError in {n}\nsecond line"} for n in failing],
        "pre_existing": [], "reverted": [], "kept": [], "sidecar_path": base.WAVE_SIDECAR, "unapplied": [],
    }
    rec.update(kw)
    return rec


def _tree(repo, paths: tuple[str, ...]) -> None:
    for rel in paths:
        base._write(repo, rel, "def test_x():\n    assert True\n" if rel.startswith("tests/") else "X = 1\n")
    base._write(repo, "pkg/__init__.py", "")


def _land(repo, out: dict, mutate=None) -> dict:
    """`dispatch.land_run` on the params the script handed the commit leg, run-identity fields aside."""
    params = led._commit_params(out)
    for key in ("run_id", "run_token", "resume_ledger"):
        params.pop(key, None)
    params.update(script_path="run.mjs", session_id=base._SID)
    if mutate is not None:
        mutate(params)
    return land_run._handler(params, repo_root=repo / ".git")


# --- Shape 1: example-game-repo f1d188 -------------------------------------------------------------------------------


def test_f1d188_an_owned_red_fixed_on_attempt_one_runs_no_review_blocked_halt_and_lands_every_chunk(monkeypatch, repo):  # noqa: F811
    blocked = _wave("BLOCKED", 2, 0, [_A, _A_TEST])
    clear = {"test": _record([]), "slices": [{"slice_id": "s1", "findings": [], "applied": True}]}
    out = _fresh_run(monkeypatch, {
        _REVIEW_LABEL: blocked, _REVERIFY_LABEL: _record([_OWNED]),
        HALT_FIX_LABEL: _DONE, HALT_REVERIFY_LABEL: clear,
    })

    labels = _labels(out)
    assert _count(out, HALT_FIX_LABEL) == 1 and _count(out, HALT_REVERIFY_LABEL) == 1, labels
    assert labels.index(HALT_FIX_LABEL) < labels.index(HALT_REVERIFY_LABEL) < labels.index("verify") < labels.index("commit")
    assert "halt-commit" not in labels
    assert out["result"]["halted_by"] is None
    fix = next(c for c in out["calls"] if c["label"] == HALT_FIX_LABEL)["prompt"]
    assert _OWNED in fix and "AssertionError in" in fix and base.WAVE_SIDECAR in fix

    led._register(repo, "R1")
    _tree(repo, (_A, _A_TEST, _B))
    led._request(repo, {"C1": [_A, _A_TEST], "C2": [_B]})
    landed = _land(repo, out)

    assert landed["committed"] is True, landed
    assert led._lands(repo) == {"C1", "C2"}
    assert {_A, _A_TEST, _B} <= base._names(repo, landed["sha"])


def test_f1d188_a_fix_that_swaps_the_red_each_leg_stops_at_the_cap_and_halts_exhausted(monkeypatch):
    blocked = _wave("BLOCKED", 2, 0, [_A, _A_TEST])
    swap = [{"test": _record([_OWNED_Y]), "slices": []}, {"test": _record([_OWNED_Z]), "slices": []}]
    out = _fresh_run(monkeypatch, {
        _REVIEW_LABEL: blocked, _REVERIFY_LABEL: _record([_OWNED]), HALT_FIX_LABEL: _DONE, HALT_REVERIFY_LABEL: swap,
    })

    assert _count(out, HALT_FIX_LABEL) == SELF_CORRECTION_CAP == 2
    halted = out["result"]["halted_by"]
    assert halted["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    sc = halted[SELF_CORRECTION_FIELD]
    assert sc["attempts"] == SELF_CORRECTION_CAP and sc["status"] == "exhausted"
    labels = _labels(out)
    assert "halt-commit" in labels and "commit" not in labels and "verify" not in labels


def test_f1d188_a_fix_leg_that_needs_an_undeclared_path_halts_naming_it_after_one_leg(monkeypatch):
    scope = {"status": "SCOPE", "paths": [], "reason": "the finding names a CLI nobody wrote",
             "needs_paths": ["tools/likeness_edit_delta.py"]}
    out = _fresh_run(monkeypatch, {_REVIEW_LABEL: _wave("BLOCKED", 1, 0, [_A]), HALT_FIX_LABEL: scope})

    assert _count(out, HALT_FIX_LABEL) == 1 and _count(out, HALT_REVERIFY_LABEL) == 0
    halted = out["result"]["halted_by"]
    assert halted[SELF_CORRECTION_FIELD]["status"] == "scope"
    assert "tools/likeness_edit_delta.py" in json.dumps(halted["next_action"])
    assert "add-task" in json.dumps(halted["next_action"])


# --- Shape 2: example-game-repo b4dcfa -------------------------------------------------------------------------------

_C3 = "pkg/c.py"
_LEDGER_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": [_A, _A_TEST], "wave": 0, "deps": [], "register_rows": ["T41"]},
    {"id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": [_B], "wave": 0, "deps": [], "register_rows": ["R2"]},
    {"id": "C3", "brief_path": "b/C3.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": [_C3], "wave": 0, "deps": [], "register_rows": ["R3"]},
]
_LEDGER_IDS = ["T41", "R2", "R3", "R4"]
_LEDGER_DISK = {
    **_DISK, "rows": _LEDGER_ROWS, "review_declared_paths": [_A, _A_TEST, _B, _C3],
    "rulable_ids": _LEDGER_IDS, "ledger_item_ids": _LEDGER_IDS,
}
_T41 = loop._unmet([f"C1: the metric in {_A} does not flag", f"C1: {_A_TEST} is missing a case"], "metric", rid="T41")
_R2 = loop._unmet(["C2: hair_zones in pkg/b.py is unexercised"], "unexercised", rid="R2")
_T41_LEFT = loop._unmet([f"C1: {_A_TEST} is missing a case"], "metric", rid="T41")
_FIX = {"status": "DONE", "paths": [_A], "reason": "fixed"}
_FIX_B = {"status": "DONE", "paths": [_B], "reason": "fixed"}


def _row_met(rid: str) -> dict:
    return {"id": rid, "status": "met", "wired": True}


def _ledger_run(monkeypatch, replies: dict) -> dict:
    return loop._run(loop._script(monkeypatch), items=[_T41, _R2], replies=replies, manifest=_LEDGER_DISK)


def _register(repo, row_ids: tuple[str, ...]) -> None:
    """Rebind the run's sizing to a register holding the rows the replayed ledger names."""
    head = [
        "schema: sizing-object", "intent: Test intent, verbatim.", "estimate:", "  tshirt: S", "  provisional: true",
        "route: spec-dispatch", "detents: []", "fork: null", "xl_exit: null", "status: routed",
        "premise:", "  provenance: read", "  evidence: fixture",
        "requirement_register:", "  sources:", "  - doc: " + base.SIZING, "    ref: ask", "  rows:",
    ]
    rows = [
        line
        for rid in row_ids
        for line in (
            f"  - id: {rid}", "    source_text: replayed row", "    anchor: relayed", "    surface: internal",
            "    status: open", "    claimed_by: []",
        )
    ]
    base._write(repo, base.SIZING, "\n".join([*head, *rows]) + "\n")


def _ledger_land(repo, out: dict) -> dict:
    """Land a ledger-resume run: the prior wave's review verdict rides the params, as a resumed run reuses it."""
    _register(repo, ("T41", "R2"))
    _tree(repo, (_A, _A_TEST, _B, _C3))
    led._request(repo, {"C1": [_A, _A_TEST], "C2": [_B], "C3": [_C3]})
    led._write_record(repo, out)
    return _land(repo, out, lambda params: params.update(inline_review=base._REVIEWED))


def test_b4dcfa_rulable_rows_with_concrete_remaining_run_their_fix_legs_and_land_every_chunk(monkeypatch, repo):  # noqa: F811
    out = _ledger_run(monkeypatch, {
        "ledger-fix:C1": _FIX, "ledger-fix:C2": _FIX_B,
        lr.LEDGER_REVERIFY_LABEL: {"items": [_row_met("T41"), _row_met("R2")]},
    })

    assert sorted(loop._fix_labels(out)) == ["ledger-fix:C1", "ledger-fix:C2"]
    assert out["result"]["halted_by"] is None
    chunks = loop._record(out)["chunks"]
    assert {cid: chunks[cid]["status"] for cid in ("C1", "C2")} == {"C1": "fixed", "C2": "fixed"}

    landed = _ledger_land(repo, out)

    assert landed["committed"] is True, landed
    assert led._lands(repo) == {"C1", "C2", "C3"}
    assert "Requirement-Open" not in led._message(repo)


def test_b4dcfa_a_repeated_remaining_stops_after_one_attempt_and_the_run_does_not_halt(monkeypatch):
    out = _ledger_run(monkeypatch, {
        "ledger-fix:C1": _FIX, "ledger-fix:C2": _FIX_B,
        lr.LEDGER_REVERIFY_LABEL: {"items": [_T41, _row_met("R2")]},
    })

    assert loop._fix_labels(out).count("ledger-fix:C1") == 1
    c1 = loop._record(out)["chunks"]["C1"]
    assert c1["status"] == "exhausted" and c1["attempts"] == 1 and c1["last_reason"] == NO_PROGRESS
    assert loop._record(out)["chunks"]["C2"]["status"] == "fixed"
    assert out["result"]["halted_by"] is None
    assert "C1" in out["result"]["incomplete"] and "C2" not in out["result"]["incomplete"]
    assert f'"{lr.RECOVERY_WITHHELD_PARAM}":["C1"]' in loop._commit_prompt(out)


def test_b4dcfa_a_repeated_remaining_lands_the_other_chunks_and_reports_the_row_open_with_its_reason(monkeypatch, repo):  # noqa: F811
    out = _ledger_run(monkeypatch, {
        "ledger-fix:C1": _FIX, "ledger-fix:C2": _FIX_B,
        lr.LEDGER_REVERIFY_LABEL: {"items": [_T41, _row_met("R2")]},
    })

    landed = _ledger_land(repo, out)

    assert landed["committed"] is True, landed
    assert led._lands(repo) == {"C2", "C3"}
    assert "Requirement-Open: T41" in led._message(repo) and NO_PROGRESS in led._message(repo)
    assert landed["recovery"]["open_rows"] == ["T41"]
    names = base._names(repo, landed["sha"])
    assert {_B, _C3} <= names and not names & {_A, _A_TEST}


def test_b4dcfa_a_row_with_progress_but_work_left_gets_both_attempts(monkeypatch):
    out = _ledger_run(monkeypatch, {
        "ledger-fix:C1": _FIX, "ledger-fix:C2": _FIX_B,
        lr.LEDGER_REVERIFY_LABEL: {"items": [_T41_LEFT, _row_met("R2")]},
    })

    assert loop._fix_labels(out).count("ledger-fix:C1") == SELF_CORRECTION_CAP
    assert loop._record(out)["chunks"]["C1"]["status"] == "exhausted"


# --- Shape 3: claude-klabauter c04f17 ---------------------------------------------------------------------------------

_SLOT, _SLOT_TEST = "pkg/slot.py", "tests/test_slot.py"
_C04_DISK = {
    **_DISK, "rows": [{**_ROWS[0], "writes": [_SLOT, _SLOT_TEST]}], "review_declared_paths": [_SLOT, _SLOT_TEST],
}
_PEER_TEST = "peer/tests/test_peer.py"
_FOREIGN = [f"{_PEER_TEST}::test_a", f"{_PEER_TEST}::test_b", "other/tests/test_other.py::test_c"]


def _c04_run(monkeypatch) -> dict:
    return _fresh_run(monkeypatch, {
        _REVIEW_LABEL: _wave("CLEAN", 0, 0, [_SLOT, _SLOT_TEST]), _REVERIFY_LABEL: _record(list(_FOREIGN)),
    }, _C04_DISK)


def _c04_tree(repo) -> None:
    """The run's two files plus a peer's red test that imports the landing module."""
    led._register(repo, "R1")
    _tree(repo, (_SLOT, _SLOT_TEST))
    base._write(repo, _PEER_TEST, "\n".join(["from pkg import slot", "", "", "def test_a():", "    assert slot", ""]))
    led._request(repo, {"C1": [_SLOT, _SLOT_TEST]})


def test_c04f17_foreign_reds_run_no_fix_leg_no_halt_and_are_recorded_on_the_reverify_record(monkeypatch):
    out = _c04_run(monkeypatch)

    labels = _labels(out)
    assert HALT_FIX_LABEL not in labels and HALT_REVERIFY_LABEL not in labels, labels
    assert "halt-commit" not in labels and labels.index("verify") < labels.index("commit")
    assert out["result"]["halted_by"] is None
    reverify = out["result"]["review"]["reverify"]
    assert sorted(reverify[FOREIGN_FIELD]) == sorted(_FOREIGN)
    assert reverify["failing"] == _FOREIGN


def test_c04f17_land_run_lands_both_files(monkeypatch, repo):  # noqa: F811
    out = _c04_run(monkeypatch)
    _c04_tree(repo)

    landed = _land(repo, out)

    assert landed["committed"] is True, landed
    assert led._lands(repo) == {"C1"}
    assert {_SLOT, _SLOT_TEST} <= base._names(repo, landed["sha"])


def test_c04f17_the_landing_gate_does_not_refuse_the_commit_params_the_script_hands_it(monkeypatch, repo):  # noqa: F811
    out = _c04_run(monkeypatch)
    _c04_tree(repo)

    found = li.reverify_red_findings(repo, led._commit_params(out), [_SLOT, _SLOT_TEST])

    assert found == []


def test_c04f17_the_landing_gate_does_not_refuse_reds_the_record_lists_as_foreign(monkeypatch, repo):  # noqa: F811
    out = _c04_run(monkeypatch)
    _c04_tree(repo)
    params = led._commit_params(out)
    params["inline_review"]["reverify"][FOREIGN_FIELD] = out["result"]["review"]["reverify"][FOREIGN_FIELD]

    assert li.reverify_red_findings(repo, params, [_SLOT, _SLOT_TEST]) == []


def test_c04f17_the_same_reds_not_listed_as_foreign_still_refuse_the_landing(monkeypatch, repo):  # noqa: F811
    out = _c04_run(monkeypatch)
    _c04_tree(repo)
    params = led._commit_params(out)
    params["inline_review"]["reverify"].pop(FOREIGN_FIELD, None)

    found = li.reverify_red_findings(repo, params, [_SLOT, _SLOT_TEST])

    assert [f.path for f in found] == [_PEER_TEST] and found[0].reason == li.REVIEW_REVERIFY_FAILED
