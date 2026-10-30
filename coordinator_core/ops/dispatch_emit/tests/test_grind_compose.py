"""
coordinator_core.ops.dispatch_emit.tests.test_grind_compose

Purpose: pins C7's composer -- ``grind_compose.py`` -- against every named
assertion in docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md's C7 row
(structural checks over the rendered `.mjs`, PLUS the behavioural call-order
test: the pure-Python admission model, driven directly, is the observation
that tells a delivered composer from one that is merely
"wrong-but-deterministic"). One file, per overengineering-reviewer #9.

Golden: the fixture profile plus a 7-row fixture queue emit the committed
`.mjs` byte-for-byte, and a second emit is identical.

This rewrite replaces every assertion that enshrined the first attempt's
defects (an emit-time verdict-bucket hash, hard-coded `_fixOutcome`/
`_verifyOutcome`, dead `batchId === ...` comparisons, an assumed `lock`
runtime global, missing `_recordCall` sites) with assertions over LIVE,
per-row routing driven through the profile's own graph.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pytest
import yaml

from coordinator_core.contract import grind_vocab as vocab
from coordinator_core.ops import _workflow_contract as wc
from coordinator_core.ops.dispatch_emit import grind_commit_contract
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

from .conftest import REVIEW_KW

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "grind-fixture.golden.mjs"


def _fixture_manifest() -> Manifest:
    entries = tuple(
        ManifestEntry(
            row_id=f"row{i}",
            path=f"state/bug-backlog/row{i}.yaml",
            digest=f"{i:064x}",
            batch_key="P0",
        )
        for i in range(7)
    )
    return Manifest(entries=entries, batch_sizes={"P0": 4}, source=None, digest="deadbeef")


def _fixture_profile():
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    gp.validate_graph(profile)
    return profile


def _compose(**overrides):
    profile = overrides.pop("profile", None) or _fixture_profile()
    appetite = overrides.pop("appetite", "standard")
    stage_models = overrides.pop("stage_models", None)
    targeted_runner = overrides.pop("targeted_runner", False)
    manifest = overrides.pop("manifest", None) or _fixture_manifest()
    knobs = gp.resolve_appetite(profile, appetite)
    knobs.update(overrides)
    return gc.compose_grind_script(
        manifest,
        profile,
        knobs,
        run_dir=Path("state/queue-grind/fixture/run-1"),
        appetite=appetite,
        agent_type_host=None,
        queue_dirs=["state/bug-backlog"],
        stage_models=stage_models,
        targeted_runner=targeted_runner,
        **REVIEW_KW,
    )


# ---------------------------------------------------------------------------
# Golden + determinism
# ---------------------------------------------------------------------------


def test_golden_byte_identical():
    script = _compose()
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    assert script == golden


def test_review_wave_sizes_reviewers_by_committed_row_count():
    import copy

    fragment = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    wave["agents"][0]["per"] = "slice"
    wave["agents"][0]["executor_rows_per_reviewer"] = {"min": 3, "max": 4}
    profile = _fixture_profile()
    script = gc.compose_grind_script(
        _fixture_manifest(),
        profile,
        gp.resolve_appetite(profile, "standard"),
        run_dir=Path("state/queue-grind/fixture/run-1"),
        appetite="standard",
        agent_type_host=None,
        queue_dirs=["state/bug-backlog"],
        stage_models=None,
        targeted_runner=False,
        **{**REVIEW_KW, "review_roster_fragment": fragment},
    )
    assert "_settled.filter((s) => s.outcome === 'committed').length" in script
    assert "_groupSlices(" in script


def test_commit_stage_archive_path_is_computed_not_relayed_from_close_agent():
    script = _compose()
    profile = _fixture_profile()
    assert f"const ARCHIVE_PATH = {gc._js_string_literal(profile.archive_path)};" in script
    assert gc.ARCHIVE_DESTINATION_JS in script
    assert "const _archived = _archiveDestination(row.path);" in script
    assert "row.touchedFiles.concat([_archived])" in script
    assert "item.new_path" not in script


def test_fix_stage_archive_path_is_computed_not_relayed_from_fix_agent():
    script = _compose()
    assert "result.close_result.new" not in script
    assert "result.close_result.old" not in script
    assert "row.closeResult = result.close_result;" not in script
    assert "row.closeResult = { old: row.path, new: _archived };" in script


def test_every_phase_title_is_declared_in_meta_phases():
    script = _compose()
    m = re.search(r"export const meta = \{.*?\n\};", script, re.S)
    findings = wc.check_phase_mismatch(script, m.group(0) if m else None)
    assert [f.code for f in findings] == []


def test_reemit_is_byte_identical():
    a = _compose()
    b = _compose()
    assert a == b


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def grind_only(script: str) -> str:
    """The script with the composed review wave cut out: review agents carry
    roster models and are not counted stage calls."""
    start = script.index("phase('Review prep')")
    end = script.index("phase('Review fixes')")
    return script[:start] + script[end:]


def _stage_model_option_count(script: str) -> int:
    names = "|".join(re.escape(n) for n in sorted(set(gc.stages.STAGE_MODELS.values())))
    return len(re.findall(rf"model: '(?:{names})'", script))


def test_every_agent_call_site_carries_a_stage_model():
    script = grind_only(_compose())
    calls = len(re.findall(r"\bagent\(", script))
    assert calls > 0
    assert calls == _stage_model_option_count(script)


def test_stage_models_override_changes_one_call_and_records_it():
    base = grind_only(_compose())
    script = grind_only(_compose(stage_models={"triage": "opus"}))
    assert script.count("model: 'opus'") == base.count("model: 'opus'") + 1
    assert "stage_models: STAGE_MODELS_USED" in script
    assert 'const STAGE_MODELS_USED = {"triage": "opus"};' in script
    assert "STAGE_MODELS_USED" not in base
    assert _compose(stage_models={}) == _compose()


def test_no_banned_tokens_and_zero_error_findings():
    script = _compose()
    for token in ("git mv", "git stash", "add -A", "Date.now", "Math.random", "new Date()"):
        assert token not in script
    findings = wc.run_checks(script)
    errors = [f for f in findings if f.severity.value == "ERROR"]
    assert errors == []


_STMT_ASSIGN = re.compile(r"(?:^|[;{}])[ \t]*([A-Za-z_$][\w$]*)[ \t]*=(?![=>])", re.M)
_DECL = re.compile(r"\b(?:let|const|var|function|class)\s+([A-Za-z_$][\w$]*)")
_DECL_LIST = re.compile(r"\b(?:let|const|var)\s+([^;=]*?=[^;]*?);")


_PARAMS = re.compile(r"\bfunction\b[^(\n]*\(([^)]*)\)|\(([^()]*)\)\s*=>")


def _undeclared_assignments(script: str) -> set[str]:
    """Identifiers assigned bare at statement start with no let/const/var/function/class decl."""
    declared = set(_DECL.findall(script))
    for m in _DECL_LIST.finditer(script):
        declared.update(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*=", m.group(1), re.M))
    for m in _PARAMS.finditer(script):
        declared.update(re.findall(r"[A-Za-z_$][\w$]*", m.group(1) or m.group(2) or ""))
    assigned = set(_STMT_ASSIGN.findall(script))
    return assigned - declared


def test_every_bare_assigned_identifier_is_declared_in_the_emitted_script():
    script = _compose()
    assert "let _waveNotRun = [];" in script
    assert _undeclared_assignments(script) == set()


def test_undeclared_assignment_guard_fails_on_the_prefix_golden():
    pre_fix = _GOLDEN_PATH.read_text(encoding="utf-8").replace("let _waveNotRun = [];\n", "")
    assert _undeclared_assignments(pre_fix) == {"_waveNotRun"}


def test_no_lock_global_only_the_inscript_mutex_object():
    """The first attempt called `lock.acquire`/`lock.release` -- no such
    runtime global exists. This module defines its own mutex object
    in-script (`_locked`/`_waiters`/`_acquire`/`_release`); every `lock.`
    reference in the golden must be to a local identifier this script
    itself declares, never a bare `lock.acquire(`/`lock.release(` call."""
    script = _compose()
    assert "lock.acquire(" not in script
    assert "lock.release(" not in script
    assert "const _locked = new Set();" in script
    assert "function withLock(keys, fn)" in script


def test_lock_invariant_acquire_all_or_nothing_release_precedes_reacquire():
    script = _compose()
    assert "await _acquire(_keys);" in script
    assert "_release(_keys);" in script
    acquire_idx = script.index("await _acquire(_keys);")
    release_idx = script.index("_release(_keys);")
    assert acquire_idx < release_idx
    # all-or-nothing: _tryAcquire refuses unless every key is free
    assert "if (keys.some((k) => _locked.has(k))) return false;" in script


def test_no_per_row_commit_remains_and_the_mutex_key_is_gone():
    script = _compose()
    assert "@commit" not in script
    assert "_commitCall" not in script
    assert script.count("coordinator-invoke dispatch.land_run") == 1
    assert "lockKeys = ['@commit'].concat(" not in script


def test_the_terminal_commit_is_one_bare_invoke_segment_with_the_params_block():
    script = _compose()
    assert script.count("coordinator-invoke dispatch.land_run --params-file ' + (_paramsFile) + '") == 1
    assert "_terminalCommitParamsBlock(params)" in script
    assert "const RUN_TOKEN = args.run_token || null;" in script
    assert gc.PARAMS_BLOCK_JS in script
    assert "agentType: 'coordinator:git-commit-agent'" in script
    assert "/scratch/warp/" in script and "terminal-commit-params.json" in script


def test_the_run_close_is_the_outer_finally_after_review_and_adjudication():
    script = _compose()
    inner = (
        "try { await runGrind(); } catch (e) { _grindErr = e; throw e; } "
        "finally { await _terminalCommit(); await _undoUnfinished(); }"
    )
    outer_open = "try {\n" + inner
    outer_close = "} catch (e) { _grindErr = e; throw e; } finally { await _runClose(); }"
    adjudicated = inner + "\nawait _adjudicatePmBound();\nawait _continueRuled();"
    assert outer_open in script and outer_close in script and adjudicated in script
    assert script.count("await _terminalCommit()") == 2 and script.count("await _runClose()") == 1
    assert script.index(outer_open) < script.index(adjudicated) < script.index("const _reviewPaths")
    assert script.index("async function _adjudicatePmBound()") < script.index(outer_open)
    assert script.index("const _reviewPaths") < script.index(outer_close)
    assert script.index(outer_close) < script.index("const HANDBACK")
    assert "_drainSweep" not in script


def test_run_close_is_one_drain_call_retried_once_and_names_both_failures():
    script = _compose()
    close = script[script.index("async function _runClose()"):]
    close = close[: close.index("\n}\n")]
    assert close.count("await _drainCall()") == 3
    assert "throw" not in close
    assert "_sweepCall" not in script and "_runRecordCall" not in script
    assert f"{grind_commit_contract.SWEEP_FAILED_HEAD}: state/queue-grind/fixture/runs/${{RUN_ID}}.json" in close
    assert "if (_drained.swept === false)" in close
    assert "run record may be missing or incomplete: state/queue-grind/fixture/runs/${RUN_ID}.json" in close


def test_drain_call_is_the_one_run_record_sweep_command_with_declared_writes_and_decisions():
    script = _compose()
    drain = script[script.index("async function _drainCall()"):]
    drain = drain[: drain.index("\n}\n")]
    assert "grind-row run-record --sweep --profile-dir ' + (_shq(PROFILE_DIR)) + ' --profile fixture --queue state/bug-backlog --run-id " in drain
    assert "--declared-urlenc" in drain and "_declaredWrites()" in drain
    assert "args.decisions_file || ''" in drain and "--decisions-file" in drain
    assert "grind-row sweep" not in drain and "commit_v2" not in drain
    assert "label: 'ledger-sweep:drain'" in drain


def test_terminal_commit_skips_when_nothing_queued_and_hands_back_without_a_token():
    script = _compose()
    fn = script[script.index("async function _terminalCommit()"):]
    fn = fn[: fn.index("\n}\n")]
    assert "if (_pendingCommit.length === 0) return;" in fn
    assert "if (!RUN_TOKEN) { _failAll(" in fn
    assert "halted: _halted" in fn
    assert "const _halted = _haltReason();" in fn
    reason = script[script.index("function _haltReason()"):]
    reason = reason[: reason.index("\n}\n")]
    assert "'budget-exhausted'" in reason and "_grindErr" in reason


def test_ledger_settle_verb_is_never_a_commit_step():
    script = _compose()
    assert "_ledgerPathFor" not in script
    assert "grind-row settle " not in script.replace("grind-row settle-run", "")


def test_no_leg_labelled_undo_or_run_record_close_is_emitted():
    script = _compose()
    assert "label: 'undo'" not in script and "label: 'run-record:close'" not in script
    assert "await _drainCall()" in script


def test_commit_failed_hand_back_names_the_runnable_settle_run_command():
    script = _compose()
    assert "SWEEP_HINT" not in script
    assert script.count("+ SETTLE_HINT") == 6
    hint = script[script.index("const SETTLE_HINT"):]
    hint = hint[: hint.index("\n")]
    assert "backlog-grind-assemble grind-row settle-run --profile-dir " in hint
    assert "+ _shq(PROFILE_DIR) +" in hint and "+ _shq(REPO_ROOT) +" in hint and "+ _shq(RUN_ID)" in hint
    assert "--profile fixture --run-id " in hint
    assert " --queue state/bug-backlog --repo-root " in hint


def test_no_git_mv_stash_add_dash_a():
    script = _compose()
    for token in ("git mv", "git stash", "add -A"):
        assert token not in script


def test_every_agent_call_site_has_recordcall_equivalent():
    """Defect: fix/verify/commit/refute-close never called `_recordCall`.
    Every captured call (`_capture`) appends `_recordCall(<kind>);`
    immediately after its own `agent(...)` -- count them 1:1."""
    script = grind_only(_compose())
    calls = len(re.findall(r"\bagent\(", script))
    # `_recordCall\('` (a quoted stage-kind literal) is a CALL SITE; the
    # bare `_recordCall(kind)` is the function's own definition.
    records = len(re.findall(r"_recordCall\('", script))
    assert calls > 0
    assert records == calls


def test_no_dead_batch_id_comparisons():
    """Defect: `_runTriage` compared `batchId === 'P0:b0'` against a value
    that never matched. This rewrite keys every call map by the manifest's
    OWN batch/row ids (looked up, never string-compared against a literal
    that cannot occur)."""
    script = _compose()
    assert "batchId ===" not in script
    assert "=== 'P0:b0'" not in script


def test_each_stage_composed_once_per_row_no_pipeline_over_batches():
    """Each stage kind is composed ONCE, as a function taking the row/batch
    object -- never unrolled per row/batch (EM follow-up: the Workflow
    tool's 512 KB inline-script cap, and the design's "manifest is the
    only per-row copy")."""
    script = _compose()
    assert "pipeline(" not in script
    assert "async function _fixCall(row) {" in script
    assert "async function _verifyCall(row) {" in script
    assert "async function _terminalCommitCall(params) {" in script
    assert "async function _commitCall" not in script
    assert script.count("async function _rejectUndoCall(row) {") == 1
    assert script.count("_rejectUndoCall(row)") == 3
    assert "_undoCall" not in script
    assert "async function _triageCall(batchId, batchKey, rowIds) {" in script


def test_agent_call_site_count_independent_of_row_count():
    """The number of literal `agent(` call SITES must be a small constant
    regardless of manifest size -- a 7-row and a 300-row manifest emit the
    identical count (break-class defect: the prior rewrite unrolled one
    call per row/batch, hitting the Workflow tool's 512 KB inline-script
    cap at ~890 real rows)."""
    profile = _fixture_profile()
    knobs = gp.resolve_appetite(profile, "sweep")

    def _manifest(n):
        keys = ["P0", "P1", "P2", "P3"]
        entries = tuple(
            ManifestEntry(
                row_id=f"row{i}", path=f"state/bug-backlog/row{i}.yaml",
                digest=f"{i:064x}", batch_key=keys[i % len(keys)],
            )
            for i in range(n)
        )
        return Manifest(entries=entries, batch_sizes={}, source=None, digest="deadbeef")

    def _agent_count(n):
        script = gc.compose_grind_script(
            _manifest(n), profile, knobs,
            run_dir=Path("state/queue-grind/fixture/run-1"), agent_type_host=None, **REVIEW_KW,
        )
        return len(re.findall(r"\bagent\(", script)), len(script.encode("utf-8"))

    small_count, _small_bytes = _agent_count(7)
    large_count, large_bytes = _agent_count(300)
    assert small_count == large_count
    assert large_bytes < 176 * 1024


def test_triage_declared_files_never_overwrites_touched_files():
    """A triage record's `declared_files` (files the triage merely CITED as
    evidence, never edited) sets the row's lock scope (`declaredFiles`)
    only. Overwriting `touchedFiles` too hands the committer paths with no
    actual diff vs HEAD, which `block-subagent-commit`'s path-scope check
    then legitimately refuses (denied-on-path-scope, "orphan" -- no session
    holds a claim because nothing was ever written there). `touchedFiles`
    starts at the row's own manifest path and is overwritten only by an
    actual write (a fix's own reported `touched_files`, or a close's
    `{old,new}` move) -- never by triage's citation list."""
    script = _compose()
    assert "row.declaredFiles = rec.declared_files; row.touchedFiles = rec.declared_files;" not in script
    m = re.search(
        r"if \(rec\.declared_files && rec\.declared_files\.length\) \{ ([^}]*) \}",
        script,
    )
    assert m is not None
    assert m.group(1).strip() == "row.declaredFiles = rec.declared_files;"


def test_real_bounded_concurrency_not_serial_pipeline():
    script = _compose()
    assert "runGrind" in script
    assert "Promise.race" not in script
    assert "Object.keys(_admitted).length < WINDOW" in script
    assert "_runBatchWorker(batchState).catch(" in script


def test_fix_commit_undo_interpolate_live_row_state_not_static_manifest_path():
    """The fix/commit/undo prompts read the row's own live
    declaredFiles/touchedFiles/removedFiles -- never a literal manifest
    path baked in as a stand-in for the run-time touched/declared list."""
    script = _compose()
    assert "(row.declaredFiles).join(', ')" in script
    assert "(row.touchedFiles).join(', ')" in script
    # no literal manifest row path inside a "stage exactly"/"you hold the
    # lock on" clause -- those clauses interpolate a live expression now.
    for m in re.finditer(r"Stage exactly this touched list: \[", script):
        assert script[m.end() : m.end() + 40].startswith(("' + ((_reviewPaths", "' + ((Array.from(new Set(row.touched"))
    # a finished row queues the paths a fix actually wrote, never the manifest path alone
    queue_fn = script[script.index("function _queueCommit"): script.index("function _unsettle")]
    assert "row.touchedFiles.concat(row.createdFiles || [])" in queue_fn
    assert "row.removedFiles" in queue_fn


# ---------------------------------------------------------------------------
# STAGE_OUTPUT_TOKENS / batch_reserve
# ---------------------------------------------------------------------------


def test_non_default_appetite_reaches_appetite_name():
    """Live-run defect 9: a run emitted with `--appetite hunt` reported
    `"appetite": "standard"` in its hand-back -- `knobs` never carried an
    `appetite` key, so `compose_grind_script` fell back to the default."""
    script = _compose(appetite="sweep")
    assert "const APPETITE_NAME = 'sweep';" in script
    assert "appetite: APPETITE_NAME" in script


def test_stage_output_tokens_all_positive():
    assert gc.STAGE_OUTPUT_TOKENS
    for kind, value in gc.STAGE_OUTPUT_TOKENS.items():
        assert value > 0, f"{kind}: {value}"


def test_batch_reserve_monotone_in_batch_size():
    sizes = [1, 2, 4, 8, 16]
    reserves = [gc.batch_reserve(n) for n in sizes]
    assert reserves == sorted(reserves)
    assert len(set(reserves)) == len(reserves)


def test_batch_reserve_refuses_nonpositive():
    with pytest.raises(ValueError):
        gc.batch_reserve(0)


# ---------------------------------------------------------------------------
# ROUTING const structural consistency with the profile the model reads
# ---------------------------------------------------------------------------


def test_routing_table_rendered_matches_profile_graph_edges_including_size_and_tradeoff_gate():
    profile = _fixture_profile()
    routing = gc.build_routing_table(profile)
    script = _compose(profile=profile)
    assert '"triage": {"edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}' in script
    for node_id, node in profile.graph.items():
        assert routing[node_id]["edges"] == dict(node.edges)
        assert routing[node_id]["kind"] == node.kind

    # engine-fixed size/tradeoff gate, driven directly (M+ -> baton;
    # below-floor with a tradeoff -> needs-judgment), before any profile
    # edge is even consulted.
    triage_node = gc.build_routing_table(profile)
    action = gc.route_after_triage(triage_node, "triage", "confirmed-bug", "M", "")
    assert action == ("handback", "baton")
    action = gc.route_after_triage(triage_node, "triage", "confirmed-bug", "S", "carries a tradeoff")
    assert action == ("handback", "needs-judgment")
    action = gc.route_after_triage(triage_node, "triage", "confirmed-bug", "S", "")
    assert action == ("node", "fix")


# ---------------------------------------------------------------------------
# Admission model -- behavioural (this is what tells a delivered composer
# from one that is wrong-but-deterministic)
# ---------------------------------------------------------------------------


class _StubBudgetSpender:
    def __init__(self, total=None):
        self._spent = 0
        self.total = total

    def spend(self, amount):
        self._spent += amount

    def spent(self):
        return self._spent

    def remaining(self):
        return None if self.total is None else self.total - self._spent


def _fixture_routing():
    return gc.build_routing_table(_fixture_profile()), "triage"


_RUN_CLOSE_REPLIES = {"ledger-sweep": {"outcome": "recorded", "swept": True}}


def _agent_stub(script_by_kind, budget):
    def _agent(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind in _RUN_CLOSE_REPLIES and stage_kind not in script_by_kind:
            return _RUN_CLOSE_REPLIES[stage_kind]
        return script_by_kind[stage_kind](unit_id)

    return _agent


def _run(batches, script_by_kind, *, batch_size=4, max_agent_calls=None, budget_tokens=None):
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender(total=budget_tokens)
    agent_fn = _agent_stub(script_by_kind, budget)
    result = gc.run_admission(
        batches,
        routing,
        triage_node,
        reserve=gc.batch_reserve(batch_size),
        max_agent_calls=max_agent_calls,
        budget=budget,
        agent=agent_fn,
    )
    return result, budget


def _scout(tshirt, **extra):
    return {"tshirt": tshirt, "intent": f"intent {tshirt}", "name": "Scout Name", **extra}


def _all_close_batches(row_ids):
    return {rid: {"verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""} for rid in row_ids}


def _all_fix_batches(row_ids):
    return {rid: {"verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""} for rid in row_ids}


def _triage_script(verdict_by_row):
    def _triage(batch_id, row_ids):
        return [{"row": rid, **verdict_by_row[rid]} for rid in row_ids]

    return _triage


def test_downstream_before_triage_and_window_bound():
    batches = [("b0", ["r0", "r1"]), ("b1", ["r2", "r3"]), ("b2", ["r4", "r5"])]
    verdicts = {**_all_close_batches(["r0", "r1"]), **_all_fix_batches(["r2", "r3", "r4", "r5"])}
    triage = _triage_script(verdicts)

    script_by_kind = {
        "triage": lambda bid: triage(bid, next(rows for b, rows in batches if b == bid)),
        "refute-close": lambda bid: {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}, {"row": "r1", "new_path": "archive/r1.yaml"}], "refuted": []},
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "abc"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=2)
    call_log = result["call_log"]

    triage_indices = [i for i, (kind, _uid) in enumerate(call_log) if kind == "triage"]
    downstream_indices = [i for i, (kind, _uid) in enumerate(call_log) if kind != "triage"]
    if len(triage_indices) >= 2 and downstream_indices:
        assert downstream_indices[0] < triage_indices[1]

    assert call_log[-2:] == [("commit", "run"), ("ledger-sweep", "run")]
    assert [c for c in call_log if c[0] == "commit"] == [("commit", "run")]


def test_admission_checks_spend_before_every_triage_admit_and_drain_hands_back_budget_exhausted():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(5)]
    verdicts = _all_fix_batches([f"r{i}" for i in range(5)])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, [bid.replace("b", "r")]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=1, budget_tokens=1)
    handback_types = {h["type"] for h in result["handed_back"]}
    assert "budget-exhausted" in handback_types
    reasons = [h["reason"] for h in result["handed_back"] if h["type"] == "budget-exhausted"]
    assert reasons and all("emit-admitted row" in r for r in reasons)


def test_max_agent_calls_is_the_deterministic_secondary_bound():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(10)]
    verdicts = _all_fix_batches([f"r{i}" for i in range(10)])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, [bid.replace("b", "r")]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=1, max_agent_calls=3)
    triage_calls = [c for c in result["call_log"] if c[0] == "triage"]
    assert len(triage_calls) == 1
    handback_types = {h["type"] for h in result["handed_back"]}
    assert "budget-exhausted" in handback_types


