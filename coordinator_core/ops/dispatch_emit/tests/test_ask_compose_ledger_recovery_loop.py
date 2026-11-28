"""The composed ask script's ledger-fix loop: compose-and-grep pins plus node drives with stubbed replies.

The loop sits between the verify leg and the ledger halt: it reopens only the chunks a blocking row names,
caps its attempts, writes `recovery.json`, and withholds the chunks it could not fix."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ledger_recovery import (
    LEDGER_FIX_LABEL,
    LEDGER_REVERIFY_LABEL,
    RECOVERY_ATTEMPT_CAP,
    RECOVERY_WITHHELD_PARAM,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-10-18-landed.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_RESUME = {"run_id": "ask-prior", "plan": _PLAN, "blocking": ["R1"], "em_discharged": {}}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["pkg/a.py", "tests/test_a.py"], "wave": 0, "deps": [], "register_rows": ["R1"]},
    {"id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["pkg/b.py"], "wave": 0, "deps": [], "register_rows": ["R1"]},
]
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": _ROWS, "review_declared_paths": ["pkg/a.py", "pkg/b.py"],
    "test_targets": [], "marker_path": "m", "head_sha": "c" * 40,
}
_MET = {"id": "R1", "status": "met", "wired": True}


def _unmet(remaining, evidence: str = "", rid: str = "R1"):
    return {"id": rid, "status": "not_met", "essential": True, "remaining": remaining, "evidence": evidence}


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
  if (label === 'verify') return cfg.verify;
  const r = cfg.replies[label];
  if (Array.isArray(r)) return r[Math.min(counts[label] - 1, r.length - 1)];
  if (r !== undefined) return r;
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""
)


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, *, items, replies=None, manifest=None, test=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    disk = manifest or _DISK
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": stage_reply(disk),
    }
    verify = {"items": items, **({"test": test} if test else {})}
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": _LANDED, **readers_cfg(),
        "replies": {"gate": gate, "gate-stage": gate, **(replies or {})},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _fix_labels(out: dict) -> list[str]:
    return [lab for lab in _labels(out) if lab and lab.startswith(LEDGER_FIX_LABEL)]


def _commit_prompt(out: dict) -> str:
    return next(c for c in out["calls"] if c["label"] == "commit")["prompt"]


def _record(out: dict) -> dict:
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-recovery-record")["prompt"]
    text = prompt.split("holding exactly this JSON text and nothing else: ", 1)[1].split(". Edit nothing else", 1)[0]
    return json.loads(text)


_FIXED = {"status": "DONE", "paths": ["pkg/a.py"], "reason": "fixed"}
_NAMED = _unmet(["C1: fix pkg/a.py"])


def test_the_loop_composes_once_with_the_cap_rendered_from_the_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    assert script.count("const _ledgerRecover = async") == 1
    assert script.count("await _ledgerRecover()") == 1
    assert f"const _recoveryCap = {RECOVERY_ATTEMPT_CAP};" in script
    assert script.count(f"label: '{LEDGER_FIX_LABEL}' + _cid") == 1
    assert script.count(f"label: '{LEDGER_REVERIFY_LABEL}'") == 1
    assert script.index("const _ledgerRecover = async") < script.index("const _rulable = ")
    assert re.search(r"phases: \[[^\]]*'ledger-fix'", script)


def test_an_xs_arm_composes_no_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_XS)
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    assert "_ledgerRecover" not in script
    assert "'ledger-fix'" not in script.split("phases: [", 1)[1].split("]", 1)[0]


def test_a_named_chunk_is_fixed_and_the_run_commits_with_no_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert _fix_labels(out) == ["ledger-fix:C1"], labels
    assert labels.index("ledger-fix:C1") < labels.index(LEDGER_REVERIFY_LABEL) < labels.index("commit")
    assert out["result"]["halted_by"] is None
    rec = _record(out)
    assert rec["chunks"]["C1"]["status"] == "fixed" and rec["chunks"]["C1"]["attempts"] == 1
    assert "C2" not in rec["chunks"]
    assert RECOVERY_WITHHELD_PARAM not in _commit_prompt(out)


def test_a_fix_that_progresses_but_never_finishes_stops_at_the_cap_and_withholds_the_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    two = _unmet(["C1: part one", "C1: part two"])
    one = _unmet(["C1: part two"])
    out = _run(
        _script(monkeypatch), items=[two],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [one]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"] * RECOVERY_ATTEMPT_CAP
    assert _labels(out).count(LEDGER_REVERIFY_LABEL) == RECOVERY_ATTEMPT_CAP
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "exhausted" and rec["attempts"] == RECOVERY_ATTEMPT_CAP
    res = out["result"]
    assert res["halted_by"] is None
    assert "C1" in res["incomplete"]
    assert f'"{RECOVERY_WITHHELD_PARAM}":["C1"]' in _commit_prompt(out)


def test_a_fix_whose_reverify_returns_the_same_remaining_is_one_attempt_then_no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_NAMED]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"]
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "exhausted" and rec["attempts"] == 1
    assert rec["last_reason"] == "self-correction-no-progress"


def test_a_ledger_halt_after_recovery_carries_the_self_correction_record(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {**_DISK, "rulable_ids": ["R1"]}
    out = _run(
        _script(monkeypatch), items=[_NAMED, _unmet(["no owner"], rid="R2")], manifest=manifest,
        replies={"ledger-fix:C1": {"status": "BLOCKED", "paths": [], "reason": "no"}},
    )
    sc = out["result"]["halted_by"]["self_correction"]
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert sc["phase"] == "ledger" and sc["attempts"] == 1 and sc["status"] == "blocked"


def test_the_fix_leg_is_told_where_the_revise_notes_are(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-fix:C1")["prompt"]
    assert "/notes.jsonl" in prompt


def test_a_blocked_fix_spends_no_more_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = {"status": "BLOCKED", "paths": [], "reason": "needs a path outside the footprint"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": blocked})
    assert _fix_labels(out) == ["ledger-fix:C1"]
    assert LEDGER_REVERIFY_LABEL not in _labels(out)
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked" and rec["attempts"] == 1
    assert f'"{RECOVERY_WITHHELD_PARAM}":["C1"]' in _commit_prompt(out)


_PIN = "pkg/a_pin.json"


def _needs(*paths: str) -> dict:
    return {"status": "BLOCKED", "paths": [], "reason": "needs a path outside the footprint", "needs_paths": list(paths)}


def test_a_fix_blocked_on_a_path_beside_the_row_writes_is_retried_with_it_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    done = {"status": "DONE", "paths": ["pkg/a.py", _PIN], "reason": "pin moved to JSON"}
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": [_needs(_PIN), done], LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert _fix_labels(out) == ["ledger-fix:C1"] * 2 and labels.count(LEDGER_REVERIFY_LABEL) == 1
    first, second = (c["prompt"] for c in out["calls"] if c["label"] == "ledger-fix:C1")
    assert _PIN not in first.split("The ONLY paths you may edit:", 1)[1].split(".", 1)[0]
    assert _PIN in second.split("The ONLY paths you may edit:", 1)[1].split("Reproduce", 1)[0]
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "fixed" and rec["attempts"] == 2 and rec["widened"] == [_PIN]
    assert rec["paths"] == [] and _PIN in rec["footprint"]


@pytest.mark.parametrize("needed", ["other/x.json", "pkg/b.py", "pkg/CLAUDE.md", "state/x.json", "../a_pin.json"])
def test_a_fix_blocked_on_any_other_path_still_spends_no_more_attempts(monkeypatch: pytest.MonkeyPatch, needed: str) -> None:
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": _needs(needed)})
    assert _fix_labels(out) == ["ledger-fix:C1"] and LEDGER_REVERIFY_LABEL not in _labels(out)
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked" and rec["attempts"] == 1 and rec["paths"] == [needed]
    assert "widened" not in rec


def test_a_blocked_fix_that_needs_an_adjacent_path_and_a_distant_one_is_not_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": _needs(_PIN, "other/x.json")})
    assert _fix_labels(out) == ["ledger-fix:C1"]
    assert _record(out)["chunks"]["C1"]["status"] == "blocked"


def test_a_path_outside_the_footprint_blocks_the_chunk_and_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    stray = {"status": "DONE", "paths": ["pkg/a.py", "pkg/_substrate.py"], "reason": "fixed"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": stray})
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked"
    assert rec["paths"] == ["pkg/_substrate.py"]
    assert rec["footprint"] == ["pkg/a.py", "tests/test_a.py"]


_REPORTS = ".coordinator-local/subagent-share/dispatch-reports/plan-slug/"


def test_a_fix_leg_reporting_its_own_dispatch_report_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    own = {"status": "DONE", "paths": ["pkg/a.py", _REPORTS + "C1.md"], "reason": "fixed"}
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": own, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "fixed"
    assert rec["paths"] == []


def test_a_fix_leg_reporting_a_peer_chunks_dispatch_report_is_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = {"status": "DONE", "paths": ["pkg/a.py", _REPORTS + "C2.md"], "reason": "fixed"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": peer})
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked"
    assert rec["paths"] == [_REPORTS + "C2.md"]


def test_a_same_named_file_outside_the_reports_prefix_is_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    stray = {"status": "DONE", "paths": ["pkg/a.py", "elsewhere/C1.md"], "reason": "fixed"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": stray})
    assert _record(out)["chunks"]["C1"]["status"] == "blocked"


def test_an_unattributed_item_still_halts_exactly_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(_script(monkeypatch), items=[_unmet(["something no chunk owns"])])
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert _fix_labels(out) == []
    assert "ledger-recovery-record" not in _labels(out)


def test_a_held_only_item_runs_no_fix_leg_and_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {**_DISK, "gated": [{"id": "C9", "reason": "depends_on_plan"}]}
    out = _run(_script(monkeypatch), items=[_unmet(["C9 waits on another plan"])], manifest=manifest)
    assert _fix_labels(out) == []
    assert _record(out)["chunks"]["C9"]["status"] == "held"
    assert _record(out)["chunks"]["C9"]["attempts"] == 0
    assert out["result"]["halted_by"] is None


def test_a_rulable_row_with_a_remaining_list_naming_a_chunks_file_runs_its_fix_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {**_DISK, "rulable_ids": ["R1"]}
    out = _run(
        _script(monkeypatch), items=[_NAMED], manifest=manifest,
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"]
    assert out["result"]["halted_by"] is None


def test_a_rulable_row_with_empty_remaining_runs_no_fix_leg_and_halts_ledger_check(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {**_DISK, "rulable_ids": ["R1"]}
    out = _run(_script(monkeypatch), items=[_unmet([], "C1 pkg/a.py")], manifest=manifest)
    assert _fix_labels(out) == []
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


def test_a_row_whose_remaining_is_only_foreign_test_ids_runs_no_fix_leg_and_does_not_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = _unmet(["other/test_x.py::test_a", "other/test_y.py::test_b"])
    out = _run(_script(monkeypatch), items=[foreign])
    assert _fix_labels(out) == []
    assert out["result"]["halted_by"] is None


def test_foreign_ids_stay_out_of_the_failing_list_the_fix_leg_gets(monkeypatch: pytest.MonkeyPatch) -> None:
    test = {
        "status": "fail", "summary": "s",
        "baseline": {"caused": ["tests/test_a.py::test_own", "other/test_x.py::test_theirs"],
                     "foreign": ["other/test_x.py::test_theirs"], "pre_existing": ["old/test_p.py::test_old"]},
    }
    out = _run(
        _script(monkeypatch), items=[_unmet(["C1: fix tests/test_a.py::test_own"])], test=test,
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-fix:C1")["prompt"]
    assert "tests/test_a.py::test_own" in prompt
    assert "other/test_x.py" not in prompt.split("Failing output:", 1)[1]
    assert "old/test_p.py" not in prompt


def test_an_owed_blocking_item_runs_no_fix_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    owed = {**_NAMED, "owed_by": ["X1"]}
    manifest = {**_DISK, "owed": [{"id": "X1", "reason": "em", "detail": "d"}]}
    out = _run(_script(monkeypatch), items=[owed], manifest=manifest)
    assert _fix_labels(out) == []


def test_a_test_file_one_chunk_owns_reopens_only_that_chunk_with_it_in_the_footprint(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_unmet(["Fix the 2 failing tests in tests/test_a.py"])],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"]
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-fix:C1")["prompt"]
    assert '["pkg/a.py","tests/test_a.py"]' in prompt
    assert "pkg/b.py" not in prompt


def test_two_chunks_claiming_the_row_are_not_both_reopened_without_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(_script(monkeypatch), items=[_unmet(["still red"])])
    assert _fix_labels(out) == []
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


def _snap(leg, *writes, cid: str = "C1", ok: bool = True, truncated: bool = False, restore=None):
    """The writing leg's reply with the live-tree bracket lines it relays: `before` ok, `after` naming `writes`."""
    label = "remnant-complete" if cid == "remnant" else f"ledger-fix:{cid}"
    extra = {"truncated": True} if truncated else {}
    return {
        label: {
            **leg,
            "snap_before": {"ok": ok, "live_tree_writes": [], **extra},
            "snap_after": {"ok": ok, "live_tree_writes": list(writes), **extra},
            **({"restore": restore} if restore is not None else {}),
        }
    }


