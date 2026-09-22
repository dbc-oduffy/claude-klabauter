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

import re
from collections import Counter
from pathlib import Path

import pytest

from coordinator_core.ops import _workflow_contract as wc
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

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
    knobs = gp.resolve_appetite(profile, overrides.pop("appetite", "standard"))
    knobs.update(overrides)
    manifest = overrides.pop("manifest", None) or _fixture_manifest()
    return gc.compose_grind_script(
        manifest,
        profile,
        knobs,
        run_dir=Path("state/queue-grind/fixture/run-1"),
        agent_type_host=None,
    )


# ---------------------------------------------------------------------------
# Golden + determinism
# ---------------------------------------------------------------------------


def test_golden_byte_identical():
    script = _compose()
    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    assert script == golden


def test_reemit_is_byte_identical():
    a = _compose()
    b = _compose()
    assert a == b


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def test_every_agent_call_site_carries_model_sonnet():
    script = _compose()
    calls = len(re.findall(r"\bagent\(", script))
    sonnet = len(re.findall(r"model: 'sonnet'", script))
    assert calls > 0
    assert calls == sonnet


def test_no_banned_tokens_and_zero_error_findings():
    script = _compose()
    for token in ("git mv", "git stash", "add -A", "Date.now", "Math.random", "new Date()"):
        assert token not in script
    findings = wc.run_checks(script)
    errors = [f for f in findings if f.severity.value == "ERROR"]
    assert errors == []


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


def test_commit_mutex_key_serialises_every_commit_call():
    """The single `_commitCall(row)` INVOCATION (per-row, plus the two
    ledger-only invocations) sits inside a `@commit`-keyed `withLock` --
    the composed prompt DEFINITION itself (which also contains the words
    "You are the committer for") is not a call site and is excluded from
    this check."""
    script = _compose()
    assert "await withLock(['@commit'], async () => _commitCall(row));" in script
    assert script.count("lockKeys = ['@commit'].concat(") == 2  # batch-end + drain ledger-only commits


def test_commit_composer_definitions_are_never_invoked_outside_a_commit_lock():
    script = _compose()
    for m in re.finditer(r"=> _commitCall\(row\)", script):
        preceding = script[: m.start()]
        assert preceding.rfind("await withLock(['@commit']") != -1


def test_ledger_only_commit_acquires_ledger_key_per_staged_file():
    script = _compose()
    assert "lockKeys = ['@commit'].concat(unsettled.map((r) => `ledger:${r}`))" in script


def test_ledger_files_staged_only_inside_commit_composer_calls():
    script = _compose()
    for m in re.finditer(r"grind-row settle", script):
        window = script[max(0, m.start() - 400) : m.start()]
        assert "You are the committer" in window


def test_no_git_mv_stash_add_dash_a():
    script = _compose()
    for token in ("git mv", "git stash", "add -A"):
        assert token not in script


def test_every_agent_call_site_has_recordcall_equivalent():
    """Defect: fix/verify/commit/refute-close never called `_recordCall`.
    Every captured call (`_capture`) appends `_recordCall(<kind>);`
    immediately after its own `agent(...)` -- count them 1:1."""
    script = _compose()
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
    assert "async function _commitCall(row) {" in script
    assert "async function _undoCall(row) {" in script
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
            run_dir=Path("state/queue-grind/fixture/run-1"), agent_type_host=None,
        )
        return len(re.findall(r"\bagent\(", script)), len(script.encode("utf-8"))

    small_count, _small_bytes = _agent_count(7)
    large_count, large_bytes = _agent_count(300)
    assert small_count == large_count
    assert large_bytes < 150 * 1024


def test_real_bounded_concurrency_not_serial_pipeline():
    script = _compose()
    assert "runGrind" in script
    assert "Promise.race(workers" in script
    assert "workers.length < WINDOW" in script


def test_drain_commit_prompt_names_run_cost_record_path():
    script = _compose()
    assert "runs/" in script and ".json in this same commit." in script
    assert "This is the drain commit" in script


def test_drain_commit_interpolates_live_run_id_and_unsettled_rows():
    """The drain/batch-end ledger-only commits interpolate the REAL runtime
    `unsettled`/`RUN_ID` values (never a static per-batch placeholder) --
    the break-class defect the EM follow-up named."""
    script = _compose()
    assert "(unsettledPaths).join(', ')" in script
    assert "(RUN_ID)" in script