def test_budget_tokens_ceiling_halts_admission_independent_of_budget_total():
    # Parity for the
    # rendered `.mjs`'s BUDGET_TOKENS ceiling: exercised here with
    # budget.total left None, so only the new `budget_tokens` param can be
    # responsible for the halt.
    batches = [(f"b{i}", [f"r{i}"]) for i in range(5)]
    verdicts = _all_fix_batches([f"r{i}" for i in range(5)])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, [bid.replace("b", "r")]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender(total=None)
    agent_fn = _agent_stub(script_by_kind, budget)
    result = gc.run_admission(
        batches, routing, triage_node,
        reserve=gc.batch_reserve(1),
        budget_tokens=1,
        budget=budget,
        agent=agent_fn,
    )
    handback_types = {h["type"] for h in result["handed_back"]}
    assert "budget-exhausted" in handback_types


def test_close_confirmed_routed_straight_to_handback_commits_inline_and_settles():
    # Parity for the
    # rendered `.mjs`'s inline archive-move-commit branch: a profile graph
    # where refute-close's `confirmed` edge has no explicit `commit` node
    # between it and its terminal type still commits, and the successful
    # commit is recorded in `settled` (never silently dropped).
    routing = {
        "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "close"}, "on_fail": None},
        "close": {"kind": "refute-close", "edges": {"confirmed": "settled-direct", "refuted": "settled-direct"}, "on_fail": None},
    }
    script_by_kind = {
        "triage": lambda bid: [{"row": "r0", "verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""}],
        "refute-close": lambda bid: {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}], "refuted": []},
        "commit": lambda _unit: {"committed": True, "sha": "deadbeef"},
    }
    budget = _StubBudgetSpender()
    agent_fn = _agent_stub(script_by_kind, budget)
    result = gc.run_admission(
        [("b0", ["r0"])], routing, "triage",
        reserve=gc.batch_reserve(1),
        budget=budget,
        agent=agent_fn,
    )
    assert {"row": "r0", "outcome": "committed", "sha": "deadbeef"} in result["settled"]
    assert ("commit", "run") in result["call_log"]
    assert ("commit", "r0") not in result["call_log"]
    # applyRoute's own handback (the routed-to terminal type) still fires --
    # the queued commit is additional, never a substitute for it.
    assert any(h["row"] == "r0" and h["type"] == "settled-direct" for h in result["handed_back"])


# ---------------------------------------------------------------------------
# P145-C2: refuted rows reach the fixer with the refuter's reason, and
# refute verdicts are counted by proposal origin.
# ---------------------------------------------------------------------------