def _remnant_record(out: dict) -> dict:
    prompt = next(c for c in out["calls"] if c["label"] == "remnant-record")["prompt"]
    text = prompt.split("exactly this JSON text: ", 1)[1].split(". Edit nothing else", 1)[0]
    return json.loads(text)


def test_a_fix_leg_that_reports_no_change_while_a_live_blob_changed_has_the_path_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    liar = {"status": "DONE", "paths": [], "reason": "live tree unchanged"}
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={LEDGER_REVERIFY_LABEL: {"items": [_MET]}, **_snap(liar, "pkg/a.py")},
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["live_tree_writes"] == ["pkg/a.py"]
    assert rec["undeclared_live_writes"] == ["pkg/a.py"]
    assert "pkg/a.py" in rec["last_reason"] and rec["last_reason"].startswith("live tree changed (engine-measured)")
    assert rec["live_snapshot"].endswith("/live-C1.json")


def test_a_fix_leg_with_unchanged_blobs_lists_no_write_and_keeps_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={LEDGER_REVERIFY_LABEL: {"items": [_MET]}, **_snap(_FIXED)},
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["live_tree_writes"] == [] and "undeclared_live_writes" not in rec
    assert rec["last_reason"] == "fixed"


def test_a_fix_leg_whose_measurement_failed_records_the_write_as_unmeasured_not_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={LEDGER_REVERIFY_LABEL: {"items": [_MET]}, **_snap(_FIXED, ok=False)},
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["live_tree_writes"] is None and rec["live_tree_unmeasured"] is True
    assert rec["last_reason"] == "fixed [live-tree write not measured]"