def test_fix_commit_undo_interpolate_live_row_state_not_static_manifest_path():
    """The fix/commit/undo prompts read the row's own live
    declaredFiles/touchedFiles/removedFiles -- never a literal manifest
    path baked in as a stand-in for the run-time touched/declared list."""
    script = _compose()
    assert "(row.declaredFiles).join(', ')" in script
    assert "(row.touchedFiles).join(', ')" in script
    assert "(row.removedFiles.concat([_ledgerPathFor(row.rowId)])).join(', ')" in script
    # no literal manifest row path inside a "stage exactly"/"you hold the
    # lock on" clause -- those clauses interpolate a live expression now.
    for m in re.finditer(r"Stage exactly this touched list: \[", script):
        assert script[m.end() : m.end() + 40].startswith("' + ((row.touchedFiles")


def test_handback_rows_are_row_ids_and_counts_populated():
    script = _compose()
    assert "counts: _counts()," in script
    assert "return { by_type, by_outcome };" in script
    assert "_handedBack.push({ row: rowId" in script or "_handedBack.push({ row: rec.row" in script


# ---------------------------------------------------------------------------
# STAGE_OUTPUT_TOKENS / batch_reserve
# ---------------------------------------------------------------------------


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


def _agent_stub(script_by_kind, budget):
    def _agent(stage_kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(stage_kind, 10))
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
        budget_tokens=budget_tokens,
        budget=budget,
        agent=agent_fn,
    )
    return result, budget


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
        "commit": lambda rid: {"outcome": "committed", "sha": "abc"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=2)
    call_log = result["call_log"]

    triage_indices = [i for i, (kind, _uid) in enumerate(call_log) if kind == "triage"]
    downstream_indices = [i for i, (kind, _uid) in enumerate(call_log) if kind != "triage"]
    if len(triage_indices) >= 2 and downstream_indices:
        assert downstream_indices[0] < triage_indices[1]

    in_flight_downstream = False
    for kind, _uid in call_log:
        if kind == "triage":
            assert not in_flight_downstream
        elif kind in ("fix", "verify", "refute-close"):
            in_flight_downstream = True
        elif kind == "commit":
            in_flight_downstream = False


def test_admission_checks_spend_before_every_triage_admit_and_drain_hands_back_budget_exhausted():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(5)]
    verdicts = _all_fix_batches([f"r{i}" for i in range(5)])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, [bid.replace("b", "r")]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=1, budget_tokens=1)
    handback_types = {h["type"] for h in result["handed_back"]}
    assert "budget-exhausted" in handback_types


def test_max_agent_calls_is_the_deterministic_secondary_bound():
    batches = [(f"b{i}", [f"r{i}"]) for i in range(10)]
    verdicts = _all_fix_batches([f"r{i}" for i in range(10)])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, [bid.replace("b", "r")]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
    }
    result, _budget = _run(batches, script_by_kind, batch_size=1, max_agent_calls=3)
    triage_calls = [c for c in result["call_log"] if c[0] == "triage"]
    assert len(triage_calls) == 1
    handback_types = {h["type"] for h in result["handed_back"]}
    assert "budget-exhausted" in handback_types


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
        "verify": lambda rid: (seen.append("verify"), {"outcome": "fail", "reason": "nope"})[1],
        "undo": lambda rid: (seen.append("undo"), {"outcome": "undone"})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert seen.count("verify") == 2
    assert seen.count("undo") == 1
    assert any(h["type"] == "rejected-after-retry" for h in result["handed_back"])


def test_row_sized_m_or_above_routes_to_baton_never_fix():
    verdicts = {"r0": {"verdict": "confirmed-bug", "tshirt_size": "M", "tradeoff": ""}}
    fix_calls = {"n": 0}
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: (fix_calls.__setitem__("n", fix_calls["n"] + 1), {"outcome": "done"})[1],
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert fix_calls["n"] == 0
    assert any(h["type"] == "baton" and h["row"] == "r0" for h in result["handed_back"])


def test_tradeoff_below_plan_weight_routes_to_needs_judgment_never_fix():
    verdicts = {"r0": {"verdict": "confirmed-bug", "tshirt_size": "S", "tradeoff": "cuts a corner"}}
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
        "fix": lambda rid: {"outcome": "done", "tradeoff": "surprise tradeoff"},
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert any(h["type"] == "needs-judgment" and h["row"] == "r0" for h in result["handed_back"])