def test_refuted_row_reaches_fix_and_by_refute_origin_counts_confirmed_and_refuted():
    verdicts = _all_close_batches(["r0", "r1"])
    triage = _triage_script(verdicts)
    fix_calls = []

    def close_stub(bid):
        return {
            "confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}],
            "refuted": [{"row": "r1", "reason": "still reproduces at HEAD"}],
        }

    def fix_stub(rid):
        fix_calls.append(rid)
        return {"outcome": "NEEDS_PLAN"}

    script_by_kind = {
        "triage": lambda bid: triage(bid, ["r0", "r1"]),
        "refute-close": close_stub,
        "fix": fix_stub,
        "commit": lambda _unit: {"committed": True, "sha": "abc"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)

    # r1's refute-close `refuted` edge routes to `fix` (fixture profile) --
    # the refuted row reaches the fixer, never dead-ends at refute-close.
    assert fix_calls == ["r1"]
    assert result["by_refute_origin"] == {
        "triage:not-reproduced": {"confirmed": 1, "refuted": 1},
    }
    # Design item 2's last bullet: a row revived after a refuted close
    # proposal that later hands back gives, in its reason, the origin of
    # that refuted proposal and the refuter's reason verbatim.
    handback = next(h for h in result["handed_back"] if h["row"] == "r1")
    assert handback["type"] == "baton"
    assert "still reproduces at HEAD" in handback["reason"]
    assert "triage:not-reproduced" in handback["reason"]


def test_by_refute_origin_keys_by_origin_not_batch_id():
    verdicts = _all_close_batches(["r0", "r1"])
    triage = _triage_script(verdicts)

    def close_stub(bid):
        return {
            "confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}, {"row": "r1", "new_path": "archive/r1.yaml"}],
            "refuted": [],
        }

    script_by_kind = {
        "triage": lambda bid: triage(bid, ["r0", "r1"]),
        "refute-close": close_stub,
        "commit": lambda _unit: {"committed": True, "sha": "abc"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    # Both rows carry the same origin (`triage:not-reproduced`) though they
    # came through the same batch -- the origin key is derived from the
    # row's own triage verdict, never the batch id.
    assert result["by_refute_origin"] == {"triage:not-reproduced": {"confirmed": 2, "refuted": 0}}


def test_composed_fix_call_interpolates_the_refuters_reason_verbatim():
    """The JS renderer's `_fixCall` interpolates `row.refuteNote` -- the
    same string `_closeBatch` set verbatim from the refute-close agent's
    own `refuted[].reason` -- into the composed fix prompt."""
    script = _compose()
    fix_fn = script[script.index("async function _fixCall"): script.index("async function _verifyCall")]
    assert "row.refuteNote" in fix_fn
    close_fn = script[script.index("async function _closeBatch"): script.index("async function _fixStage")]
    assert "row.refuteNote = entry.reason || '';" in close_fn


def test_counts_expose_by_refute_origin_from_the_rendered_script():
    script = _compose()
    assert "by_refute_origin: _byRefuteOrigin" in script
    assert "function _bumpRefuteOrigin(origin, key)" in script


def test_triage_sets_row_origin_from_its_own_verdict_not_batch_id():
    script = _compose()
    triage_fn = script[script.index("async function _triageBatch"): script.index("async function _closeBatch")]
    assert "row.origin = `triage:${rec.verdict}`;" in triage_fn


def test_close_proposals_carry_their_own_origin_field():
    script = _compose()
    close_fn = script[script.index("async function _closeBatch"): script.index("async function _fixStage")]
    assert "origin: row.origin" in close_fn


def test_widen_release_reacquire_exactly_once_then_widen_exhausted():
    calls = {"fix": 0}
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (calls.__setitem__("fix", calls["fix"] + 1), {"outcome": "NEEDS_WIDER_SCOPE"})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert calls["fix"] == 2
    assert any(h["type"] == "widen-exhausted" for h in result["handed_back"])


def test_verify_retry_exactly_once_then_undo_rejected_after_retry():
    seen = []
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (seen.append("fix"), {"outcome": "done"})[1],
        "verify": lambda rid: (seen.append("verify"), {"outcome": "fail", "reason": "nope", "undo": {"outcome": "undone"}})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert seen.count("verify") == 2
    assert "undo" not in result["spend"]["agent_calls_by_stage_kind"]
    assert any(h["type"] == "rejected-after-retry" for h in result["handed_back"])


@pytest.mark.parametrize("undo_result", [None, {}, {"outcome": "undo-failed", "reason": "3 files still modified"}])
def test_undo_that_does_not_return_undone_hands_back_stage_dead_undo_failed(undo_result):
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "fail", "reason": "nope", "undo": undo_result},
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    types = [h["type"] for h in result["handed_back"] if h["row"] == "r0"]
    assert types == ["stage-dead"]
    reason = next(h["reason"] for h in result["handed_back"] if h["row"] == "r0")
    assert reason.startswith("undo-failed:")
    assert not result["settled"]


def test_golden_verify_stage_gates_rejected_after_retry_on_undone_outcome():
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    stage = golden[golden.index("async function _verifyStage"):golden.index("async function _commitStage")]
    assert "const _undone = result.undo || {};" in stage
    stage = stage[stage.index("const _undone = result.undo"):]
    assert "_undoCall" not in stage
    assert stage.index("_undone.outcome !== 'undone'") < stage.index("'rejected-after-retry'")
    assert "'undo-failed: " in stage


def test_row_sized_m_or_above_routes_to_baton_never_fix():
    verdicts = {"r0": {"verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""}}
    fix_calls = {"n": 0}
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "size": lambda rid: _scout("M", exit_code=0),
        "fix": lambda rid: (fix_calls.__setitem__("n", fix_calls["n"] + 1), {"outcome": "done"})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert fix_calls["n"] == 0
    assert any(h["type"] == "baton" and h["row"] == "r0" for h in result["handed_back"])


def test_tradeoff_below_plan_weight_routes_to_needs_judgment_never_fix():
    verdicts = {"r0": {"verdict": "confirmed-bug", "tshirt_size": "S", "has_tradeoff": True, "tradeoff": "cuts a corner"}}
    fix_calls = {"n": 0}
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (fix_calls.__setitem__("n", fix_calls["n"] + 1), {"outcome": "done"})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert fix_calls["n"] == 0
    assert any(h["type"] == "needs-judgment" and h["row"] == "r0" for h in result["handed_back"])


def test_fixer_reported_tradeoff_also_routes_needs_judgment():
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "done", "has_tradeoff": True, "tradeoff": "surprise tradeoff"},
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert any(h["type"] == "needs-judgment" and h["row"] == "r0" for h in result["handed_back"])


def test_each_stage_runs_exactly_once_per_row_in_call_log():
    verdicts = _all_fix_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    counts = Counter(result["call_log"])
    for rid in ("r0", "r1"):
        assert counts[("fix", rid)] == 1
        assert counts[("verify", rid)] == 1
        assert counts[("commit", rid)] == 0
    assert counts[("commit", "run")] == 1


def test_refute_close_confirmed_routes_to_commit_refuted_revives_to_fix():
    verdicts = _all_close_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "refute-close": lambda bid: {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}], "refuted": [{"row": "r1", "reason": "still broken"}]},
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    settled_rows = {s["row"] for s in result["settled"]}
    # the fixture profile's refute_close node routes `confirmed` to commit
    # directly, but `refuted` REVIVES to `fix` per the new refusal -- both
    # rows still settle, but r1 only after a fresh fix/verify pass.
    assert settled_rows == {"r0", "r1"}
    assert ("fix", "r1") in result["call_log"]


# ---------------------------------------------------------------------------
# Hand-back spend accounting (PM ruling: actual spend, no dry-run forecast)
# ---------------------------------------------------------------------------


def test_handback_spend_matches_stub_budget_delta_and_call_counts():
    verdicts = {
        "r0": {"verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""},
        "r1": {"verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""},
    }
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "refute-close": lambda bid: {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}], "refuted": []},
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    spend = result["spend"]
    assert spend["output_tokens"] == budget.spent()
    assert spend["agent_calls_total"] == len(result["call_log"])
    assert sum(spend["agent_calls_by_stage_kind"].values()) == spend["agent_calls_total"]
    expected = Counter(kind for kind, _uid in result["call_log"])
    assert dict(spend["agent_calls_by_stage_kind"]) == dict(expected)


def test_on_fail_traversed_at_most_once_structurally():
    profile = _fixture_profile()
    for node in profile.graph.values():
        assert node.on_fail is None or node.on_fail in profile.graph


def test_on_fail_naming_a_hand_back_type_hands_back_without_spending_the_retry():
    routing = {
        "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": "needs-judgment"},
        "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": "fix"},
    }
    row = gc.Row(row_id="r1", path="p", batch_id="b")
    assert gc.follow_edge(routing, "fix", "failed", row) == ("handback", "needs-judgment")
    assert row.on_fail_used is False
    assert gc.follow_edge(routing, "verify", "fail", row) == ("node", "fix")
    assert row.on_fail_used is True


# ---------------------------------------------------------------------------
# EM follow-up #3: runtime/prompt defects (break-class)
# ---------------------------------------------------------------------------


def test_no_literal_script_placeholder_in_golden():
    script = _compose()
    assert "<script>" not in script


# ---------------------------------------------------------------------------
# MANIFEST_STALE (grind-row close exit 3) -- fix outcome + refute-close stale
# ---------------------------------------------------------------------------


def test_fix_manifest_stale_outcome_hands_back_manifest_stale():
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "MANIFEST_STALE"},
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert any(h["type"] == "manifest-stale" and h["row"] == "r0" for h in result["handed_back"])


def test_refute_close_stale_row_hands_back_manifest_stale():
    verdicts = _all_close_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "refute-close": lambda bid: {
            "confirmed": [],
            "refuted": [{"row": "r1", "reason": "still broken"}],
            "stale": ["r0"],
        },
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    assert any(h["type"] == "manifest-stale" and h["row"] == "r0" for h in result["handed_back"])


def test_verify_fail_routes_back_to_fix_with_feedback_then_undo_on_second_fail():
    """DR-404: a verify failure routes back to FIX with the verifier's
    reason as feedback, not a blind re-verify of the same fix."""
    verdicts = _all_fix_batches(["r0"])
    seen = []

    def fix_stub(rid):
        seen.append("fix")
        return {"outcome": "done"}

    def verify_stub(rid):
        seen.append("verify")
        return {"outcome": "fail", "reason": "tests still fail", "undo": {"outcome": "undone"}}

    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": fix_stub,
        "verify": verify_stub,
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert seen == ["fix", "verify", "fix", "verify"]
    assert any(h["type"] == "rejected-after-retry" for h in result["handed_back"])