def test_a_fix_leg_reply_with_no_snap_before_key_is_recorded_as_unmeasured_not_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["live_tree_writes"] is None and rec["live_tree_unmeasured"] is True
    assert "undeclared_live_writes" not in rec
    assert rec["last_reason"] == "fixed [live-tree write not measured]"


def test_each_fix_leg_runs_the_live_tree_bracket_itself_and_no_agent_leg_is_emitted_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={LEDGER_REVERIFY_LABEL: {"items": [_MET]}, **_snap(_FIXED)},
    )
    assert not [lab for lab in _labels(out) if lab and (lab.startswith("live-snap") or lab.startswith("live-restore"))]
    fix_prompt = next(c for c in out["calls"] if c["label"] == "ledger-fix:C1")["prompt"]
    assert "hash-object" not in fix_prompt and "live_before" not in fix_prompt
    before, after, restore = (fix_prompt.index(m) for m in ("ledger_recovery before --root", "ledger_recovery after --root", "live_write_guard"))
    assert before < after < restore and "live-C1.json" in fix_prompt and """--paths-json '["pkg/a.py","tests/test_a.py"]'""" in fix_prompt


def test_the_remnant_leg_carries_the_same_bracket_and_emits_no_live_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    leg = {"status": "DONE", "paths": [], "reason": "completed"}
    out = _run(
        _script(monkeypatch), items=[_unmet(["something no chunk owns"])],
        replies={"remnant-reverify": {"items": [_MET]}, **_snap(leg, cid="remnant")},
    )
    assert not [lab for lab in _labels(out) if lab and lab.startswith("live-")]
    prompt = next(c for c in out["calls"] if c["label"] == "remnant-complete")["prompt"]
    assert "ledger_recovery before --root" in prompt and "live-remnant.json" in prompt and "live_write_guard" in prompt