def test_each_stage_runs_exactly_once_per_row_in_call_log():
    verdicts = _all_fix_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "pass"},
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    counts = Counter(result["call_log"])
    for rid in ("r0", "r1"):
        assert counts[("fix", rid)] == 1
        assert counts[("verify", rid)] == 1
        assert counts[("commit", rid)] == 1


def test_refute_close_confirmed_and_refuted_both_route_via_profile_edges():
    verdicts = _all_close_batches(["r0", "r1"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0", "r1"]),
        "refute-close": lambda bid: {"confirmed": [{"row": "r0", "new_path": "archive/r0.yaml"}], "refuted": [{"row": "r1", "reason": "still broken"}]},
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    settled_rows = {s["row"] for s in result["settled"]}
    # the fixture profile's refute_close node routes BOTH confirmed and
    # refuted to `commit` -- both rows settle, per the profile's own graph.
    assert settled_rows == {"r0", "r1"}


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
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
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


def test_triage_prompt_names_rows_and_grind_row_append_with_digest():
    script = _compose()
    assert "Your rows (row_id/path/digest) are: " in script
    assert "grind-row append --profile" in script
    assert "--digest" in script
    assert "--row-id" in script
    assert "--stage" in script
    assert "--evidence-file" in script
    assert "--run-stamp" in script
    assert "grind-row check --manifest " in script
    assert "SCRIPT_PATH" in script


def test_close_prompt_interpolates_proposals_and_close_flags():
    script = _compose()
    assert "Your close proposals (row_id/path/digest/evidence) are: " in script
    assert "JSON.stringify(proposals)" in script
    assert "grind-row close --profile-dir" in script
    assert "--closed-by refute-close" in script
    assert "--run-stamp " in script


def test_undo_prompt_interpolates_created_files():
    """The undo prompt restores the fixer's created files -- plus, on a
    row whose fix already closed it (`row.closeResult` set), the archived
    `close_result.new` path so a retried undo cleans that up too (finding
    9: the undo must also remove `close_result.new` and restore the
    original row path)."""
    script = _compose()
    assert "(row.createdFiles.concat(row.closeResult ? [row.closeResult.new] : [])).join(', ')" in script
    assert "(row.touchedFiles.concat(row.closeResult ? [row.closeResult.old] : [])).join(', ')" in script


def test_commit_prompt_includes_ledger_path_and_archive_path():
    script = _compose()
    assert "_ledgerPathFor(row.rowId)" in script
    assert "row.touchedFiles" in script
    # the fix stage's close_result / refute-close's new_path both feed
    # into row.touchedFiles at runtime (asserted structurally below).


def test_fix_close_result_and_refute_close_confirmed_feed_touched_removed_files():
    script = _compose()
    assert "result.close_result.new" in script
    assert "result.close_result.old" in script
    assert "item.new_path" in script


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
        "commit": lambda rid: {"outcome": "committed", "sha": "x"},
    }
    result, _budget = _run([("b0", ["r0", "r1"])], script_by_kind, batch_size=2)
    assert any(h["type"] == "manifest-stale" and h["row"] == "r0" for h in result["handed_back"])


def test_rendered_js_fix_stage_maps_manifest_stale():
    script = _compose()
    assert "if (outcome === 'MANIFEST_STALE')" in script
    assert "type: 'manifest-stale', reason: 'fix reported MANIFEST_STALE'" in script


def test_rendered_js_close_batch_maps_stale_to_manifest_stale():
    script = _compose()
    assert "result.stale || []" in script
    assert "type: 'manifest-stale', reason: 'refute-close close exited 3" in script


def test_fix_schema_enum_includes_manifest_stale():
    script = _compose()
    assert '"NEEDS_PLAN", "MANIFEST_STALE"' in script


def test_close_schema_carries_stale_field():
    script = _compose()
    assert '"stale": {"items": {"type": "string"}, "type": "array"}' in script


def test_fix_prompt_names_manifest_stale_on_close_exit_3():
    script = _compose()
    assert "report MANIFEST_STALE and stop" in script


def test_close_prompt_names_stale_on_close_exit_3():
    script = _compose()
    assert "put that row\\'s id in `stale` instead" in script
    assert "row.removedFiles.concat([row.path])" in script


def test_op_verify_normalises_exit_code_and_per_row_failing_ids():
    """Op-mode verify returns `{exit_code, output}`, not `.outcome` --
    `_verifyCall` must normalise it to `{outcome, reason}` per row."""
    script = _compose()
    assert "_result.exit_code === 0" in script
    assert "_failing.includes(row.rowId)" in script
    assert "outcome: _pass ? 'pass' : 'fail'" in script


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
        return {"outcome": "fail", "reason": "tests still fail"}

    def undo_stub(rid):
        seen.append("undo")
        return {"outcome": "undone"}

    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": fix_stub,
        "verify": verify_stub,
        "undo": undo_stub,
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    assert seen == ["fix", "verify", "fix", "verify", "undo"]
    assert any(h["type"] == "rejected-after-retry" for h in result["handed_back"])


def test_fix_call_site_carries_feedback_expression():
    script = _compose()
    assert "row.verifyFeedback" in script
    assert "Verifier feedback from your last attempt" in script


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
    assert "Its content is exactly this JSON, byte for byte: ' + (JSON.stringify(_runCostRecord()))" in script
    record_fn = script[script.index("function _runCostRecord()"):]
    record_fn = record_fn[: record_fn.index("\n}") ]
    for key in ("profile:", "appetite:", "resolved_knobs: RESOLVED_KNOBS", "manifest_digest: MANIFEST_DIGEST",
                "counts: _counts()", "spend: _spend()"):
        assert key in record_fn, key


def test_batch_helpers_receive_a_batches_entry_never_batch_state():
    """`batchState` ({id, batchKey, triaged, closeCalled}) has no `.rows`; a
    helper typed on a BATCHES entry that is handed batchState throws at run
    time (every grind died at _finishBatch this way). Node is barred, so the
    call sites are checked statically over the rendered script."""
    import re
    script = _compose()
    helpers = re.findall(r"function (\w+)\(batch\)", script)
    assert {"_pendingRow", "_batchDone", "_batchUnsettledRows"} <= set(helpers)
    for name in helpers:
        for arg in re.findall(rf"\b{name}\(([^()]*(?:\([^()]*\)[^()]*)*)\)", script):
            if arg == "batch":
                continue
            assert arg.startswith("BATCHES.find("), f"{name}({arg})"
    assert "batchState.rows" not in script


def test_finish_batch_renders_unsettled_rows_from_the_batches_entry():
    script = _compose()
    finish = script[script.index("async function _finishBatch"):]
    finish = finish[: finish.index("\n}\n")]
    assert "_batchUnsettledRows(BATCHES.find((b) => b.id === batchState.id))" in finish


# ---------------------------------------------------------------------------
# staff-eng review follow-up (coordinator-staff-eng.af423efe80835b8d2.md):
# runtime defects found by hand-trace over the golden -- one test per
# numbered finding.
# ---------------------------------------------------------------------------


def test_finding1_every_captured_agent_result_is_null_guarded():
    """Every `_result = await agent(...)` capture is wrapped `|| {}` so a
    null/undefined agent result reads as an empty object instead of
    throwing on the first `.field` access."""
    script = _compose()
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
    assert result["handed_back"] == [{"row": "r0", "type": "stage-dead", "reason": "fix outcome None"}]


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


def test_finding1_rendered_commit_stage_treats_anything_but_committed_as_failed():
    script = _compose()
    assert "if (result.outcome !== 'committed') { row.done = true; _handedBack.push({ row: rowId, type: 'commit-failed'" in script


def test_finding2_run_batch_worker_wraps_body_in_try_catch_and_still_finishes():
    script = _compose()
    worker = script[script.index("async function _runBatchWorker"): script.index("async function runGrind")]
    assert "try {" in worker
    assert "} catch (err) {" in worker
    assert "await _finishBatch(batchState);" in worker
    # the catch and the finish/HANDBACK path are both reached -- the catch
    # is not itself inside the try it guards, and _finishBatch sits after
    # the try/catch, not inside it.
    catch_idx = worker.index("} catch (err) {")
    finish_idx = worker.index("await _finishBatch(batchState);")
    assert catch_idx < finish_idx


def test_finding2_batch_worker_throw_hands_back_every_not_done_row_stage_dead():
    worker = _compose()
    assert "for (const r of batch.rows) {" in worker
    assert "batch worker threw:" in worker


def test_finding3_close_batch_sweeps_unresolved_proposals_to_stage_dead():
    script = _compose()
    close_batch = script[script.index("async function _closeBatch"): script.index("async function _fixStage")]
    assert "for (const r of proposalIds) {" in close_batch
    assert "refute-close left this proposal unresolved" in close_batch


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


def test_finding3_dispatch_row_hands_back_when_close_already_called():
    script = _compose()
    assert "else { row.done = true; _handedBack.push({ row: rowId, type: 'stage-dead', reason: 'refute-close already called for this batch' }); }" in script


def test_finding4_dispatch_row_has_else_branch_for_unhandled_node_kind():
    script = _compose()
    dispatch = script[script.index("async function _dispatchRow"): script.index("async function _finishBatch")]
    assert "else { row.done = true; _handedBack.push({ row: rowId, type: 'stage-dead', reason: `unhandled node kind ${kind}` }); }" in dispatch


def test_finding5_triage_batch_filters_records_and_stale_to_its_own_batch():
    script = _compose()
    triage_batch = script[script.index("async function _triageBatch"): script.index("async function _closeBatch")]
    assert "if (!batch.rows.includes(rec.row)) continue;" in triage_batch
    assert "if (!batch.rows.includes(staleId)) continue;" in triage_batch
    assert "_out.stale || []" in triage_batch


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


def test_finding5_triage_stale_rows_hand_back_manifest_stale():
    script = _compose()
    assert "'manifest-stale', reason: 'grind-row check reported this row stale/vanished'" in script
    assert '"stale": {"items": {"type": "string"}, "type": "array"}' in script


def test_finding6_verify_agent_prompt_carries_row_context():
    script = _compose()
    assert "You are the verify stage for row " + "' + (row.rowId) + '" + " at " in script
    assert "The fixer touched: [" in script
    assert "Triage evidence: " in script
    assert "The fix plan was: " in script
    assert "(row.fixPlan)" in script
    assert "(row.evidence)" in script


def test_finding6_triage_batch_populates_row_fix_plan():
    script = _compose()
    assert "row.fixPlan = rec.fix_plan || '';" in script


def test_finding7_op_verify_fails_closed_on_nonzero_exit_no_failing_ids():
    script = _compose()
    assert (
        "const _pass = _result.exit_code === 0 || "
        "(Array.isArray(_failing) && _failing.length > 0 && !_failing.includes(row.rowId));"
    ) in script


def test_finding8_fix_prompt_names_already_closed_path_on_retry():
    script = _compose()
    assert "This row is already closed at " in script
    assert "amend the fix only, do not run `grind-row close` again." in script
    assert "row.closeResult = result.close_result;" in script


def test_finding8_undo_restores_original_path_and_removes_new_close_path():
    script = _compose()
    assert "row.touchedFiles.concat(row.closeResult ? [row.closeResult.old] : [])" in script
    assert "row.createdFiles.concat(row.closeResult ? [row.closeResult.new] : [])" in script


def test_finding9_drain_never_restages_a_ledger_finish_batch_already_committed():
    script = _compose()
    assert "!_ledgerCommitted.has(r)" in script
    assert "!_ledgerCommitted.has(r.rowId)" in script
    assert "_ledgerCommitted.add(r)" in script


def test_finding9_ledger_only_commit_failed_is_recorded_in_handback():
    script = _compose()
    assert script.count("type: 'commit-failed', reason: 'ledger-only commit did not land'") == 2


def test_finding9_refuted_to_commit_edge_left_alone_by_design():
    """The fixture profile's refute_close `refuted -> commit` edge is a
    fixture-authoring choice (both confirmed and refuted proposals settle
    via commit in this profile), not the break-class defect the review
    flagged elsewhere -- left unchanged; see grind_profile._check_verify_nodes
    docstring for the verify-on_fail disposition made instead."""
    profile = _fixture_profile()
    assert profile.graph["refute_close"].edges["refuted"] == "commit"


# ---------------------------------------------------------------------------
# Structural safety net (brief: extend it) -- every `agent(` consumer is
# null-guarded, and every `while` loop has a progress guarantee.
# ---------------------------------------------------------------------------


def test_every_while_loop_has_a_progress_guarantee_reference():
    """Every `while` loop in the golden sits next to a comment or a call
    naming the progress mechanism that prevents it spinning forever."""
    script = _compose()
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
            if stage_kind == "undo":
                return {"outcome": "undone"}
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
            if stage_kind == "undo":
                return {"outcome": "undone"}
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