def test_triage_rows_omitted_from_result_hand_back_stage_dead_not_stranded():
    """A row triage never returns a record for must hand back `stage-dead`,
    never stay pending with `node === null` forever."""
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return []  # never mentions r0
        return {"outcome": "n/a"}

    result = gc.run_admission(
        [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
        budget=budget, agent=agent_fn,
    )
    assert any(h["row"] == "r0" and h["type"] == "stage-dead" for h in result["handed_back"])


def test_drain_commit_is_handed_the_run_cost_record_body():
    """The drain agent writes runs/<run-id>.json; it must be given the body
    (profile, appetite, resolved_knobs, manifest_digest, counts, spend), not
    just the file name."""
    script = _compose()
    assert "encodeURIComponent(JSON.stringify(_runCostRecord()))" in script
    assert "--record-from-transcript " in script
    assert "--record-urlenc" not in script
    assert "--record-len" not in script and "--record-sha256" not in script
    assert ".rec'" not in script and "RUNREC-OPEN-" in script and "RUNREC-CLOSE-" in script
    record_fn = script[script.index("function _runCostRecord()"):]
    record_fn = record_fn[: record_fn.index("\n}") ]
    for key in ("profile:", "appetite:", "resolved_knobs: RESOLVED_KNOBS", "manifest_digest: MANIFEST_DIGEST",
                "counts: _counts()", "spend: _spend()", "handed_back: _handedBack",
                "halted: _haltReason() !== null", "halted_reason: _haltReason()"):
        assert key in record_fn, key


def test_run_cost_record_reads_not_run_until_the_review_wave_assigns_it():
    """Node is barred, so the record's initial state and the only assignment sites are checked over the script."""
    script = _compose()
    assert "let _reviewState = 'not-run';\nlet _reviewVerdictsRecord = null;" in script
    record_fn = script[script.index("function _runCostRecord()"):]
    assert "review_state: _reviewState, review_verdicts: _reviewVerdictsRecord," in record_fn[: record_fn.index("\n}")]
    assert script.count("_reviewState = 'ran'") == 1 and script.count("_reviewState = 'withheld'") == 1
    assert script.index("_reviewState = 'ran'") < script.index("_reviewState = 'withheld'") < script.index("phase('Review fixes')")


def test_run_cost_record_carries_only_wave_entries_that_have_a_verdict():
    script = _compose()
    start = script.index("const _waveVerdicts = ")
    block = script[start:script.index("\n  }", start)]
    assert "typeof r.verdict === 'string' && r.verdict.trim()" in block
    assert "verdict: r.verdict," in block and "r.verdict ?? null" not in block
    assert "if (_waveVerdicts.length > 0) {\n    _reviewState = 'ran';" in block
    assert "_reviewVerdictsRecord = []" not in script


def test_run_record_review_verdicts_are_assigned_inside_the_review_gate_before_run_close():
    script = _compose()
    record_fn = script[script.index("function _runCostRecord()"):]
    assert "review_verdicts: _reviewVerdictsRecord" in record_fn[: record_fn.index("\n}")]
    gate = script.index("if (_reviewPaths.length === 0)")
    assign = script.index("_reviewVerdictsRecord = _waveVerdicts")
    close = script.index("finally { await _runClose(); }")
    assert gate < assign < close


def test_batch_helpers_receive_a_batches_entry_never_batch_state():
    """`batchState` ({id, batchKey, triaged, closedRows}) has no `.rows`; a
    helper typed on a BATCHES entry that is handed batchState throws at run
    time (every grind died at _finishBatch this way). Node is barred, so the
    call sites are checked statically over the rendered script."""
    import re
    script = _compose()
    helpers = re.findall(r"function (\w+)\(batch\)", script)
    assert {"_pendingRow", "_batchDone"} <= set(helpers)
    for name in helpers:
        for arg in re.findall(rf"\b{name}\(([^()]*(?:\([^()]*\)[^()]*)*)\)", script):
            if arg == "batch":
                continue
            assert arg.startswith("BATCHES.find("), f"{name}({arg})"
    assert "batchState.rows" not in script


# ---------------------------------------------------------------------------
# staff-eng review follow-up (coordinator-staff-eng.af423efe80835b8d2.md):
# runtime defects found by hand-trace over the golden -- one test per
# numbered finding.
# ---------------------------------------------------------------------------


def test_finding1_every_captured_agent_result_is_null_guarded():
    """Every `_result = await agent(...)` capture is wrapped `|| {}` so a
    null/undefined agent result reads as an empty object instead of
    throwing on the first `.field` access."""
    script = grind_only(_compose())
    captures = re.findall(r"const _result = \(await agent\(", script)
    guards = re.findall(r"\)\) \|\| \{\};", script)
    assert captures
    assert len(captures) == len(guards) == len(re.findall(r"\bagent\(", script))


def test_finding1_null_fix_verify_close_route_to_stage_dead_never_throw():
    """A null result from fix/verify/close routes its row(s) to stage-dead
    rather than throwing (driven through the pure-Python admission model,
    which mirrors the guarded rendered script)."""
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
        return None

    result = gc.run_admission(
        [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
        budget=budget, agent=agent_fn,
    )
    assert [h for h in result["handed_back"] if h["row"] == "r0"] == [
        {"row": "r0", "type": "stage-dead", "reason": "fix outcome None"}
    ]


def test_finding1_null_commit_result_hands_back_commit_failed():
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
        if stage_kind in ("fix",):
            return {"outcome": "done"}
        if stage_kind == "verify":
            return {"outcome": "pass"}
        if stage_kind == "commit":
            return None
        return None

    result = gc.run_admission(
        [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
        budget=budget, agent=agent_fn,
    )
    assert any(h["row"] == "r0" and h["type"] == "commit-failed" for h in result["handed_back"])


def test_finding2_run_batch_worker_wraps_body_in_try_catch_and_still_finishes():
    script = _compose()
    worker = script[script.index("async function _runBatchWorker"): script.index("async function runGrind")]
    assert "try {" in worker
    assert "} catch (err) {" in worker
    # the catch and the post-batch cleanup are both reached -- the catch
    # is not itself inside the try it guards, and the cleanup sits after
    # the try/catch, not inside it.
    catch_idx = worker.index("} catch (err) {")
    finish_idx = worker.index("delete _admitted[batchState.id];")
    assert catch_idx < finish_idx


def test_finding3_refute_close_leftover_hands_back_stage_dead_behaviourally():
    verdicts = _all_close_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        # neither row is confirmed, refuted or stale -- {} is a legal (if
        # useless) refute-close result.
        "refute-close": lambda bid: {},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    handback_types = {(h["row"], h["type"]) for h in result["handed_back"]}
    assert ("r0", "stage-dead") in handback_types
    assert ("r1", "stage-dead") in handback_types


def test_finding5_cross_batch_triage_record_is_ignored_python_model():
    """A triage record naming a row from another batch never reroutes it
    (misroute 5) -- the python model already filters by `batch.row_ids`."""
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()

    def agent_fn(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
        if stage_kind == "triage":
            # b1's triage call mentions r0, which belongs to b0.
            if unit_id == "b0":
                return []
            return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
        return {"outcome": "n/a"}

    result = gc.run_admission(
        [("b0", ["r0"]), ("b1", ["r1"])], routing, triage_node, reserve=gc.batch_reserve(1),
        budget=budget, agent=agent_fn,
    )
    # r0's own (b0) triage never mentioned it -- it hands back stage-dead,
    # never rerouted by b1's mis-scoped record.
    assert any(h["row"] == "r0" and h["type"] == "stage-dead" for h in result["handed_back"])


def test_finding9_refuted_to_commit_edge_now_refused():
    """The predecessor plan's fixture-authoring choice -- refute_close's
    `refuted -> commit` edge, settling both confirmed and refuted proposals
    via commit -- is exactly the KEPT_OPEN stranding the 2026-09-21 grind
    handoff measured (fourteen confirmed defects stranded); no DoE profile
    relied on it (census row 1). The fixture now routes `refuted` to `fix`,
    and `validate_graph` refuses the old routing outright."""
    profile = _fixture_profile()
    assert profile.graph["refute_close"].edges["refuted"] == "fix"

    doc = yaml.safe_load((_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"))
    doc["graph"]["refute_close"]["edges"]["refuted"] = "commit"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "reverted.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        reverted = gp.load_profile("reverted", tmp_path)
        with pytest.raises(gp.ProfileError) as exc_info:
            gp.validate_graph(reverted)
        assert exc_info.value.rule == "refuted_not_revived"


# ---------------------------------------------------------------------------
# Structural safety net (brief: extend it) -- every `agent(` consumer is
# null-guarded, and every `while` loop has a progress guarantee.
# ---------------------------------------------------------------------------


def test_every_while_loop_has_a_progress_guarantee_reference():
    """Every `while` loop in the golden sits next to a comment or a call
    naming the progress mechanism that prevents it spinning forever."""
    script = _compose().replace(grind_commit_contract.RELAY_SHA256_JS, "")
    while_lines = [i for i, line in enumerate(script.splitlines()) if re.search(r"\bwhile\s*\(", line)]
    assert while_lines
    lines = script.splitlines()
    progress_markers = ("_pendingRow", "_queue.length", "workers.length")
    for idx in while_lines:
        window = "\n".join(lines[max(0, idx - 2): idx + 2])
        assert any(marker in window for marker in progress_markers), lines[idx]


def test_no_row_can_stay_non_done_forever_for_the_fixture_graph_under_every_outcome_including_null():
    """Drive the pure-Python admission model over the fixture graph under
    every distinct agent outcome the stage library's own schemas enumerate
    (including a null/None result) and assert `_pendingRow`'s python
    mirror -- a row on a node with no runnable kind -- never happens: every
    row ends up `done` (committed or handed back), never stuck forever."""
    from coordinator_core.contract import grind_vocab as vocab

    routing, triage_node = _fixture_routing()

    fix_outcomes = list(vocab.STAGE_OUTCOMES["fix"]) + [None]
    verify_outcomes = ["pass", "fail", None]
    commit_outcomes = ["committed", "commit-failed", None]
    close_shapes = [
        {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}], "refuted": []},
        {"confirmed": [], "refuted": [{"row": "r0", "reason": "x"}]},
        {},
        None,
    ]

    for fix_outcome in fix_outcomes:
        budget = _StubBudgetSpender()

        def agent_fn(stage_kind, unit_id, _fo=fix_outcome):
            budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
            if stage_kind == "triage":
                return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
            if stage_kind == "fix":
                return {"outcome": _fo} if _fo is not None else None
            if stage_kind == "verify":
                return {"outcome": "pass"}
            if stage_kind == "commit":
                return {"outcome": "committed", "sha": "x"}
            return None

        result = gc.run_admission(
            [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
            budget=budget, agent=agent_fn, max_agent_calls=20,
        )
        settled_or_handed_back = {s["row"] for s in result["settled"]} | {h["row"] for h in result["handed_back"]}
        assert "r0" in settled_or_handed_back, (fix_outcome, result)

    for verify_outcome in verify_outcomes:
        budget = _StubBudgetSpender()

        def agent_fn(stage_kind, unit_id, _vo=verify_outcome):
            budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
            if stage_kind == "triage":
                return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
            if stage_kind == "fix":
                return {"outcome": "done"}
            if stage_kind == "verify":
                return {"outcome": _vo} if _vo is not None else None
            if stage_kind == "commit":
                return {"outcome": "committed", "sha": "x"}
            return None

        result = gc.run_admission(
            [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
            budget=budget, agent=agent_fn, max_agent_calls=20,
        )
        settled_or_handed_back = {s["row"] for s in result["settled"]} | {h["row"] for h in result["handed_back"]}
        assert "r0" in settled_or_handed_back, (verify_outcome, result)

    for commit_outcome in commit_outcomes:
        budget = _StubBudgetSpender()

        def agent_fn(stage_kind, unit_id, _co=commit_outcome):
            budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
            if stage_kind == "triage":
                return [{"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}]
            if stage_kind == "fix":
                return {"outcome": "done"}
            if stage_kind == "verify":
                return {"outcome": "pass"}
            if stage_kind == "commit":
                return {"outcome": _co} if _co is not None else None
            return None

        result = gc.run_admission(
            [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
            budget=budget, agent=agent_fn, max_agent_calls=20,
        )
        settled_or_handed_back = {s["row"] for s in result["settled"]} | {h["row"] for h in result["handed_back"]}
        assert "r0" in settled_or_handed_back, (commit_outcome, result)

    for close_shape in close_shapes:
        budget = _StubBudgetSpender()
        verdicts = _all_close_batches(["r0"])

        def agent_fn(stage_kind, unit_id, _cs=close_shape):
            budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
            if stage_kind == "triage":
                return _triage_script(verdicts)(unit_id, ["r0"])
            if stage_kind == "refute-close":
                return _cs
            if stage_kind == "commit":
                return {"outcome": "committed", "sha": "x"}
            return None

        result = gc.run_admission(
            [("b0", ["r0"])], routing, triage_node, reserve=gc.batch_reserve(1),
            budget=budget, agent=agent_fn, max_agent_calls=20,
        )
        settled_or_handed_back = {s["row"] for s in result["settled"]} | {h["row"] for h in result["handed_back"]}
        assert "r0" in settled_or_handed_back, (close_shape, result)


# ---------------------------------------------------------------------------
# The size stage: one scout per plan-weight row, one size-assemble per run
# ---------------------------------------------------------------------------


def _m_triage(row_ids):
    return lambda batch_id: [
        {"row": rid, "verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""} for rid in row_ids
    ]


def test_scout_below_plan_weight_reaches_the_fix_node_with_size_origin():
    fix_seen = []

    def fix(row_id):
        fix_seen.append(row_id)
        return {"outcome": "NEEDS_PLAN", "has_tradeoff": False}

    result, _ = _run([("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: _scout("S"), "fix": fix})
    assert fix_seen == ["r0"]
    assert result["by_triage_check"]["size_collapsed"] == 1
    assert result["by_triage_check"]["fix_needs_plan"] == 1
    assert "size-assemble" not in result["spend"]["agent_calls_by_stage_kind"]


def test_scout_below_plan_weight_marks_origin_size_below_plan_weight():
    origins = []

    def fix(row_id):
        return {"outcome": "NEEDS_PLAN", "has_tradeoff": False}

    original = gc.Row

    class _Spy(original):
        def __setattr__(self, name, value):
            if name == "origin":
                origins.append(value)
            super().__setattr__(name, value)

    gc.Row = _Spy
    try:
        _run([("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: _scout("XS"), "fix": fix})
    finally:
        gc.Row = original
    assert "size:below-plan-weight" in origins


def test_scout_at_plan_weight_hands_back_baton_naming_the_sizing():
    script_by_kind = {
        "triage": _m_triage(["r0"]),
        "size": lambda rid: _scout("M", exit_code=0),
    }
    result, _ = _run([("b0", ["r0"])], script_by_kind, )
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["type"] == "baton"
    assert hand["reason"].startswith("sized at state/sizings/")
    assert hand["reason"].endswith("-scout-name-r0.yaml: intent M")
    assert result["by_triage_check"]["size_confirmed"] == 1


def test_nonzero_assemble_exit_is_a_baton_naming_the_exit_code():
    script_by_kind = {
        "triage": _m_triage(["r0"]),
        "size": lambda rid: _scout("L", exit_code=3),
    }
    result, _ = _run([("b0", ["r0"])], script_by_kind)
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["type"] == "baton"
    assert hand["reason"] == "intent L (size step failed: exit 3: (no stderr))"


def test_nonzero_assemble_exit_hand_back_carries_the_stderr_tail():
    script_by_kind = {
        "triage": _m_triage(["r0"]),
        "size": lambda rid: _scout("L", exit_code=1, stderr="  error: estimate.tshirt is 'XS'  "),
    }
    result, _ = _run([("b0", ["r0"])], script_by_kind)
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["reason"] == "intent L (size step failed: exit 1: error: estimate.tshirt is 'XS')"


def test_scout_that_returns_no_exit_code_is_a_baton_naming_a_missing_exit():
    result, _ = _run([("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: _scout("M")})
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["reason"] == (
        "intent M (size step failed: exit missing: the scout ran no size-chain command; (no stderr))"
    )


def test_scout_with_no_exit_code_is_redispatched_once_and_the_second_reply_sizes_the_row():
    replies = [_scout("M"), _scout("M", exit_code=0)]
    result, _ = _run([("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: replies.pop(0)})
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["reason"].startswith("sized at state/sizings/")
    assert result["spend"]["agent_calls_by_stage_kind"]["size"] == 2


def test_scout_with_no_exit_code_twice_hands_back_naming_its_own_reason_after_one_retry():
    replies = [_scout("M"), _scout("M", reason="could not run: no shell")]
    result, _ = _run([("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: replies.pop(0)})
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["reason"] == (
        "intent M (size step failed: exit missing: the scout ran no size-chain command; "
        "could not run: no shell; (no stderr))"
    )
    assert result["spend"]["agent_calls_by_stage_kind"]["size"] == 2


def test_scout_that_ran_the_fail_command_and_named_a_reason_is_not_redispatched():
    result, _ = _run(
        [("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: _scout("M", reason="no shell")}
    )
    assert result["spend"]["agent_calls_by_stage_kind"]["size"] == 1


def test_scout_with_an_exit_code_is_not_redispatched():
    result, _ = _run(
        [("b0", ["r0"])], {"triage": _m_triage(["r0"]), "size": lambda rid: _scout("L", exit_code=3)}
    )
    assert result["spend"]["agent_calls_by_stage_kind"]["size"] == 1


def test_emitted_script_redispatches_a_size_scout_without_an_exit_code_once():
    script = _compose()
    assert "Number.isInteger(scouted.exit_code)" in script
    assert script.count("_sizeCall(row, true)") == 1
    assert "reason" in gc.stages.SIZE_SCOUT_SCHEMA["properties"]


def test_scout_with_no_size_hands_back_baton_without_an_assemble_call():
    script_by_kind = {"triage": _m_triage(["r0"]), "size": lambda rid: {"reason": "refused"}}
    result, _ = _run([("b0", ["r0"])], script_by_kind)
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["type"] == "baton"
    assert hand["reason"] == "size scout returned no size: refused"
    assert "size-assemble" not in result["spend"]["agent_calls_by_stage_kind"]


def test_two_plan_weight_rows_in_two_batches_make_two_scouts_and_no_assemble_call():
    batches = [("b0", ["r0"]), ("b1", ["r1"])]

    def triage(batch_id):
        return _m_triage(["r0"] if batch_id == "b0" else ["r1"])(batch_id)

    script_by_kind = {
        "triage": triage,
        "size": lambda rid: _scout("M", exit_code=0),
    }
    result, _ = _run(batches, script_by_kind, batch_size=1)
    by_kind = result["spend"]["agent_calls_by_stage_kind"]
    assert by_kind["size"] == 2
    assert "size-assemble" not in by_kind
    assert {h["row"] for h in result["handed_back"] if h["type"] == "baton"} == {"r0", "r1"}


def test_a_run_with_no_plan_weight_row_makes_no_size_call():
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {"committed": True, "sha": "x"},
    }
    result, _ = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    by_kind = result["spend"]["agent_calls_by_stage_kind"]
    assert "size" not in by_kind and "size-assemble" not in by_kind


def test_by_triage_check_counts_each_branch_and_a_straight_hand_back():
    def triage(batch_id):
        return [
            {"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": "a corner"},
            {"row": "r1", "verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""},
            {"row": "r2", "verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""},
            {"row": "r3", "verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""},
        ]

    sizes = {"r1": "M", "r2": "S", "r3": "S"}
    fixes = {
        "r2": {"outcome": "NEEDS_PLAN", "has_tradeoff": False},
        "r3": {"outcome": "done", "has_tradeoff": True, "tradeoff": "late"},
    }
    script_by_kind = {
        "triage": lambda bid: [dict(r, has_tradeoff=bool(r["tradeoff"])) for r in triage(bid)],
        "size": lambda rid: _scout(sizes[rid], exit_code=0),
        "fix": lambda rid: fixes[rid],
    }
    result, _ = _run([("b0", ["r0", "r1", "r2", "r3"])], script_by_kind)
    assert result["by_triage_check"] == {
        "triaged": 4,
        "triage_handback": 1,
        "size_confirmed": 1,
        "size_collapsed": 2,
        "fix_needs_plan": 1,
        "fix_not_reproduced": 0,
        "fix_late_tradeoff": 1,
        "ruled_tradeoff": 0,
        "repeat_refused": 0,
    }


def test_fix_not_reproduced_is_counted():
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "NOT_REPRODUCED", "has_tradeoff": False},
    }
    result, _ = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert result["by_triage_check"]["fix_not_reproduced"] == 1


def test_two_fix_kind_triage_targets_leave_the_fix_verdict_unresolved_and_the_row_sized():
    """`triage_fix_verdict` refuses to pick a fix node when triage's own edges
    reach TWO fix-kind targets, so a below-floor scout size is assembled."""
    routing = {
        "triage": {"kind": "triage", "edges": {"v1": "fixA", "v2": "fixB"}, "on_fail": None},
        "fixA": {"kind": "fix", "edges": {}, "on_fail": None},
        "fixB": {"kind": "fix", "edges": {}, "on_fail": None},
    }
    assert gc.triage_fix_verdict(routing, "triage") is None

    def triage(batch_id):
        return [{"row": "r0", "verdict": "v1", "tshirt_size": "M", "tradeoff": ""}]

    budget = _StubBudgetSpender()
    agent_fn = _agent_stub(
        {"triage": triage, "size": lambda rid: _scout("S", exit_code=0)},
        budget,
    )
    result = gc.run_admission(
        [("b0", ["r0"])], routing, "triage", reserve=gc.batch_reserve(1), budget=budget, agent=agent_fn,
    )
    hand = next(h for h in result["handed_back"] if h["row"] == "r0")
    assert hand["type"] == "baton"


def test_triage_fix_verdict_resolves_the_fix_kind_verdict():
    routing = {
        "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}, "on_fail": None},
        "fix": {"kind": "fix", "edges": {}, "on_fail": None},
        "refute_close": {"kind": "refute-close", "edges": {}, "on_fail": None},
    }
    assert gc.triage_fix_verdict(routing, "triage") == "confirmed-bug"
    profile = _fixture_profile()
    fixture_routing = gc.build_routing_table(profile)
    verdict = gc.triage_fix_verdict(fixture_routing, gc._triage_node_id(profile))
    assert verdict is None or verdict in profile.verdicts


def test_golden_size_call_site_ordered_before_first_dispatch_row_call():
    script = _compose()
    size_agent_idx = script.index("label: 'size'")
    dispatch_row_call_idx = script.index("await _dispatchRow(")
    assert size_agent_idx < dispatch_row_call_idx


def test_golden_size_scout_hands_back_the_baton_from_its_own_assemble_reply():
    script = _compose()
    assert "_sizeAssembleCall" not in script and "_sizePending" not in script
    assert "_recordCall('size-assemble')" not in script and "_recordCall('size')" in script
    assert "label: 'size-assemble'" not in script
    to_size = script[script.index("let scouted = await _issue(batchState.id, rid, () => _sizeCall(row));"):]
    to_size = to_size[:to_size.index("async function _closeBatch")]
    assert "scouted.exit_code === 0" in to_size
    assert "row.sizingPath = _sizingPath(scouted, rid);" in to_size
    assert "'sized at ' + row.sizingPath" in to_size
    assert "size step failed: ' + _sizeFailReason(scouted)" in to_size


def test_golden_holds_no_resize_in_any_case():
    assert "resize" not in _GOLDEN_PATH.read_text(encoding="utf-8").lower()
    assert "resize" not in _compose().lower()


def test_golden_run_cost_record_holds_no_sized_key_and_counts_by_triage_check():
    script = _compose()
    record = script[script.index("function _runCostRecord()"):]
    record = record[:record.index("\n}")]
    assert "sized" not in record
    assert "by_triage_check: _byTriageCheck" in script


@pytest.mark.parametrize(
    "path,escapes",
    [
        ("/abs/sibling/f.py", True),
        ("C:/x/f.py", True),
        ("C:\\x\\f.py", True),
        ("//server/share/f.py", True),
        ("..\\sibling\\f.py", True),
        ("../sibling/f.py", True),
        ("a/../../f.py", True),
        ("a/b/../f.py", False),
        ("./a/f.py", False),
        ("coordinator_core/f.py", False),
    ],
)
def test_out_of_root_paths_classifier(path, escapes):
    assert gc._out_of_root_paths([path]) == ([path] if escapes else [])


def test_fix_exit_out_of_root_path_hands_back_needs_judgment_without_verify_or_commit():
    seen = []
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (seen.append("fix"), {"outcome": "done", "touched_files": ["/sib/repo/f.py"]})[1],
        "verify": lambda rid: (seen.append("verify"), {"outcome": "pass"})[1],
        "commit": lambda _unit: (seen.append("commit"), {"committed": True})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert seen == ["fix"]
    hb = [h for h in result["handed_back"] if h["type"] == "needs-judgment"]
    assert len(hb) == 1 and "/sib/repo/f.py" in str(hb[0])


def test_fix_exit_in_root_path_routes_unchanged():
    seen = []
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (seen.append("fix"), {"outcome": "done", "touched_files": ["a/f.py"]})[1],
        "verify": lambda rid: (seen.append("verify"), {"outcome": "pass"})[1],
        "commit": lambda _unit: (seen.append("commit"), {"committed": True})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert "verify" in seen
    assert not any(h["type"] == "needs-judgment" for h in result["handed_back"])


def test_fixstage_script_carries_out_of_root_check_before_follow_edge():
    script = _compose()
    assert "function _outOfRootPaths(paths)" in script
    fix_fn = script[script.index("async function _fixStage"): script.index("async function _verifyStage")]
    assert fix_fn.index("_outOfRootPaths(") < fix_fn.index("followEdge(")
    assert "outside repo_root" in fix_fn


def test_fix_prompt_withholds_close_when_tradeoff_reported():
    """A fixer that reports a tradeoff must not archive the row: the
    adjudicator edits `pm_ruling` on the row in place, and
    block_subagent_archive_write denies an edit under archive/."""
    from coordinator_core.ops.dispatch_emit import grind_stages

    call = grind_stages.compose_fix_call(label="fix", phase_title="Grind", row_id="r")
    assert "and `has_tradeoff` is false run `" in call
    assert "When `has_tradeoff` is true, do not run `backlog-grind-assemble grind-row close`" in call


def test_targeted_runner_off_is_the_golden():
    assert _compose(targeted_runner=False) == _GOLDEN_PATH.read_text(encoding="utf-8")


def _forced_line(script: str) -> str:
    verify = script[script.index("async function _verifyCall"): script.index("async function _terminalCommitCall")]
    return next(ln for ln in verify.splitlines() if "const _forced" in ln)


def test_targeted_runner_non_bug_profile_keeps_agent_verdict_on_no_runner():
    assert '["failed", "refused"]' in _forced_line(_compose(targeted_runner=True))


def test_targeted_runner_verify_forces_fail_on_runner_failure_outcomes():
    import dataclasses

    bug = dataclasses.replace(_fixture_profile(), name="bug")
    script = _compose(targeted_runner=True, profile=bug)
    verify = script[script.index("async function _verifyCall"): script.index("async function _terminalCommitCall")]
    assert '["failed", "refused", "no-runner"]' in _forced_line(script)
    assert "outcome: 'fail'" in verify and "tests_failing_ids" in verify
    assert "reason: JSON.stringify(_result.output)" in verify


def test_targeted_runner_script_names_the_runner_and_no_suite_command():
    script = _compose(targeted_runner=True)
    assert "targeted-test-run" in script
    for banned in ("fast_test_cmd", "full_test_cmd", "with-suite-mutex", "tier-u-grant-cli"):
        assert banned not in script


# ---------------------------------------------------------------------------
# One terminal commit: queue, overlap, halt, stamp
# ---------------------------------------------------------------------------


def _run_sink(batches, script_by_kind, *, max_agent_calls=None, **kw):
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()
    sink: list = []
    agent_fn = _agent_stub(script_by_kind, budget)
    try:
        result = gc.run_admission(
            batches, routing, triage_node, reserve=gc.batch_reserve(4), max_agent_calls=max_agent_calls,
            budget=budget, agent=agent_fn, commit_params_sink=sink, **kw,
        )
    except Exception as err:
        return err, sink
    return result, sink


def _fix_script(rows, *, fix=None, declared=None, commit=None):
    declared = declared or {}
    verdicts = {rid: {"verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""} for rid in rows}
    return {
        "triage": lambda bid: [
            {"row": rid, **verdicts[rid], **({"declared_files": declared[rid]} if rid in declared else {})}
            for rid in rows
        ],
        "fix": fix or (lambda rid: {"outcome": "done", "touched_files": [f"src/{rid}.py"]}),
        "verify": lambda rid: {"outcome": "pass"},
        "commit": commit or (lambda _unit: {"committed": True, "sha": "tip"}),
    }


def test_two_non_overlapping_rows_end_with_one_commit_call():
    result, sink = _run_sink([("b0", ["r0", "r1"])], _fix_script(["r0", "r1"]))
    assert [c for c in result["call_log"] if c[0] == "commit"] == [("commit", "run")]
    assert len(sink) == 1
    grind = sink[0]["grind"]
    assert [r["row_id"] for r in grind["rows"]] == ["r0", "r1"]
    assert grind["rows"][0]["paths"] == ["src/r0.py"]
    assert grind["halted"] is None
    assert {s["row"] for s in result["settled"]} == {"r0", "r1"}
    assert all(s["sha"] == "tip" for s in result["settled"])


def test_exhausted_run_still_records_the_commit_with_halted_budget_exhausted():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(4)]
    rows = [f"r{i}" for i in range(4)]
    script = _fix_script(rows)
    script["triage"] = lambda bid: [
        {"row": bid.replace("b", "r"), "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}
    ]
    result, sink = _run_sink(batches, script, max_agent_calls=3)
    assert len(sink) == 1
    assert sink[0]["grind"]["halted"] == "budget-exhausted"
    assert [r["row_id"] for r in sink[0]["grind"]["rows"]] == ["r0"]
    assert any(h["type"] == "budget-exhausted" for h in result["handed_back"])


def test_a_stage_that_raises_after_one_row_finished_still_commits_that_row_with_the_error_as_halted():
    def fix(rid):
        if rid == "r1":
            raise RuntimeError("stage blew up")
        return {"outcome": "done", "touched_files": [f"src/{rid}.py"]}

    err, sink = _run_sink([("b0", ["r0", "r1"])], _fix_script(["r0", "r1"], fix=fix))
    assert isinstance(err, RuntimeError)
    assert len(sink) == 1
    assert sink[0]["grind"]["halted"] == "stage blew up"
    assert [r["row_id"] for r in sink[0]["grind"]["rows"]] == ["r0"]


def test_a_row_declaring_a_queued_rows_path_is_overlap_deferred_with_no_fix_call():
    script = _fix_script(
        ["r0", "r1"],
        declared={"r0": ["shared.py"], "r1": ["shared.py"]},
        fix=lambda rid: {"outcome": "done", "touched_files": ["shared.py"]},
    )
    result, sink = _run_sink([("b0", ["r0", "r1"])], script)
    deferred = [h for h in result["handed_back"] if h["type"] == "overlap-deferred"]
    assert [h["row"] for h in deferred] == ["r1"]
    assert "shared.py" in deferred[0]["reason"]
    assert ("fix", "r1") not in result["call_log"]
    assert [r["row_id"] for r in sink[0]["grind"]["rows"]] == ["r0"]


def test_a_post_fix_collision_hands_both_rows_back_entangled_undone_and_no_commit():
    script = _fix_script(
        ["r0", "r1"],
        declared={"r0": ["a.py"], "r1": ["b.py"]},
        fix=lambda rid: {"outcome": "done", "touched_files": ["shared.py"]},
    )
    script["verify"] = lambda rid: {"outcome": "pass", "undo": {"outcome": "undone"}}
    result, sink = _run_sink([("b0", ["r0", "r1"])], script)
    entangled = {h["row"]: h["reason"] for h in result["handed_back"] if h["type"] == "entangled"}
    assert set(entangled) == {"r0", "r1"}
    assert all("shared.py" in reason and reason.endswith("; undone") for reason in entangled.values())
    assert ("verify", "r0") in result["call_log"] and ("verify", "r1") in result["call_log"]
    assert result["settled"] == []
    assert sink == [] and ("commit", "run") not in result["call_log"]


def test_a_reply_with_one_row_only_in_dropped_hands_that_row_back_and_stamps_the_other():
    dropped = [{"row_id": "r1", "path": "state/queue-grind/p/r1.done.md", "reason": "ledger-root"}]
    reply = {
        "committed": True, "sha": "tip",
        "rows": [{"row_id": "r0", "outcome": "settled", "sha": "tip0"}],
        "dropped": dropped,
    }
    result, _sink = _run_sink([("b0", ["r0", "r1"])], _fix_script(["r0", "r1"], commit=lambda _unit: reply))
    assert result["settled"] == [{"row": "r0", "outcome": "committed", "sha": "tip0"}]
    handed = [h for h in result["handed_back"] if h["row"] == "r1"]
    assert [h["type"] for h in handed] == ["commit-failed"]
    assert "state/queue-grind/p/r1.done.md (ledger-root)" in handed[0]["reason"]
    assert result["terminal_commit_dropped"] == dropped


def test_a_failed_terminal_commit_hands_every_queued_row_back_naming_the_settle_run_recovery():
    reply = {"committed": False, "refused": "no readable pathspec"}
    result, _sink = _run_sink([("b0", ["r0", "r1"])], _fix_script(["r0", "r1"], commit=lambda _unit: reply))
    failed = [h for h in result["handed_back"] if h["type"] == "commit-failed"]
    assert {h["row"] for h in failed} == {"r0", "r1"}
    assert all("no readable pathspec" in h["reason"] and "settle-run" in h["reason"] for h in failed)
    assert result["settled"] == []


def test_no_run_token_hands_the_queue_back_without_calling_the_commit_agent():
    result, sink = _run_sink([("b0", ["r0"])], _fix_script(["r0"]), has_run_token=False)
    assert ("commit", "run") not in result["call_log"] and sink == []
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["commit-failed"]
    assert "settle-run" in result["handed_back"][0]["reason"]


def test_a_200_row_params_block_renders_under_the_spawn_transcript_read_cap():
    import json

    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import (
        READ_CAP_BYTES,
        render_params_block,
    )

    def _params(n, *, sources):
        rows = [
            {
                "row_id": f"2026-10-15-bug-row-{i:04d}-slug",
                "outcome": "settled",
                "paths": [
                    *[f"coordinator_core/ops/mod_{i:04d}_{k}.py" for k in range(sources)],
                    f"archive/2026-10/2026-10-15-bug-row-{i:04d}-slug.yaml",
                ],
                "deleted_paths": [f"state/bug-backlog/2026-10-15-bug-row-{i:04d}-slug.yaml"],
            }
            for i in range(n)
        ]
        return {
            "script_path": "scratch/queue-grind/bug/queue-grind-20261015T180000Z.mjs",
            "run_id": "20261015T180000Z",
            "run_token": "0" * 32,
            "grind": {"profile": "bug", "rows": rows, "halted": None},
        }

    params = _params(200, sources=1)
    block = render_params_block(params)
    assert len(block.encode("utf-8")) < READ_CAP_BYTES
    assert len(json.dumps(block)) < gc.PARAMS_ESCAPED_BUDGET
    assert json.loads(block.splitlines()[1]) == params
    # Rows heavy enough to overflow are the case the emitted budget check hands back, never truncates.
    assert len(json.dumps(render_params_block(_params(200, sources=4)))) > gc.PARAMS_ESCAPED_BUDGET


def test_a_params_block_over_the_transcript_budget_hands_every_row_back_and_calls_no_commit_agent():
    heavy = "x" * 40000
    script = _fix_script(
        ["r0", "r1"], fix=lambda rid: {"outcome": "done", "touched_files": [f"src/{heavy}/{rid}.py"]}
    )
    result, sink = _run_sink([("b0", ["r0", "r1"])], script)
    assert ("commit", "run") not in result["call_log"] and sink == []
    failed = [h for h in result["handed_back"] if h["type"] == "commit-failed"]
    assert {h["row"] for h in failed} == {"r0", "r1"}
    assert all("transcript can carry" in h["reason"] and "settle-run" in h["reason"] for h in failed)
    script_text = _compose()
    assert f"const PARAMS_BUDGET = {gc.PARAMS_ESCAPED_BUDGET};" in script_text
    assert "if (_size > PARAMS_BUDGET) { _failAll(" in script_text


def test_the_composer_and_stages_never_branch_on_the_profile_name():
    here = Path(gc.__file__).parent
    for name in ("grind_compose.py", "grind_stages.py"):
        for lineno, line in enumerate((here / name).read_text(encoding="utf-8").splitlines(), 1):
            if "profile.name ==" in line or "PROFILE_NAME ===" in line:
                assert "RUNNER_FAIL_OUTCOMES" in line, f"{name}:{lineno} branches on the profile name: {line.strip()}"


def test_the_script_has_one_terminal_stage_for_the_fixture_profile():
    script = _compose()
    assert script.count("async function _terminalCommit()") == 1
    assert "@commit" not in script


# ---------------------------------------------------------------------------
# Run close: one record, last, whatever happened before it
# ---------------------------------------------------------------------------


def _run_logged(batches, script_by_kind, **kw):
    """`(result_or_error, kinds_called_in_order, run_close_sink)`."""
    routing, triage_node = _fixture_routing()
    budget = _StubBudgetSpender()
    base = _agent_stub(script_by_kind, budget)
    kinds: list = []
    sink: list = []

    def agent_fn(kind, unit):
        kinds.append(kind)
        return base(kind, unit)

    try:
        result = gc.run_admission(
            batches, routing, triage_node, reserve=gc.batch_reserve(4), budget=budget, agent=agent_fn,
            run_close_sink=sink, **kw,
        )
    except Exception as err:
        return err, kinds, sink
    return result, kinds, sink


def test_a_grind_that_throws_still_records_exactly_one_run_record_last_and_rethrows():
    def fix(rid):
        if rid == "r1":
            raise RuntimeError("stage blew up")
        return {"outcome": "done", "touched_files": [f"src/{rid}.py"]}

    script = _fix_script(["r0", "r1"], fix=fix)
    script.update({"review": lambda u: {}, "adjudicate": lambda u: {}})
    err, kinds, sink = _run_logged([("b0", ["r0", "r1"])], script, post_stages=("review", "adjudicate"))
    assert isinstance(err, RuntimeError)
    assert kinds.count("ledger-sweep") == 1 and kinds[-1] == "ledger-sweep"
    assert "review" not in kinds and "adjudicate" not in kinds
    assert kinds.index("commit") < kinds.index("ledger-sweep")
    assert sink[0]["record"]["halted"] is True and sink[0]["record"]["halted_reason"] == "stage blew up"


def test_a_clean_run_records_once_after_the_review_and_adjudication_calls():
    script = _fix_script(["r0"])
    script.update({"review": lambda u: {}, "adjudicate": lambda u: {}})
    result, kinds, sink = _run_logged([("b0", ["r0"])], script, post_stages=("review",))
    assert kinds[-3:] == ["commit", "review", "ledger-sweep"]
    assert kinds.count("ledger-sweep") == 1 and "run-record" not in kinds and len(sink) == 1
    assert sink[0]["record"]["halted"] is False and sink[0]["record"]["halted_reason"] is None
    assert result["halted"] is False


def test_a_failed_or_throwing_review_stage_still_closes_the_run():
    def review(unit):
        raise RuntimeError("review blew up")

    script = _fix_script(["r0"])
    script.update({"review": review, "adjudicate": lambda u: {}})
    err, kinds, sink = _run_logged([("b0", ["r0"])], script, post_stages=("review", "adjudicate"))
    assert isinstance(err, RuntimeError)
    assert kinds[-1] == "ledger-sweep" and sink[0]["record"]["halted_reason"] == "review blew up"


def test_a_drain_whose_sweep_failed_still_records_and_names_the_sweep_in_record_and_hand_back():
    script = _fix_script(["r0"])
    script["ledger-sweep"] = lambda u: {"outcome": "recorded", "swept": False, "sweep_reason": "ledger locked"}
    result, kinds, sink = _run_logged([("b0", ["r0"])], script)
    assert kinds[-1] == "ledger-sweep" and kinds.count("ledger-sweep") == 1
    for handed in (sink[0]["record"]["handed_back"], result["handed_back"]):
        dead = [h for h in handed if h["type"] == "stage-dead"]
        assert len(dead) == 1 and dead[0]["reason"].startswith(grind_commit_contract.SWEEP_FAILED_HEAD)
        assert "state/queue-grind/profile/runs/20260101T000000Z.json" in dead[0]["reason"]
        assert dead[0]["reason"].endswith(": ledger locked")
    assert sink[0]["record"]["counts"]["by_type"]["stage-dead"] == 1


@pytest.mark.parametrize("raises", ["once", "twice"])
def test_a_drain_that_raises_is_retried_once_and_only_a_second_raise_is_stage_dead(raises):
    calls = {"n": 0}

    def ledger_sweep(unit):
        calls["n"] += 1
        if raises == "twice" or calls["n"] == 1:
            raise RuntimeError("drain exploded")
        return {"outcome": "recorded", "swept": True}

    script = _fix_script(["r0"])
    script["ledger-sweep"] = ledger_sweep
    result, kinds, sink = _run_logged([("b0", ["r0"])], script)
    assert kinds.count("ledger-sweep") == 2 and len(sink) == 1
    dead = [h["reason"] for h in result["handed_back"] if h["type"] == "stage-dead"]
    if raises == "once":
        assert dead == []
    else:
        assert len(dead) == 2
        assert dead[0].startswith(grind_commit_contract.SWEEP_FAILED_HEAD) and dead[0].endswith("drain exploded")
        assert dead[1].startswith("run record may be missing or incomplete") and dead[1].endswith("drain exploded")


def test_a_failed_run_record_is_named_on_the_hand_back_and_never_masks_the_grind_result():
    script = _fix_script(["r0"])
    script["ledger-sweep"] = lambda u: {"outcome": "record-failed", "swept": True, "reason": "commit refused"}
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    dead = [h for h in result["handed_back"] if h["type"] == "stage-dead"]
    assert len(dead) == 1 and "run record may be missing or incomplete" in dead[0]["reason"]
    assert "commit refused" in dead[0]["reason"]


@pytest.mark.parametrize(
    "reply, expect_sweep_dead, expect_record_dead",
    [
        ({"outcome": "record-failed", "swept": False}, True, True),
        ({"outcome": "record-failed"}, False, True),
        ({"outcome": "recorded", "swept": False}, True, False),
    ],
)
def test_a_drain_that_reports_failure_without_a_reason_still_names_one(reply, expect_sweep_dead, expect_record_dead):
    script = _fix_script(["r0"])
    script["ledger-sweep"] = lambda u: reply
    result, _kinds, sink = _run_logged([("b0", ["r0"])], script)
    dead = [h["reason"] for h in result["handed_back"] if h["type"] == "stage-dead"]
    swept = [r for r in dead if r.startswith(grind_commit_contract.SWEEP_FAILED_HEAD)]
    recorded = [r for r in dead if r.startswith("run record may be missing or incomplete")]
    assert len(swept) == int(expect_sweep_dead) and len(recorded) == int(expect_record_dead)
    assert all(r.endswith(gc._NO_SWEEP_REASON) for r in swept)
    assert all(r.endswith(gc._NO_RECORD_REASON) for r in recorded)
    assert gc._NO_SWEEP_REASON in _compose() and gc._NO_RECORD_REASON in _compose()


def _corrupt_then(second):
    calls = {"n": 0}

    def ledger_sweep(unit):
        calls["n"] += 1
        if calls["n"] == 1 or second is None:
            return {
                "outcome": "record-failed", "swept": False,
                "reason": grind_commit_contract.RELAY_CORRUPT_HEAD + ": --record-urlenc arrived as 9 chars",
            }
        return second

    return ledger_sweep


def test_a_relay_corrupt_record_failed_is_redriven_once_and_a_good_second_reply_hands_nothing_back():
    script = _fix_script(["r0"])
    script["ledger-sweep"] = _corrupt_then({"outcome": "recorded", "swept": True})
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    assert kinds.count("ledger-sweep") == 2
    assert [h for h in result["handed_back"] if h["type"] == "stage-dead"] == []


def test_two_relay_corrupt_replies_make_two_calls_and_one_stage_dead_naming_the_head():
    script = _fix_script(["r0"])
    script["ledger-sweep"] = _corrupt_then(None)
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    assert kinds.count("ledger-sweep") == 2
    dead = [h for h in result["handed_back"] if h["type"] == "stage-dead"]
    assert len(dead) == 2 and any(grind_commit_contract.RELAY_CORRUPT_HEAD in h["reason"] for h in dead)


def test_a_record_failed_without_the_relay_head_is_not_redriven():
    script = _fix_script(["r0"])
    script["ledger-sweep"] = lambda u: {"outcome": "record-failed", "swept": True, "reason": "commit refused"}
    _result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    assert kinds.count("ledger-sweep") == 1


def test_the_composed_script_emits_the_sha256_helper_once_and_retries_on_the_contract_head():
    script = _compose()
    assert script.count("function _sha256Ascii(") == 1
    run_close = script[script.index("async function _runClose()"):]
    assert f".includes('{grind_commit_contract.RELAY_CORRUPT_HEAD}')" in run_close
    assert run_close.index(".includes(") < run_close.index("_drained.swept === false")


def test_a_halted_run_without_a_throw_runs_the_close_and_the_record_says_halted():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(4)]
    script = _fix_script([f"r{i}" for i in range(4)])
    script["triage"] = lambda bid: [
        {"row": bid.replace("b", "r"), "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""}
    ]
    result, kinds, sink = _run_logged(batches, script, max_agent_calls=3)
    assert kinds[-1] == "ledger-sweep"
    assert sink[0]["record"]["halted"] is True and sink[0]["record"]["halted_reason"] == "budget-exhausted"
    assert result["halted"] is True


def test_the_run_record_carries_every_required_key_and_the_declared_writes():
    from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract

    script = _fix_script(["r0", "r1"])
    result, _kinds, sink = _run_logged([("b0", ["r0", "r1"])], script)
    record = sink[0]["record"]
    assert set(contract.RUN_RECORD_REQUIRED) <= set(record)
    assert record["profile"] == "profile" and record["run_id"] == "20260101T000000Z"
    assert record["handed_back"] == result["handed_back"]
    assert sink[0]["declared"] == ["r0", "r1"]


def test_a_row_rejected_twice_accumulates_both_attempts_paths_for_the_undo_and_the_commit():
    attempts = {"n": 0}

    def fix(rid):
        attempts["n"] += 1
        return {"outcome": "done", "touched_files": [f"src/attempt{attempts['n']}.py"], "created_files": [f"new{attempts['n']}.py"]}

    verifies = {"n": 0}

    def verify(rid):
        verifies["n"] += 1
        return {"outcome": "fail"} if verifies["n"] == 1 else {"outcome": "pass"}

    script = _fix_script(["r0"], fix=fix)
    script["verify"] = verify
    _result, sink = _run_sink([("b0", ["r0"])], script)
    assert sink[0]["grind"]["rows"][0]["paths"] == ["src/attempt1.py", "src/attempt2.py", "new1.py", "new2.py"]


def test_the_emitted_fix_stage_accumulates_touched_and_created_paths_across_attempts():
    script = _compose()
    fix = script[script.index("async function _fixStage(rowId)"):]
    fix = fix[: fix.index("\n}\n")]
    assert "row.touchedFiles = _union(row.fixReported ? row.touchedFiles : [], result.touched_files); row.fixReported = true;" in fix
    assert "row.createdFiles = _union(row.createdFiles, result.created_files);" in fix
    assert "row.createdFiles = result.created_files || [];" not in fix
    assert "_undoPaths(row)" in script[script.index("async function _verifyCall"):script.index("async function _terminalCommitCall")]


@pytest.mark.parametrize("written", ["state/warp/x.md", "./docs/plans/p.md", "state/sizings/s.yaml"])
def test_a_fix_that_writes_a_historical_record_is_undone_and_handed_back_needs_judgment(written):
    undone: list = []
    script = _fix_script(
        ["r0"], fix=lambda rid: {"outcome": "done", "touched_files": ["src/r0.py"], "created_files": [written]},
    )
    script["verify"] = lambda rid: undone.append(rid) or {"undo": {"outcome": "undone"}}
    result, kinds, sink = _run_logged([("b0", ["r0"])], script)
    assert undone == ["r0"] and kinds.count("verify") == 1 and "commit" not in kinds
    assert "undo" not in kinds
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert [h["type"] for h in handed] == ["needs-judgment"]
    assert written in handed[0]["reason"] and "historical" in handed[0]["reason"]
    assert result["settled"] == []


def test_a_historical_write_whose_undo_fails_is_stage_dead_naming_the_undo_failure():
    script = _fix_script(["r0"], fix=lambda rid: {"outcome": "done", "touched_files": ["state/warp/x.md"]})
    script["verify"] = lambda rid: {"undo": {"outcome": "undo-failed", "reason": "left: state/warp/x.md (peer)"}}
    result, _kinds, _sink = _run_logged([("b0", ["r0"])], script)
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert [h["type"] for h in handed] == ["stage-dead"]
    assert handed[0]["reason"].startswith("undo-failed: the fix wrote historical records")
    assert "left: state/warp/x.md (peer)" in handed[0]["reason"]


def test_a_non_historical_path_that_merely_resembles_a_prefix_is_not_refused():
    assert gc._historical_paths(["state/warp-notes/x.md", "docs/plans.md", "src/state/warp/x.py", "state/warp/x.md"]) == [
        "state/warp/x.md"
    ]


def test_the_emitted_fix_stage_checks_historical_records_before_the_entangle_check():
    script = _compose()
    fix = script[script.index("async function _fixStage(rowId)"):]
    fix = fix[: fix.index("\n}\n")]
    assert fix.index("_historicalPaths(_written)") < fix.index("_entangle(rowId, _written)")
    assert "withLock(lockKeys, async () => _rejectUndoCall(row))" in fix
    assert 'const HISTORICAL_PREFIXES = ["state/warp/", "docs/plans/", "state/handoffs/", "state/sizings/", "docs/decisions/"];' in script


def test_a_fix_tradeoff_is_handed_back_verbatim_and_rides_the_run_record():
    text = "keeps the old retry path; costs one extra spawn"
    script = _fix_script(
        ["r0"], fix=lambda rid: {"outcome": "done", "touched_files": ["src/r0.py"], "has_tradeoff": True, "tradeoff": text},
    )
    script["verify"] = lambda rid: {"undo": {"outcome": "undone"}}
    result, _kinds, sink = _run_logged([("b0", ["r0"])], script)
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert handed[0]["type"] == "needs-judgment" and handed[0]["tradeoff"] == text and text in handed[0]["reason"]
    assert [h for h in sink[0]["record"]["handed_back"] if h["row"] == "r0"][0]["tradeoff"] == text


def test_a_triage_tradeoff_is_handed_back_verbatim_and_rides_the_run_record():
    text = "two valid readings of the row"
    script = _fix_script(["r0"])
    script["triage"] = lambda bid: [
        {"row": "r0", "verdict": "confirmed-bug", "tshirt_size": "S", "has_tradeoff": True, "tradeoff": text}
    ]
    result, _kinds, sink = _run_logged([("b0", ["r0"])], script)
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert handed[0]["type"] == "needs-judgment" and handed[0]["tradeoff"] == text and text in handed[0]["reason"]
    assert sink[0]["record"]["handed_back"][0]["tradeoff"] == text


def test_the_emitted_script_carries_the_tradeoff_on_both_handbacks():
    script = _compose()
    assert "const _entry = tradeoff ? { row: rowId, type, reason, tradeoff } : { row: rowId, type, reason };" in script
    assert "'fix reported a tradeoff: ' + tradeoff, tradeoff)" in script
    assert "`triage reported a tradeoff: ${tradeoff}`, tradeoff)" in script


# ---------------------------------------------------------------------------
# Every non-landing row is undone through the verify-op reject mode
# ---------------------------------------------------------------------------

_WRITES = {"outcome": "done", "touched_files": ["src/r0.py"], "created_files": ["src/new.py"]}


def _undo_script(fix_reply, *, undo, fix_rows=("r0",)):
    """A fix stub returning ``fix_reply`` and a verify stub whose every call is an undo reply."""
    script = _fix_script(list(fix_rows), fix=lambda rid: dict(fix_reply))
    script["verify"] = lambda rid: {"undo": dict(undo)}
    return script


_NONLANDING = [
    ("widen-exhausted", {"outcome": "NEEDS_WIDER_SCOPE"}),
    ("needs-judgment", {"outcome": "done", "has_tradeoff": True, "tradeoff": "trade"}),
    ("baton", {"outcome": "NEEDS_PLAN"}),
    ("manifest-stale", {"outcome": "MANIFEST_STALE"}),
]


@pytest.mark.parametrize(("htype", "fix_reply"), _NONLANDING)
@pytest.mark.parametrize("undone", [True, False])
def test_every_post_fix_hand_back_with_reported_writes_makes_one_reject_undo(htype, fix_reply, undone):
    undo = {"outcome": "undone"} if undone else {"outcome": "undo-failed", "reason": "left: src/r0.py"}
    result, kinds, _sink = _run_logged([("b0", ["r0"])], _undo_script({**_WRITES, **fix_reply}, undo=undo))
    assert kinds.count("verify") == 1 and "commit" not in kinds
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert len(handed) == 1
    if undone:
        assert handed[0]["type"] == htype and handed[0]["reason"].endswith("; undone")
    else:
        assert handed[0]["type"] == "stage-dead"
        assert handed[0]["reason"].startswith(f"undo-failed: the row was handed back {htype}")
        assert "left: src/r0.py" in handed[0]["reason"]
    assert result["settled"] == []


@pytest.mark.parametrize("undone", [True, False])
def test_an_entangled_pair_is_undone_row_by_row(undone):
    undo = {"outcome": "undone"} if undone else {"outcome": "undo-failed"}
    script = _fix_script(
        ["r0", "r1"], declared={"r0": ["a.py"], "r1": ["b.py"]},
        fix=lambda rid: {"outcome": "done", "touched_files": ["shared.py"]},
    )
    script["verify"] = lambda rid: {"outcome": "pass", "undo": dict(undo)}
    result, kinds, _sink = _run_logged([("b0", ["r0", "r1"])], script)
    assert kinds.count("verify") == 3  # r0's own verify stage, then one undo per entangled row
    handed = {h["row"]: h for h in result["handed_back"]}
    expected = "entangled" if undone else "stage-dead"
    assert {h["type"] for h in handed.values()} == {expected}
    if not undone:
        assert all(h["reason"].startswith("undo-failed: the row was handed back entangled") for h in handed.values())


def test_a_row_whose_fix_reported_no_writes_makes_no_undo_call():
    result, kinds, _sink = _run_logged(
        [("b0", ["r0"])], _undo_script({"outcome": "NEEDS_PLAN"}, undo={"outcome": "undone"}),
    )
    assert "verify" not in kinds
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["baton"]


def test_a_commit_failed_row_makes_no_undo_call():
    reply = {"committed": False, "refused": "no readable pathspec"}
    script = _fix_script(["r0"], commit=lambda _unit: reply)
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["commit-failed"]
    assert kinds.count("verify") == 1 and kinds.index("verify") < kinds.index("commit")


def test_a_widen_exhausted_row_undoes_once_after_both_attempts_and_the_script_unions_their_paths():
    attempts = {"n": 0}

    def fix(rid):
        attempts["n"] += 1
        return {
            "outcome": "NEEDS_WIDER_SCOPE", "touched_files": [f"src/t{attempts['n']}.py"],
            "created_files": [f"src/c{attempts['n']}.py"], "extra_files": ["src/wide.py"],
        }

    script = _fix_script(["r0"], fix=fix)
    script["verify"] = lambda rid: {"undo": {"outcome": "undone"}}
    undo_sink: list = []
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script, undo_paths_sink=undo_sink)
    assert attempts["n"] == 2 and kinds.count("verify") == 1
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["widen-exhausted"]
    assert len(undo_sink) == 1 and undo_sink[0]["row"] == "r0"
    assert set(undo_sink[0]["paths"]) == {"src/t1.py", "src/t2.py", "src/c1.py", "src/c2.py"}
    script_js = _compose()
    body = script_js[script_js.index("async function _handBackRow("):]
    body = body[: body.index("\n}\n")]
    assert "_rejectUndoCall(row)" in body
    assert "_undoPaths(row)" in script_js[script_js.index("async function _rejectUndoCall"):script_js.index("async function _handBackRow(")]


def test_an_out_of_root_needs_judgment_row_undoes_only_its_in_root_paths():
    fix = {"outcome": "done", "touched_files": ["/etc/outside.conf"], "created_files": []}
    result, kinds, _sink = _run_logged(
        [("b0", ["r0"])], _undo_script(fix, undo={"outcome": "undone"}),
    )
    assert "verify" not in kinds
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert handed[0]["type"] == "needs-judgment" and "/etc/outside.conf" in handed[0]["reason"]
    mixed = {"outcome": "done", "touched_files": ["/etc/outside.conf", "src/r0.py"]}
    undo_sink: list = []
    _result, kinds, _sink = _run_logged(
        [("b0", ["r0"])], _undo_script(mixed, undo={"outcome": "undone"}), undo_paths_sink=undo_sink,
    )
    assert kinds.count("verify") == 1
    assert [e["paths"] for e in undo_sink if e["row"] == "r0"] == [["src/r0.py"]]
    assert "/etc/outside.conf" not in undo_sink[0]["paths"]
    script_js = _compose()
    undo_paths = script_js[script_js.index("function _undoPaths(row)"):script_js.index("function _hasWrites")]
    assert "_outOfRootPaths([p]).length === 0" in undo_paths


def test_a_row_reaching_stage_dead_through_a_verify_edge_with_writes_is_undone():
    calls = {"n": 0}

    def verify(rid):
        calls["n"] += 1
        return {"outcome": "weird"} if calls["n"] == 1 else {"undo": {"outcome": "undone"}}

    script = _fix_script(["r0"], fix=lambda rid: dict(_WRITES))
    script["verify"] = verify
    result, kinds, _sink = _run_logged([("b0", ["r0"])], script)
    assert kinds.count("verify") == 2
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert handed[0]["type"] == "stage-dead" and handed[0]["reason"].endswith("; undone")


def test_a_thrown_runs_unfinished_row_with_writes_is_undone_after_the_terminal_commit():
    state = {"n": 0}

    def fix(rid):
        state["n"] += 1
        if rid == "r1":
            raise RuntimeError("stage blew up")
        return {"outcome": "done", "touched_files": [f"src/{rid}.py"]}

    script = _fix_script(["r0", "r1"], fix=fix)
    script["verify"] = lambda rid: {"outcome": "pass", "undo": {"outcome": "undone"}}
    err, kinds, _sink = _run_logged([("b0", ["r0", "r1"])], script)
    assert isinstance(err, RuntimeError)
    assert "verify" in kinds
    inner = _compose()
    assert "finally { await _terminalCommit(); await _undoUnfinished(); }" in inner
    assert "run threw: ${_msg}" in inner


def test_the_emitted_fix_stage_has_no_bare_hand_back_for_a_non_landing_row():
    script = _compose()
    fix = script[script.index("async function _fixStage(rowId)"):]
    fix = fix[: fix.index("\n}\n")]
    assert "_handBack(rowId, 'widen-exhausted'" not in fix
    assert "await _handBackRow(rowId, 'widen-exhausted'" in fix
    assert "_handBack(rowId, 'commit-failed'" not in fix
    terminal = script[script.index("async function _terminalCommit()"):]
    assert "_handBack(q.row_id, 'commit-failed'" in terminal and "_handBackRow" not in terminal.split("\n}\n")[0]


# ---------------------------------------------------------------------------
# One definition of "wrote" and of the undo set, in both twins
# ---------------------------------------------------------------------------


def _golden_function(name: str) -> str:
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    start = golden.index(f"function {name}(")
    first_line_end = golden.index("\n", start)
    if golden[start:first_line_end].rstrip().endswith("{"):
        return golden[start: golden.index("\n}\n", start) + 2]
    return golden[start:first_line_end]


def _golden_undo_verdict(row: dict) -> dict:
    """The golden's `_hasWrites`/`_undoPaths` evaluated by node over ``row``."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    js = (
        _golden_function("_outOfRootPaths") + "\n"
        + _golden_function("_undoPaths") + "\n"
        + _golden_function("_hasWrites") + "\n"
        + f"const row = {json.dumps(row)};\n"
        + "console.log(JSON.stringify({ wrote: !!_hasWrites(row), paths: _undoPaths(row) }));"
    )
    out = subprocess.run(
        [node, "-"], input=js, capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_emitted_undo_helpers_read_removed_files_and_never_close_result():
    for name in ("_undoPaths", "_hasWrites"):
        body = _golden_function(name)
        assert "removedFiles" in body and "closeResult" not in body


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    ("py_row", "js_row"),
    [
        (
            dict(removed_files=["state/bug-backlog/gone.yaml"]),
            dict(fixReported=False, touchedFiles=[], createdFiles=[], removedFiles=["state/bug-backlog/gone.yaml"]),
        ),
        (
            dict(removed_files=["state/bug-backlog/a.yaml"], touched_files=["state/archive/a.yaml"]),
            dict(
                fixReported=False, touchedFiles=["state/archive/a.yaml"], createdFiles=[],
                removedFiles=["state/bug-backlog/a.yaml"], closeResult={"old": "x", "new": "y"},
            ),
        ),
        (
            dict(fix_reported=True, touched_files=["src/a.py", "/etc/outside.conf"], removed_files=["../up.yaml"]),
            dict(fixReported=True, touchedFiles=["src/a.py", "/etc/outside.conf"], createdFiles=[], removedFiles=["../up.yaml"]),
        ),
        (dict(), dict(fixReported=False, touchedFiles=[], createdFiles=[], removedFiles=[])),
    ],
)
def test_python_and_emitted_undo_helpers_agree(py_row, js_row):
    row = gc.Row(row_id="r0", path="r0", batch_id="b0", **py_row)
    seen = _golden_undo_verdict(js_row)
    assert seen["wrote"] is gc._has_writes(row)
    assert seen["paths"] == gc._undo_paths(row)


def test_a_removed_only_row_is_a_write_with_an_undo_set():
    row = gc.Row(row_id="r0", path="r0", batch_id="b0", removed_files=["state/bug-backlog/gone.yaml"])
    assert gc._has_writes(row) is True
    assert gc._undo_paths(row) == ["state/bug-backlog/gone.yaml"]


def test_a_peer_dirty_row_with_writes_makes_no_undo_call_and_names_the_paths_left():
    fix = {**_WRITES, "outcome": "PEER_DIRTY"}
    undo_sink: list = []
    result, kinds, _sink = _run_logged(
        [("b0", ["r0"])], _undo_script(fix, undo={"outcome": "undone"}), undo_paths_sink=undo_sink,
    )
    assert "verify" not in kinds and undo_sink == []
    handed = [h for h in result["handed_back"] if h["row"] == "r0"]
    assert [h["type"] for h in handed] == ["peer-dirty"]
    assert "not undone" in handed[0]["reason"]
    assert "src/r0.py" in handed[0]["reason"] and "src/new.py" in handed[0]["reason"]
    assert not handed[0]["reason"].endswith("; undone")


def test_the_emitted_hand_back_tests_the_exempt_set_and_it_holds_both_types():
    script = _compose()
    assert 'const _UNDO_EXEMPT = ["commit-failed", "peer-dirty"];' in script
    body = script[script.index("async function _handBackRow("):]
    body = body[: body.index("\n}\n")]
    assert "_UNDO_EXEMPT.includes(type)" in body and "type === 'commit-failed'" not in body
    assert set(gc._UNDO_EXEMPT) == {"commit-failed", "peer-dirty"}


def test_a_thrown_runs_unfinished_row_whose_only_write_is_a_removed_file_is_undone():
    calls = {"n": 0}

    def verify(rid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("verify blew up")
        return {"undo": {"outcome": "undone"}}

    script = _fix_script(
        ["r0"], fix=lambda rid: {
            "outcome": "done", "close_result": {"old": "state/bug-backlog/r0.yaml", "new": "state/archive/r0.yaml"},
        },
    )
    script["verify"] = verify
    undo_sink: list = []
    err, kinds, _sink = _run_logged([("b0", ["r0"])], script, undo_paths_sink=undo_sink)
    assert isinstance(err, RuntimeError)
    assert undo_sink and set(undo_sink[0]["paths"]) == {"state/bug-backlog/r0.yaml", "state/archive/r0.yaml"}
    assert kinds.count("verify") == 2


# ---------------------------------------------------------------------------
# Replay-stable issue order and the already-landed terminal commit reply
# ---------------------------------------------------------------------------

_REPLAY_HARNESS_JS = r"""
import fs from 'node:fs';
import crypto from 'node:crypto';
const [,, scriptPath, order] = process.argv;
const src = fs.readFileSync(scriptPath, 'utf8').replace(/\r\n/g, '\n').replace(/export const meta = \{[\s\S]*?\n\};\n/, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const canon = (o) => JSON.stringify(o, Object.keys(o || {}).sort());
let chain = '';
const keys = [];
let ledgerPrompt = '';
const rowNo = (prompt) => { const m = /row(\d+)/.exec(prompt); return m ? Number(m[1]) : 0; };
const delayFor = (prompt) => ((rowNo(prompt) < 4 ? 0 : 1) === (order === 'fwd' ? 0 : 1) ? 1 : 25);
const reply = (opts, prompt) => {
  const label = opts.label || '';
  const rows = [...new Set([...prompt.matchAll(/"row_id":"(row\d+)"/g)].map((m) => m[1]))];
  if (label === 'triage') return { rows: rows.map((r) => ({ row: r, verdict: 'confirmed-bug', tshirt_size: 'S', has_tradeoff: false, evidence: 'e', fix_plan: 'p' })), stale: [] };
  if (label === 'fix') return { outcome: 'done', touched_files: ['f' + rowNo(prompt) + '.py'] };
  if (label.startsWith('verify')) return { outcome: 'pass', exit_code: 0, output: {} };
  if (label === 'commit') {
    return {
      committed: true, sha: 'abc123', already_landed: true,
      rows: rows.filter((r) => r !== 'row6').map((r) => ({ row_id: r, outcome: 'settled', sha: 'abc123' })),
      unlanded: rows.includes('row6') ? [{ row_id: 'row6', reason: 'row6 is not in landed commit abc123; it needs a new run' }] : [],
    };
  }
  return {};
};
const agent = (prompt, opts = {}) => {
  const h = crypto.createHash('sha256').update(chain).update('\0').update(prompt).update('\0')
    .update(canon({ schema: opts.schema, model: opts.model, effort: opts.effort, agentType: opts.agentType })).digest('hex');
  if ((opts.label || '').startsWith('ledger-sweep')) ledgerPrompt = prompt;
  chain = h;
  keys.push(opts.label + ':' + h);
  return new Promise((res) => setTimeout(() => res(reply(opts, prompt)), delayFor(prompt)));
};
const budget = { spent: () => 0, remaining: () => null, total: null };
const fn = new AsyncFunction('args', 'agent', 'phase', 'log', 'budget', 'parallel', src);
await fn(
  { run_stamp: '20261017T000000Z', script_path: 'x.mjs', profile_dir: 'pd', repo_root: '/r', run_token: 'tok' },
  agent, () => {}, () => {}, budget, async (xs) => Promise.all(xs),
);
const record = decodeURIComponent(/RUNREC-OPEN-\S+\n\d+ [0-9a-f]{64}\n(\S+)\nRUNREC-CLOSE-/.exec(ledgerPrompt)[1]);
console.log(JSON.stringify({ keys, unlanded_reason_in_record: record.includes('it needs a new run'), landed_sha_in_record: record.includes('abc123') }));
"""


def _run_replay_harness(tmp_path, order: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    script = tmp_path / "grind.mjs"
    script.write_bytes(_compose().encode("utf-8"))
    harness = tmp_path / "harness.mjs"
    harness.write_text(_REPLAY_HARNESS_JS, encoding="utf-8")
    out = subprocess.run(
        [node, str(harness), str(script), order], capture_output=True, text=True, check=False, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_emitted_grind_issues_the_same_agent_call_keys_in_either_completion_order(tmp_path):
    """The runner chains every replay key on the call before it; one recorded result set delivered
    with batch 0 first and with batch 1 first must therefore yield one key sequence."""
    forward = _run_replay_harness(tmp_path, "fwd")["keys"]
    reverse = _run_replay_harness(tmp_path, "rev")["keys"]
    kinds = [k.split(":")[0] for k in forward]
    assert "commit" in kinds and kinds.count("fix") == 7 and kinds[0] == kinds[1] == "triage"
    assert forward == reverse


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_emitted_terminal_commit_settles_an_already_landed_reply_and_hands_back_unlanded(tmp_path):
    seen = _run_replay_harness(tmp_path, "fwd")
    assert seen["landed_sha_in_record"] is True
    assert seen["unlanded_reason_in_record"] is True


def test_every_agent_call_issued_by_a_batch_worker_goes_through_the_issue_gate():
    script = _compose()
    worker_half = script[script.index("function _gateTick()"): script.index("const HANDBACK")]
    for call in ("_triageCall(", "_sizeCall(", "_closeCall(", "_fixCall(", "_rejectUndoCall(", "_verifyCall("):
        for m in re.finditer(re.escape("await " + call), worker_half):
            raise AssertionError(f"ungated worker call at {worker_half[m.start() - 40: m.end() + 40]!r}")
    assert script.count("_issue(") >= 8
    gate = script[script.index("function _issue("): script.index("async function _runBatchWorker")]
    assert "withLock" not in gate


def test_the_issue_gate_sits_outside_the_lock_it_would_otherwise_deadlock_against():
    script = _compose()
    assert "_issue(row.batchId, rowId, () => withLock(lockKeys, async () => _fixCall(row)))" in script
    assert "_issue(row.batchId, rowId, () => withLock(lockKeys, async () => _verifyCall(row)))" in script
    assert "withLock(lockKeys, async () => _issue(" not in script


def test_the_terminal_commit_prompt_and_schema_carry_already_landed_and_unlanded():
    script = _compose()
    call = script[script.index("async function _terminalCommitCall"): script.index("function _counts()")]
    assert grind_commit_contract.ALREADY_LANDED_KEY in call and grind_commit_contract.UNLANDED_KEY in call
    assert f"{grind_commit_contract.ALREADY_LANDED_KEY} and {grind_commit_contract.UNLANDED_KEY}, verbatim" in call


def test_the_emitted_terminal_commit_reads_unlanded_rows_with_the_engines_reason():
    script = _compose()
    body = script[script.index("async function _terminalCommit()"):]
    body = body[: body.index("\n}\n")]
    assert f"reply.{grind_commit_contract.UNLANDED_KEY}" in body
    unlanded_branch = body[body.index("if (!hit && _unlanded[q.row_id])"):]
    unlanded_branch = unlanded_branch[: unlanded_branch.index("continue;")]
    assert "_unlanded[q.row_id].reason" in unlanded_branch
    assert "'commit-failed'" in unlanded_branch and "_unsettle(q.row_id)" in unlanded_branch
    assert "every path of this row was dropped" not in unlanded_branch


def test_the_twin_terminal_commit_hands_an_unlanded_row_back_with_the_engines_reason():
    batches = [("b0", ["r0", "r1"])]
    verdicts = _all_fix_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda _unit: {
            "committed": True, "sha": "abc123", "already_landed": True,
            "rows": [{"row_id": "r0", "outcome": "settled", "sha": "abc123"}],
            "unlanded": [{"row_id": "r1", "reason": "r1 is not in landed commit abc123"}],
        },
    }
    result, _budget = _run(batches, script_by_kind, batch_size=2)
    handed = {h["row"]: h for h in result["handed_back"]}
    assert "r0" not in handed
    assert handed["r1"]["type"] == "commit-failed" and handed["r1"]["reason"].startswith("r1 is not in landed commit abc123")
    assert [s["sha"] for s in result["settled"] if s["row"] == "r0"] == ["abc123"]


def test_refute_close_guard_is_once_per_row_not_once_per_batch():
    """A row reaching a refute-close node after the batch's first close call gets its own call."""
    routing = {
        "triage": {"kind": "triage", "edges": {"confirmed-bug": "fix", "not-reproduced": "close"}, "on_fail": None},
        "fix": {"kind": "fix", "edges": {"done": "close"}, "on_fail": None},
        "close": {"kind": "refute-close", "edges": {"confirmed": "settled-direct", "refuted": "settled-direct"}, "on_fail": None},
    }
    close_calls: list[str] = []

    def close_stub(bid):
        close_calls.append(bid)
        pending = "r0" if len(close_calls) == 1 else "r1"
        return {"confirmed": [{"row": pending, "new_path": f"archive/{pending}.yaml"}], "refuted": []}

    script_by_kind = {
        "triage": lambda bid: [
            {"row": "r0", "verdict": "not-reproduced", "tshirt_size": "S", "tradeoff": ""},
            {"row": "r1", "verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": ""},
        ],
        "fix": lambda rid: {"outcome": "done"},
        "refute-close": close_stub,
        "commit": lambda _unit: {"committed": True, "sha": "abc"},
    }
    budget = _StubBudgetSpender()
    result = gc.run_admission(
        [("b0", ["r0", "r1"])], routing, "triage",
        reserve=gc.batch_reserve(2), budget=budget, agent=_agent_stub(script_by_kind, budget),
    )
    assert close_calls == ["b0", "b0"]
    assert not [h for h in result["handed_back"] if "already called" in h.get("reason", "")]
    assert {h["row"] for h in result["handed_back"] if h["type"] == "settled-direct"} == {"r0", "r1"}


def test_rendered_refute_close_guard_is_keyed_by_row():
    script = _compose()
    assert "closeCalled" not in script
    assert "closedRows.has(rowId)" in script and "closedRows.add(r)" in script


def _retire_triage(verdict, tshirt):
    return lambda batch_id: [{"row": "r0", "verdict": verdict, "tshirt_size": tshirt, "tradeoff": ""}]


def test_retire_verdict_row_skips_baton_and_size_routing():
    for verdict in sorted(vocab.RETIRE_VERDICTS):
        routing = {
            "triage": {"kind": "triage", "edges": {verdict: "fix"}},
            "fix": {"kind": "fix", "edges": {}},
        }
        assert gc.route_after_triage(routing, "triage", verdict, "L", "") == ("node", "fix")
        assert gc.route_after_triage(routing, "triage", "other", "L", "") == ("handback", "baton")


def test_retire_verdict_gate_is_in_the_emitted_script():
    script = _compose()
    assert 'const RETIRE_VERDICTS = ["dedupe", "discard"];' in script
    assert "!RETIRE_VERDICTS.includes(verdict) && _tshirtRank(size) >= PLAN_WEIGHT_FLOOR_RANK" in script


def test_size_fail_reason_names_missing_exit_and_exit_code_with_stderr_tail():
    assert gc.size_fail_reason({}) == "exit missing: the scout ran no size-chain command; (no stderr)"
    assert gc.size_fail_reason({"exit_code": 1, "stderr": "x" * 400}) == "exit 1: " + "x" * 300
    assert gc.size_fail_reason({"reason": "no shell"}) == (
        "exit missing: the scout ran no size-chain command; no shell; (no stderr)"
    )


def test_fix_target_reached_through_verify_nodes_is_found_and_ambiguity_refused():
    routing = {
        "triage": {"kind": "triage", "edges": {"a": "vfix", "b": "vfix", "c": "vclose"}},
        "vfix": {"kind": "verify", "edges": {"pass": "fix"}},
        "vclose": {"kind": "verify", "edges": {"pass": "close"}},
        "close": {"kind": "refute-close", "edges": {}},
        "fix": {"kind": "fix", "edges": {}},
    }
    assert gc.triage_fix_target(routing, "triage") == "vfix"
    routing["triage"]["edges"]["d"] = "fix"
    assert gc.triage_fix_target(routing, "triage") is None


def test_below_floor_size_collapse_only_for_a_row_whose_own_verdict_reaches_fix():
    script = _compose()
    assert "_ownTarget === SIZE_FIX_TARGET" in script