_ORPHAN = _unmet(["something no chunk owns"])


def test_a_remnant_leg_that_changed_a_footprint_path_it_declared_records_it_and_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    leg = {"status": "DONE", "paths": ["pkg/a.py"], "reason": "completed"}
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={"remnant-reverify": {"items": [_MET]}, **_snap(leg, "pkg/a.py", cid="remnant")},
    )
    rec = _remnant_record(out)
    assert rec["live_tree_writes"] == ["pkg/a.py"] and "undeclared_live_writes" not in rec
    assert rec["status"] == "fixed" and "remnant-reverify" in _labels(out)


def test_a_remnant_leg_whose_report_leaves_out_a_changed_live_path_is_blocked_before_reverify(monkeypatch: pytest.MonkeyPatch) -> None:
    leg = {"status": "DONE", "paths": [], "reason": "live tree unchanged"}
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={"remnant-reverify": {"items": [_MET]}, **_snap(leg, "pkg/a.py", cid="remnant")},
    )
    rec = _remnant_record(out)
    assert rec["status"] == "blocked"
    assert rec["live_tree_writes"] == ["pkg/a.py"] and rec["undeclared_live_writes"] == ["pkg/a.py"]
    assert rec["last_reason"].startswith("remnant wrote live path(s) it did not report")
    assert "remnant-reverify" not in _labels(out)


def test_a_remnant_leg_that_changed_a_path_outside_the_footprint_names_it_for_the_landing_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ledger_recovery

    leg = {"status": "DONE", "paths": ["pkg/other.py"], "reason": "completed"}
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={"remnant-reverify": {"items": [_MET]}, **_snap(leg, "pkg/other.py", cid="remnant")},
    )
    rec = _remnant_record(out)
    assert rec["status"] == "blocked" and rec["paths"] == ["pkg/other.py"]
    assert ledger_recovery.remnant_breach(rec) == ["pkg/other.py"]



def test_a_fix_leg_that_changed_a_path_has_its_relayed_engine_restore_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={
            LEDGER_REVERIFY_LABEL: {"items": [_MET]},
            **_snap(_FIXED, "pkg/a.py", restore={"ok": True, "restored": ["pkg/a.py"], "refused": [], "peer_held": []}),
        },
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["live_restored"] == ["pkg/a.py"] and rec["status"] != "blocked"


def test_a_fix_leg_whose_write_the_engine_could_not_restore_is_blocked_not_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={
            LEDGER_REVERIFY_LABEL: {"items": [_MET]},
            **_snap(_FIXED, "pkg/a.py", restore={"ok": True, "restored": [], "refused": [{"path": "pkg/a.py", "reason": "clash"}]}),
        },
    )
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked" and rec["live_refused"][0]["path"] == "pkg/a.py"
    assert "could not restore" in rec["last_reason"]


def test_a_fix_leg_over_a_truncated_directory_snapshot_is_recorded_as_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={LEDGER_REVERIFY_LABEL: {"items": [_MET]}, **_snap(_FIXED, truncated=True)},
    )
    assert _record(out)["chunks"]["C1"]["live_tree_unmeasured"] is True


def test_a_remnant_write_to_a_peer_held_path_is_not_blamed_as_unreported(monkeypatch: pytest.MonkeyPatch) -> None:
    leg = {"status": "DONE", "paths": [], "reason": "live tree unchanged"}
    out = _run(
        _script(monkeypatch), items=[_ORPHAN],
        replies={
            "remnant-reverify": {"items": [_MET]},
            **_snap(leg, "pkg/a.py", cid="remnant", restore={"ok": True, "restored": [], "refused": [], "peer_held": ["pkg/a.py"]}),
        },
    )
    rec = _remnant_record(out)
    assert "undeclared_live_writes" not in rec and rec["status"] == "fixed"
    assert "remnant-reverify" in _labels(out)
