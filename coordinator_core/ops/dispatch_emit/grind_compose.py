"""
grind_compose — the queue-grind engine's composer: edge interpreter, runtime
mutex, bounded downstream-first admission, budget, hand-back (§ Design §
Composer, docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md's C7 row).

``compose_grind_script`` turns a frozen ``queue_select.Manifest``, a
validated ``grind_profile.Profile`` and resolved appetite knobs into ONE
top-level Workflow ``.mjs`` script -- top-level statements only, never a
wrapper function. ``grind_stages.py`` (C5) supplies every ``agent(...)``
call's text; this module owns the manifest/routing consts, the runtime
mutex, the generic edge interpreter driving those calls PER ROW at run
time, the downstream-first bounded admission gate, the actual-spend
accounting, and the hand-back document.

**Live routing.** ``ROUTING`` is the profile's own graph, rendered once as
data; a small generic interpreter (``routeAfterTriage``/``followEdge`` in
the emitted script, mirrored by ``route_after_triage``/``follow_edge`` in
the pure-Python model below) reads it at RUN time against whatever the
triage/fix/verify/refute-close agent actually returns, per ROW -- two rows
in the same triage batch can diverge to different graph nodes depending
on their own verdict/size/tradeoff.

**Engine-fixed routing** (DR-404 § Run authority, not a profile knob): a
row sized ``>= grind_vocab.PLAN_WEIGHT_FLOOR`` routes to ``baton`` and
never reaches ``fix``; a row below plan weight with a non-empty tradeoff
routes to ``needs-judgment`` (checked at triage-exit AND fix-exit, since a
fixer can surface a tradeoff triage missed); ``NEEDS_PLAN`` maps to
``baton``; ``NEEDS_WIDER_SCOPE`` gets exactly one release-and-reacquire
retry over the union of its locked files plus every extra file, then
``widen-exhausted``; ``PEER_DIRTY`` hands back ``peer-dirty`` directly; a
``verify`` failure gets exactly one retry then ``undo`` then
``rejected-after-retry``; a node's ``on_fail`` back-edge is traversed at
most once per row.

STAGE_OUTPUT_TOKENS / batch_reserve live here, not a separate module.
Calibration source (measured once at authoring time, 2026-09-21): this
session's LOCAL per-agent transcripts under
``subagents/workflows/wf_d3e60811-d4c/`` supply the MEDIAN per-agent
output-token total per label prefix: ``triage`` 5786 (n=371), ``close``
8 (n=5), ``fix`` 4 (n=3). **PARTIAL**: no sample anywhere for
``verify``/``commit``/``undo`` -- those stage kinds are simply ABSENT
below, never an invented value or an import-time raise.
``batch_reserve(batch_size)`` is the dominant measured per-call cost times
batch size -- no ``verdict_mix`` parameter; the reserve stays sound over a
partial map.

**Runtime-interpolated prompts, not a static approximation.**
``grind_stages.py``'s ``compose_fix_call``/``compose_commit_call``/
``compose_undo_call``/``compose_commit_ledger_only_call`` each take an
optional ``*_js`` parameter naming a JS runtime expression; when supplied,
the composed prompt is built as static, escaped literal pieces
concatenated (via ``+``) with that live expression, never a static
per-row manifest-path stand-in. This module always supplies the live
expression: a fix's lock-key/"files you hold" clause reads the row's
runtime ``declaredFiles``; a commit's "stage exactly this touched list"
and declared-deletion clauses read the row's runtime
``touchedFiles``/``removedFiles``; an undo restores the same live
``touchedFiles``; the ledger-only commits (batch-end and drain) read the
live ``unsettled``/``RUN_ID`` in scope at commit time. The mutex lock keys
`withLock` acquires are computed from these SAME live values, so the
actual lock scope and the prompt's own description of it never diverge.

Negative-spec: this module performs no runtime read of any transcript,
file, or clock at import or call time; ``compose_grind_script`` is a pure
function of its arguments. It owns no CLI, no op registration, no disk
write of its own -- ``queue_emit.py`` (C8) writes the returned text.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md
§ Design § Composer, Tasks § C7.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.contract import grind_vocab as vocab
from coordinator_core.ops.dispatch_emit import grind_stages as stages
from coordinator_core.ops.dispatch_emit.emit import _meta_block
from coordinator_core.ops.dispatch_emit.grind_profile import Profile
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry
from coordinator_core.ops.workflow_scaffold import _js_string_literal

__all__ = [
    "STAGE_OUTPUT_TOKENS",
    "batch_reserve",
    "build_routing_table",
    "route_after_triage",
    "resize_verdict_map",
    "follow_edge",
    "Row",
    "run_admission",
    "compose_grind_script",
]


STAGE_OUTPUT_TOKENS: dict[str, int] = {
    "triage": 5786,
    "close": 8,
    "fix": 4,
}

def batch_reserve(batch_size: int) -> int:
    """The fixed per-batch output-token reserve at ``batch_size``.

    Monotone non-decreasing in ``batch_size`` by construction: the dominant
    (highest measured) per-call cost among ``STAGE_OUTPUT_TOKENS`` members
    times ``batch_size`` -- a conservative reserve that stays sound even
    though ``STAGE_OUTPUT_TOKENS`` is a partial map."""
    if batch_size <= 0:
        raise ValueError(f"grind_compose.batch_reserve: batch_size must be positive, got {batch_size!r}")
    dominant = max(STAGE_OUTPUT_TOKENS.values())
    return dominant * batch_size


_TSHIRT_ORDER: tuple[str, ...] = ("XS", "S", "M", "L", "XL", "XXL")

def _tshirt_rank(size: Optional[str]) -> int:
    try:
        return _TSHIRT_ORDER.index(size or "XS")
    except ValueError:
        return 0

_PLAN_WEIGHT_FLOOR_RANK = _tshirt_rank(vocab.PLAN_WEIGHT_FLOOR)

def build_routing_table(profile: Profile) -> dict[str, dict[str, Any]]:
    return {
        node_id: {"kind": node.kind, "edges": dict(node.edges), "on_fail": node.on_fail}
        for node_id, node in profile.graph.items()
    }

def _triage_node_id(profile: Profile) -> str:
    for node_id, node in profile.graph.items():
        if node.kind == "triage":
            return node_id
    raise ValueError("profile graph has no triage node")

def route_after_triage(
    routing: Mapping[str, dict], triage_node_id: str, verdict: str, tshirt_size: Optional[str], tradeoff: str
) -> tuple[str, str]:
    if _tshirt_rank(tshirt_size) >= _PLAN_WEIGHT_FLOOR_RANK:
        return ("handback", "baton")
    if tradeoff:
        return ("handback", "needs-judgment")
    node = routing[triage_node_id]
    target = node["edges"].get(verdict)
    if target is None or target not in routing:
        return ("handback", target or "stage-dead")
    return ("node", target)

def resize_verdict_map(routing: Mapping[str, dict], triage_node_id: str) -> tuple[Optional[str], Optional[str]]:
    edges = routing[triage_node_id]["edges"]
    fix_verdicts = [v for v, t in edges.items() if t in routing and routing[t]["kind"] == "fix"]
    close_verdicts = [v for v, t in edges.items() if t in routing and routing[t]["kind"] == "refute-close"]
    fix_verdict = fix_verdicts[0] if len(fix_verdicts) == 1 else None
    close_verdict = close_verdicts[0] if len(close_verdicts) == 1 else None
    return fix_verdict, close_verdict


def follow_edge(routing: Mapping[str, dict], node_id: str, outcome: str, row: "Row") -> tuple[str, str]:
    node = routing[node_id]
    target = node["edges"].get(outcome)
    if target is None:
        on_fail = node.get("on_fail")
        if on_fail and on_fail not in routing:
            return ("handback", on_fail)
        if on_fail and not row.on_fail_used:
            row.on_fail_used = True
            return ("node", on_fail)
        return ("handback", "stage-dead")
    if target not in routing:
        return ("handback", target)
    return ("node", target)


@dataclass
class Row:
    row_id: str
    path: str
    batch_id: str
    node: Optional[str] = None
    fix_node: Optional[str] = None
    verify_feedback: str = ""
    touched_files: list = field(default_factory=list)
    removed_files: list = field(default_factory=list)
    created_files: list = field(default_factory=list)
    widened: bool = False
    verify_retried: bool = False
    on_fail_used: bool = False
    done: bool = False
    sha: str = ""
    origin: str = ""
    refute_note: str = ""

@dataclass
class _Batch:
    batch_id: str
    row_ids: list[str]
    triaged: bool = False
    close_called: bool = False

def run_admission(
    batches: Sequence[tuple[str, Sequence[str]]],
    routing: Mapping[str, dict],
    triage_node_id: str,
    *,
    reserve: int,
    max_agent_calls: Optional[int] = None,
    budget_tokens: Optional[int] = None,
    budget: Any,
    agent: Callable[[str, str], Any],
) -> dict:
    """Drive the admission policy over ``batches`` (``(batch_id,
    [row_id, ...])`` pairs, in queue order).

    ``budget_tokens``, when given, mirrors the rendered `.mjs`'s
    ``BUDGET_TOKENS`` ceiling: admission halts once the next batch's
    ``reserve`` would carry cumulative spend past it -- independent of
    ``budget.total``/``remaining()`` below.

    ``agent(stage_kind, unit_id) -> result`` is the caller's stub. ``unit_id``
    is a batch id for ``triage``/``refute-close``, a row id for
    ``fix``/``verify``/``commit``/``undo``. It is expected to call
    ``budget.spend(n)`` itself (mirroring the real runtime, where every
    agent call is what advances ``budget.spent()``).

    For ``triage`` the stub returns a list of
    ``{"row": id, "verdict": v, "tshirt_size": s, "tradeoff": t}``. For
    ``refute-close`` it returns ``{"confirmed": [...], "refuted": [{"row":
    id, "reason": r}, ...]}``. For ``fix``/``verify``/``commit`` it returns
    a dict carrying at least ``"outcome"`` (and ``"tradeoff"``/
    ``"extra_files"``/``"sha"`` where relevant).

    Downstream-first, one admitted batch's row work fully drains before the
    next admission decision -- this reference scheduler never holds more
    than one batch open at once, a compliant (if not maximally concurrent)
    instance of "at most WINDOW batches in flight" (no `window` param here:
    a single-batch-at-a-time scheduler is compliant at every window size).
    The rendered `.mjs` (below) implements the SAME admission invariant
    with real bounded concurrency via an async worker pool, since Node
    cannot be executed to verify true interleaving under pytest."""
    start_spent = budget.spent()

    rows: dict[str, Row] = {}
    order: list[str] = []
    batch_objs: dict[str, _Batch] = {}
    queue: list[str] = []
    for batch_id, row_ids in batches:
        batch_objs[batch_id] = _Batch(batch_id=batch_id, row_ids=list(row_ids))
        queue.append(batch_id)
        for rid in row_ids:
            rows[rid] = Row(row_id=rid, path=rid, batch_id=batch_id)
            order.append(rid)

    call_log: list[tuple[str, str]] = []
    calls_by_kind: dict[str, int] = {}
    handed_back: list[dict] = []
    settled: list[dict] = []
    admitted: dict[str, _Batch] = {}
    exhausted = False
    by_refute_origin: dict[str, dict[str, int]] = {}

    def _bump_refute_origin(origin: str, key: str) -> None:
        entry = by_refute_origin.setdefault(origin, {"confirmed": 0, "refuted": 0})
        entry[key] += 1

    def _record_call(kind: str, unit_id: str) -> Any:
        result = agent(kind, unit_id)
        call_log.append((kind, unit_id))
        calls_by_kind[kind] = calls_by_kind.get(kind, 0) + 1
        return result

    def _handback(row: Row, htype: str, reason: str) -> None:
        row.done = True
        if row.refute_note:
            reason = f"{reason} (revived from a refuted close proposal, origin {row.origin!r}: {row.refute_note!r})"
        if any(h["row"] == row.row_id and h["type"] == htype for h in handed_back):
            return
        handed_back.append({"row": row.row_id, "type": htype, "reason": reason})

    def _apply_route(row: Row, action: tuple[str, str], reason: str) -> None:
        kind, value = action
        if kind == "handback":
            _handback(row, value, reason)
        else:
            row.node = value

    def _pending_row(batch: _Batch) -> Optional[Row]:
        for rid in batch.row_ids:
            row = rows[rid]
            if not row.done and row.node is not None:
                return row
        return None

    def _batch_done(batch: _Batch) -> bool:
        return all(rows[rid].done for rid in batch.row_ids)

    def _triage_batch(batch: _Batch) -> None:
        result = _record_call("triage", batch.batch_id)
        batch.triaged = True
        seen: set[str] = set()
        to_resize: list[Row] = []
        for rec in result or []:
            if rec["row"] not in batch.row_ids:
                continue
            row = rows[rec["row"]]
            seen.add(rec["row"])
            row.origin = f"triage:{rec['verdict']}"
            tradeoff = rec.get("tradeoff", "") if rec.get("has_tradeoff") else ""
            action = route_after_triage(
                routing, triage_node_id, rec["verdict"], rec.get("tshirt_size"), tradeoff
            )
            if action == ("handback", "baton"):
                to_resize.append(row)
                continue
            _apply_route(row, action, f"triage verdict {rec['verdict']!r}")
        for rid in batch.row_ids:
            row = rows[rid]
            if rid not in seen and not row.done and row.node is None:
                _handback(row, "stage-dead", "triage never returned a record for this row")
        if to_resize:
            _resize_batch(batch, to_resize)

    def _resize_batch(batch: _Batch, to_resize: list[Row]) -> None:
        fix_verdict, close_verdict = resize_verdict_map(routing, triage_node_id)
        edges = routing[triage_node_id]["edges"]
        fix_target = edges.get(fix_verdict) if fix_verdict else None
        close_target = edges.get(close_verdict) if close_verdict else None
        result = _record_call("resize", batch.batch_id) or {}
        by_row = {a["row"]: a for a in result.get("answers", [])}
        for row in to_resize:
            ans = by_row.get(row.row_id)
            answer = ans.get("answer") if ans else None
            if answer == "focused-fix" and fix_target and fix_target in routing:
                row.origin = "resize:focused-fix"
                row.node = fix_target
            elif answer == "not-reproduced" and close_target and close_target in routing:
                row.origin = "resize:not-reproduced"
                row.node = close_target
            else:
                reason = (ans.get("design_question") if ans else None) or "resize confirmed plan-weight"
                _handback(row, "baton", reason)

    def _close_batch(batch: _Batch) -> None:
        proposal_ids = [
            rid for rid in batch.row_ids if rows[rid].node is not None and not rows[rid].done
        ]
        result = _record_call("refute-close", batch.batch_id) or {}
        batch.close_called = True
        for item in result.get("confirmed", []):
            row = rows[item["row"]]
            if row.node is None or row.done:
                continue
            _bump_refute_origin(row.origin, "confirmed")
            row.removed_files = list(row.removed_files) + [row.path]
            row.touched_files = list(row.touched_files) + [item.get("new_path", "")]
            action = follow_edge(routing, row.node, "confirmed", row)
            _apply_route(row, action, "refute-close confirmed")
            # never silently dropped from `HANDBACK.settled`/`_counts`.
            if action[0] == "handback":
                commit_result = _record_call("commit", row.row_id) or {}
                if commit_result.get("outcome") == "committed":
                    row.sha = commit_result.get("sha", "")
                    settled.append({"row": row.row_id, "outcome": "committed", "sha": row.sha})
                else:
                    _handback(row, "commit-failed", "close's archive-move commit did not land")
        for entry in result.get("refuted", []):
            row = rows[entry["row"]]
            if row.node is None or row.done:
                continue
            _bump_refute_origin(row.origin, "refuted")
            row.refute_note = entry.get("reason", "")
            _apply_route(row, follow_edge(routing, row.node, "refuted", row), "refute-close refuted")
        for stale_row_id in result.get("stale", []):
            row = rows[stale_row_id]
            if row.node is None or row.done:
                continue
            _handback(row, "manifest-stale", "refute-close close exited 3 (digest mismatch)")
        close_node_ids = {nid for nid, node in routing.items() if node["kind"] == "refute-close"}
        for rid in proposal_ids:
            row = rows[rid]
            if not row.done and row.node in close_node_ids:
                _handback(row, "stage-dead", "refute-close left this proposal unresolved (absent from confirmed/refuted/stale)")

    def _fix_row(row: Row) -> None:
        prior_node = row.node
        result = _record_call("fix", row.row_id) or {}
        tradeoff = result.get("tradeoff", "") if result.get("has_tradeoff") else ""
        outcome = result.get("outcome")
        if result.get("touched_files"):
            row.touched_files = list(result["touched_files"])
        row.created_files = list(result.get("created_files") or [])
        close_result = result.get("close_result")
        if outcome == "done" and close_result:
            row.removed_files = list(row.removed_files) + [close_result.get("old", row.path)]
            row.touched_files = list(row.touched_files) + [close_result.get("new", "")]
        if tradeoff:
            _handback(row, "needs-judgment", "fix reported a tradeoff")
            return
        if outcome == "NEEDS_PLAN":
            _handback(row, "baton", "fix reported NEEDS_PLAN")
            return
        if outcome == "PEER_DIRTY":
            _handback(row, "peer-dirty", "fix reported PEER_DIRTY")
            return
        if outcome == "MANIFEST_STALE":
            _handback(row, "manifest-stale", "fix reported MANIFEST_STALE")
            return
        if outcome == "NEEDS_WIDER_SCOPE":
            if not row.widened:
                row.widened = True
                return
            _handback(row, "widen-exhausted", "second NEEDS_WIDER_SCOPE")
            return
        row.fix_node = prior_node
        _apply_route(row, follow_edge(routing, row.node, outcome, row), f"fix outcome {outcome!r}")

    def _verify_row(row: Row) -> None:
        result = _record_call("verify", row.row_id) or {}
        outcome = result.get("outcome")
        if outcome == "fail":
            if not row.verify_retried:
                row.verify_retried = True
                row.verify_feedback = result.get("reason", "")
                row.node = row.fix_node or row.node
                return
            _record_call("undo", row.row_id)
            _handback(row, "rejected-after-retry", "verify failed twice")
            return
        _apply_route(row, follow_edge(routing, row.node, outcome, row), f"verify outcome {outcome!r}")

    def _commit_row(row: Row) -> None:
        result = _record_call("commit", row.row_id) or {}
        if result.get("outcome") != "committed":
            _handback(row, "commit-failed", "commit did not land")
            return
        row.done = True
        row.sha = result.get("sha", "")
        settled.append({"row": row.row_id, "outcome": "committed", "sha": row.sha})

    def _dispatch_row(row: Row, batch: _Batch) -> None:
        kind = routing[row.node]["kind"]
        if kind == "fix":
            _fix_row(row)
        elif kind == "verify":
            _verify_row(row)
        elif kind == "commit":
            _commit_row(row)
        elif kind == "refute-close":
            if not batch.close_called:
                _close_batch(batch)
            else:
                _handback(row, "stage-dead", "refute-close already called for this batch")
        else:
            _handback(row, "stage-dead", f"unhandled node kind {kind!r}")

    def _finish_batch(batch: _Batch) -> None:
        del admitted[batch.batch_id]

    while True:
        finished = [bid for bid, b in admitted.items() if _batch_done(b)]
        if finished:
            _finish_batch(admitted[finished[0]])
            continue

        acted = False
        for batch in list(admitted.values()):
            pending = _pending_row(batch)
            if pending is not None:
                _dispatch_row(pending, batch)
                acted = True
                break
        if acted:
            continue

        untriaged = next((b for b in admitted.values() if not b.triaged), None)
        if untriaged is not None:
            _triage_batch(untriaged)
            continue

        if not queue and not admitted:
            break
        if not queue:
            break

        if max_agent_calls is not None and len(call_log) >= max_agent_calls:
            exhausted = True
            break
        # BUDGET_TOKENS ceiling (a caller-supplied hard cap, distinct from
        if budget_tokens is not None and (budget.spent() - start_spent) + reserve > budget_tokens:
            exhausted = True
            break
        if budget.total is not None and (budget.remaining() or 0) <= reserve:
            exhausted = True
            break

        next_bid = queue.pop(0)
        admitted[next_bid] = batch_objs[next_bid]

    if exhausted:
        for bid in queue:
            for rid in batch_objs[bid].row_ids:
                handed_back.append({"row": rid, "type": "budget-exhausted", "reason": "admission ceiling reached"})

    end_spent = budget.spent()
    return {
        "call_log": call_log,
        "handed_back": handed_back,
        "settled": settled,
        "by_refute_origin": by_refute_origin,
        "spend": {
            "output_tokens": end_spent - start_spent,
            "agent_calls_total": len(call_log),
            "agent_calls_by_stage_kind": calls_by_kind,
        },
    }


def _group_into_batches(manifest: Manifest, knobs: Mapping[str, Any]) -> list[tuple[str, list[ManifestEntry]]]:
    batch_size_knob = knobs.get("batch_size", 4)
    groups: dict[str, list[ManifestEntry]] = {}
    for entry in manifest.entries:
        groups.setdefault(entry.batch_key, []).append(entry)

    batches: list[tuple[str, list[ManifestEntry]]] = []
    for key in sorted(groups):
        entries = groups[key]
        size = (
            batch_size_knob.get(key, batch_size_knob.get("default", 4))
            if isinstance(batch_size_knob, Mapping)
            else batch_size_knob
        )
        size = max(1, int(size))
        for i in range(0, len(entries), size):
            chunk = entries[i : i + size]
            # coordinator_core/bash_guards/_helpers.py::_ILLEGAL_CHARS_ORDER)
            batches.append((f"{key}-b{i // size}", chunk))
    return batches


_NODE_CHECK_COMMENT = (
    "// This script runs inside the Workflow runner, which executes this\n"
    "// file's body directly and permits a top-level `return`. `node --check`\n"
    "// therefore reports `SyntaxError: Illegal return statement` for this\n"
    "// file -- that is not a defect."
)

_MUTEX_HELPERS = (
    "const _locked = new Set();\n"
    "const _waiters = [];\n"
    "function _tryAcquire(keys) {\n"
    "  if (keys.some((k) => _locked.has(k))) return false;\n"
    "  keys.forEach((k) => _locked.add(k));\n"
    "  return true;\n"
    "}\n"
    "function _acquire(keys) {\n"
    "  return new Promise((resolve) => {\n"
    "    if (_tryAcquire(keys)) { resolve(); return; }\n"
    "    _waiters.push({ keys, resolve });\n"
    "  });\n"
    "}\n"
    "function _release(keys) {\n"
    "  keys.forEach((k) => _locked.delete(k));\n"
    "  for (let i = 0; i < _waiters.length; ) {\n"
    "    if (_tryAcquire(_waiters[i].keys)) { const w = _waiters.splice(i, 1)[0]; w.resolve(); }\n"
    "    else i += 1;\n"
    "  }\n"
    "}\n"
    "async function withLock(keys, fn) {\n"
    "  const _keys = Array.from(new Set(keys)).sort();\n"
    "  await _acquire(_keys);\n"
    "  try {\n"
    "    return await fn();\n"
    "  } finally {\n"
    "    _release(_keys);\n"
    "  }\n"
    "}"
)

_ROUTING_HELPERS = (
    "const _TSHIRT_ORDER = ['XS', 'S', 'M', 'L', 'XL', 'XXL'];\n"
    "function _tshirtRank(size) { const i = _TSHIRT_ORDER.indexOf(size || 'XS'); return i < 0 ? 0 : i; }\n"
    "function routeAfterTriage(verdict, size, tradeoff) {\n"
    "  if (_tshirtRank(size) >= PLAN_WEIGHT_FLOOR_RANK) return { kind: 'handback', value: 'baton' };\n"
    "  if (tradeoff) return { kind: 'handback', value: 'needs-judgment' };\n"
    "  const target = ROUTING[TRIAGE_NODE_ID].edges[verdict];\n"
    "  if (!target || !ROUTING[target]) return { kind: 'handback', value: target || 'stage-dead' };\n"
    "  return { kind: 'node', value: target };\n"
    "}\n"
    "function followEdge(nodeId, outcome, row) {\n"
    "  const node = ROUTING[nodeId];\n"
    "  const target = node.edges[outcome];\n"
    "  if (target === undefined) {\n"
    "    if (node.on_fail && !ROUTING[node.on_fail]) return { kind: 'handback', value: node.on_fail };\n"
    "    if (node.on_fail && !row.onFailUsed) { row.onFailUsed = true; return { kind: 'node', value: node.on_fail }; }\n"
    "    return { kind: 'handback', value: 'stage-dead' };\n"
    "  }\n"
    "  if (!ROUTING[target]) return { kind: 'handback', value: target };\n"
    "  return { kind: 'node', value: target };\n"
    "}\n"
    "function applyRoute(row, rowId, route, reason) {\n"
    "  if (route.kind === 'handback') { row.done = true; _handBack(rowId, route.value, reason); }\n"
    "  else { row.node = route.value; }\n"
    "}\n"
    "function _lockKeysFor(row, rowId) { return row.declaredFiles.concat([`ledger:${rowId}`]); }\n"
    "function _commitReason(result) { return result && result.reason ? `: ${result.reason}` : ''; }"
)

def _manifest_const(manifest: Manifest) -> str:
    entries = [
        {"row_id": e.row_id, "path": e.path, "digest": e.digest, "batch_key": e.batch_key}
        for e in manifest.entries
    ]
    declined = [
        {"row_id": d.row_id, "path": d.path, "reason": d.reason} for d in manifest.declined
    ]
    return (
        "const QUEUE_GRIND_MANIFEST = "
        + json.dumps(
            {"entries": entries, "digest": manifest.digest, "declined": declined}, sort_keys=True
        )
        + ";"
    )

def _capture(call_text: str, stage_kind: str, *, return_expr: str = "_result", tail: Optional[str] = None) -> str:
    prefix = "  await agent("
    suffix = ");"
    if not call_text.startswith(prefix) or not call_text.endswith(suffix):
        raise ValueError(f"grind_compose._capture: unexpected composer output shape: {call_text[:40]!r}")
    body = "  const _result = (await agent(" + call_text[len(prefix) : -len(suffix)] + ")) || {};"
    body += f"\n  _recordCall({_js_string_literal(stage_kind)});"
    if tail is not None:
        return body + f"\n{tail}"
    return body + f"\n  return {return_expr};"

def _indent_block(text: str, indent: str) -> str:
    return indent + f"\n{indent}".join(text.splitlines())


def _ledger_commit_block(
    fn_signature: str,
    *,
    unsettled_expr: str,
    is_drain: bool,
    profile_name: str,
    agent_type_host: Optional[str],
) -> str:
    label = "commit-ledger:drain" if is_drain else "commit-ledger:batch"
    raw = stages.compose_commit_ledger_only_call(
        label=label,
        phase_title="Grind",
        profile=profile_name,
        unsettled_row_ids_js="unsettledPaths",
        run_id_js="RUN_ID",
        is_drain=is_drain,
        record_js="JSON.stringify(_runCostRecord())" if is_drain else None,
        repo_root=".",
        agent_type_host=agent_type_host,
    )
    run_record_note = ""
    if is_drain:
        run_record_note = (
            "\n    _handBack(RUN_ID, 'commit-failed', "
            f"`drain commit did not land; run-cost record not written: "
            f"state/queue-grind/{profile_name}/runs/${{RUN_ID}}.json`);"
        )
    call_block = (
        "  const result = await withLock(lockKeys, async () => {\n"
        + _indent_block(_capture(raw, "commit"), "    ")
        + "\n  });\n"
        "  if (result.outcome === 'commit-failed') {\n"
        "    for (const r of unsettled) { _handBack(r, 'commit-failed', 'ledger-only commit did not land -- settle ledgers with grind-row sweep; never commit them'); }"
        + run_record_note + "\n"
        "  } else {\n"
        "    for (const r of unsettled) { _ledgerCommitted.add(r); }\n"
        "  }"
    )
    guarded_call = call_block if is_drain else ("  if (unsettled.length) {\n" + _indent_block(call_block, "  ") + "\n  }")
    body = (
        f"  const unsettled = {unsettled_expr};\n"
        "  const unsettledPaths = unsettled.map((r) => _ledgerPathFor(r));\n"
        "  const lockKeys = ['@commit'].concat(unsettled.map((r) => `ledger:${r}`));\n"
        f"{guarded_call}\n"
    )
    return f"async function {fn_signature} {{\n{body}}}"


def _verify_mode_for_key(profile: Profile, node_id: str, batch_key: str) -> tuple[str, Optional[str]]:
    node = profile.graph[node_id]
    verify_map = node.verify or {}
    mode = verify_map.get(batch_key, verify_map.get("default"))
    if isinstance(mode, Mapping):
        return "op", mode.get("op")
    return (mode or "agent"), None

#: missing arg, or a ``run_stamp`` without the leading ``YYYYMMDD`` that
#: ``carries no YYYYMMDD date`` refusal per close. The emitter prints the
_FIRE_ARGS_CHECK = (
    "if (!args || !args.run_stamp || !args.script_path || !args.profile_dir"
    " || !/^\\d{8}/.test(String(args.run_stamp).replace(/-/g, ''))) {"
    " throw new Error('queue-grind fire args: need {run_stamp, script_path, profile_dir},"
    " run_stamp starting YYYYMMDD (e.g. 20260922T221000Z); got '"
    " + JSON.stringify(args ?? null)"
    " + '. Re-fire with the Workflow call emit-dispatch-workflow printed.'); }"
)


def compose_grind_script(
    manifest: Manifest,
    profile: Profile,
    knobs: Mapping[str, Any],
    *,
    run_dir: Any,
    appetite: str = "standard",
    agent_type_host: Optional[str] = None,
    preamble: Optional[str] = None,
) -> str:
    """Compose one top-level `.mjs` Workflow script implementing § Design §
    Composer over ``manifest``/``profile``/``knobs``. Pure function of its
    arguments: no clock, no random, no disk read. Assumes the Workflow
    runtime provides ``agent``, ``phase``, ``budget`` (``spent()``/``total``/
    ``remaining()``) and ``args`` globals; the runtime mutex is defined
    in-script (no ``lock`` global is assumed).

    Fire-time ``args`` contract (the launcher supplies these; the script
    bakes none of them, so its bytes never vary with the emitting host):
    ``run_stamp`` (run id and the only timestamp), ``script_path`` (this
    script's path, which ``grind-row check --manifest`` reads) and
    ``profile_dir`` (the profile directory, which may live in another repo
    per DR-404's DoE-owned profiles).

    ``preamble`` (optional) is a run-wide posture block declared
    ONCE as a ``const PREAMBLE`` and prepended, at RUN time via a bare
    ``PREAMBLE +`` expression, to every general-purpose (executor-tier)
    stage prompt -- triage, refute-close, resize, fix, verify-agent, undo.
    Never inlined per call site (mirrors the ``_shared`` write-set array's
    own discipline, ``emit.py``'s ``SharedBlocks``), and never spliced into
    ``verify-op`` or ``commit``, which are not executor prompts. A no-op
    when omitted."""
    run_dir_s = Path(run_dir).as_posix()
    grouped = _group_into_batches(manifest, knobs)
    window = int(knobs.get("window", 6))
    batch_size_knob = knobs.get("batch_size", 4)
    reserve_batch_size = max(batch_size_knob.values()) if isinstance(batch_size_knob, Mapping) else batch_size_knob
    max_agent_calls = knobs.get("max_agent_calls")
    budget_tokens = knobs.get("budget_tokens")
    triage_depth_knob = knobs.get("triage_depth", "standard")
    triage_node_id = _triage_node_id(profile)
    node_kind = {node_id: node.kind for node_id, node in profile.graph.items()}
    routing_table = build_routing_table(profile)
    resize_fix_verdict, resize_close_verdict = resize_verdict_map(routing_table, triage_node_id)
    resize_edges = routing_table[triage_node_id]["edges"]
    resize_fix_target = resize_edges.get(resize_fix_verdict) if resize_fix_verdict else None
    resize_close_target = resize_edges.get(resize_close_verdict) if resize_close_verdict else None

    batches_const: list[dict[str, Any]] = [
        {"id": batch_id, "batch_key": entries[0].batch_key, "rows": [e.row_id for e in entries]}
        for batch_id, entries in grouped
    ]
    distinct_keys = sorted({e.batch_key for e in manifest.entries})
    triage_depth_by_key = {
        bk: (
            triage_depth_knob.get(bk, triage_depth_knob.get("default", "standard"))
            if isinstance(triage_depth_knob, Mapping)
            else str(triage_depth_knob)
        )
        for bk in distinct_keys
    }
    verify_node_ids = [node_id for node_id, kind in node_kind.items() if kind == "verify"]
    verify_spec_by_node: dict[str, dict[str, dict[str, Any]]] = {
        node_id: {
            bk: dict(zip(("mode", "op"), _verify_mode_for_key(profile, node_id, bk))) for bk in distinct_keys
        }
        for node_id in verify_node_ids
    }
    has_close_node = any(kind == "refute-close" for kind in node_kind.values())

    lines: list[str] = []
    lines.append(_NODE_CHECK_COMMENT)
    lines.append(_meta_block("queue-grind:" + profile.name, f"Queue grind over profile {profile.name!r}.", ["Grind"]))
    lines.append(_manifest_const(manifest))
    lines.append(
        "const BATCHES = " + json.dumps(batches_const, sort_keys=True) + ";"
    )
    lines.append(
        "const ROUTING = " + json.dumps(routing_table, sort_keys=True) + ";"
    )
    lines.append(f"const TRIAGE_NODE_ID = {_js_string_literal(triage_node_id)};")
    lines.append(f"const PLAN_WEIGHT_FLOOR_RANK = {_PLAN_WEIGHT_FLOOR_RANK};")
    lines.append(f"const RESIZE_FIX_TARGET = {json.dumps(resize_fix_target)};")
    lines.append(f"const RESIZE_CLOSE_TARGET = {json.dumps(resize_close_target)};")
    lines.append(_MUTEX_HELPERS)
    lines.append(_ROUTING_HELPERS)

    lines.append(f"const WINDOW = {window};")
    lines.append(f"const RESERVE = {batch_reserve(reserve_batch_size)};")
    lines.append(f"const MAX_AGENT_CALLS = {json.dumps(max_agent_calls)};")
    lines.append(f"const BUDGET_TOKENS = {json.dumps(budget_tokens)};")
    # properties of undefined` instead of `_FIRE_ARGS_CHECK`'s named,
    lines.append(_FIRE_ARGS_CHECK)
    lines.append(f"const RUN_ID = args.run_stamp;")
    lines.append(f"const SCRIPT_PATH = args.script_path;")
    lines.append(f"const PROFILE_NAME = {_js_string_literal(profile.name)};")
    lines.append("const PROFILE_DIR = args.profile_dir;")
    preamble_expr: Optional[str] = None
    if preamble:
        lines.append(f"const PREAMBLE = {_js_string_literal(preamble)};")
        preamble_expr = "PREAMBLE"
    lines.append(f"const APPETITE_NAME = {_js_string_literal(str(appetite))};")
    lines.append(
        f"const RESOLVED_KNOBS = {json.dumps({k: v for k, v in knobs.items() if k != 'appetite'}, sort_keys=True)};"
    )
    lines.append(f"const MANIFEST_DIGEST = {_js_string_literal(manifest.digest)};")
    lines.append("const TRIAGE_DEPTH_BY_KEY = " + json.dumps(triage_depth_by_key, sort_keys=True) + ";")
    lines.append("const VERIFY_SPEC = " + json.dumps(verify_spec_by_node, sort_keys=True) + ";")
    lines.append(
        "function _ledgerPathFor(rowId) { return `state/queue-grind/${PROFILE_NAME}/${rowId}.jsonl`; }"
    )

    lines.append("let _callCount = 0;")
    lines.append("const _agentCallsByStageKind = {};")
    lines.append(
        "function _recordCall(kind) { _callCount += 1; "
        "_agentCallsByStageKind[kind] = (_agentCallsByStageKind[kind] || 0) + 1; }"
    )
    lines.append("const _startSpent = budget.spent();")
    lines.append("let _exhausted = false;")
    lines.append("const _handedBack = [];")
    lines.append("const _settled = [];")
    lines.append("const _ledgerCommitted = new Set();")
    lines.append("const _byRefuteOrigin = {};")
    lines.append(
        "function _bumpRefuteOrigin(origin, key) {\n"
        "  if (!_byRefuteOrigin[origin]) { _byRefuteOrigin[origin] = { confirmed: 0, refuted: 0 }; }\n"
        "  _byRefuteOrigin[origin][key] += 1;\n"
        "}"
    )
    lines.append(
        "function _handBack(rowId, type, reason) {\n"
        "  const _row = _rows[rowId];\n"
        "  if (_row && _row.refuteNote) {\n"
        "    reason = `${reason} (revived from a refuted close proposal, origin ${_row.origin}: ${_row.refuteNote})`;\n"
        "  }\n"
        "  if (_handedBack.some((h) => h.row === rowId && h.type === type)) return;\n"
        "  _handedBack.push({ row: rowId, type, reason });\n"
        "}\n"
        "function _normalizeRowRef(items, ref) {\n"
        "  const hit = items.find((it) => it.path === ref);\n"
        "  return hit ? hit.row_id : ref;\n"
        "}"
    )

    # params; per-batch-key variation reads TRIAGE_DEPTH_BY_KEY/VERIFY_SPEC.
    triage_raw = stages.compose_triage_call(
        label="triage", phase_title="Grind", run_dir=run_dir_s, profile=profile.name,
        verdicts=profile.verdicts,
        batch_id_js="batchId", triage_depth_js="TRIAGE_DEPTH_BY_KEY[batchKey]",
        rows_js="JSON.stringify(rowsData)", script_path_js="SCRIPT_PATH", run_id_js="RUN_ID",
        agent_type_host=agent_type_host, repo_root=".", preamble_expr=preamble_expr,
    )
    lines.append(
        "async function _triageCall(batchId, batchKey, rowIds) {\n"
        "  const rowsData = rowIds.map((r) => {\n"
        "    const e = QUEUE_GRIND_MANIFEST.entries.find((x) => x.row_id === r);\n"
        "    return { row_id: r, path: e.path, digest: e.digest };\n"
        "  });\n"
        + _indent_block(
            _capture(triage_raw, "triage", return_expr="{ rows: _result.rows || [], stale: _result.stale || [] }"),
            "  ",
        )
        + "\n}"
    )

    resize_raw = stages.compose_resize_call(
        label="resize", phase_title="Grind", profile=profile.name,
        rows_js="JSON.stringify(rowsData)", run_id_js="RUN_ID",
        fix_verdict=resize_fix_verdict, close_verdict=resize_close_verdict,
        agent_type_host=agent_type_host, repo_root=".", preamble_expr=preamble_expr,
    )
    lines.append(
        "async function _resizeCall(rowIds) {\n"
        "  const rowsData = rowIds.map((r) => {\n"
        "    const row = _rows[r];\n"
        "    return { row_id: r, path: row.path, digest: row.digest };\n"
        "  });\n"
        + _indent_block(_capture(resize_raw, "resize", return_expr="_result.answers || []"), "  ")
        + "\n}"
    )

    if has_close_node:
        close_raw = stages.compose_refute_close_call(
            label="close", phase_title="Grind", profile=profile.name,
            profile_dir_js="PROFILE_DIR",
            proposals_js="JSON.stringify(proposals)", run_id_js="RUN_ID", agent_type_host=agent_type_host,
            repo_root=".", preamble_expr=preamble_expr,
        )
        lines.append(
            "async function _closeCall(proposals) {\n" + _indent_block(_capture(close_raw, "refute-close"), "  ") + "\n}"
        )

    fix_raw = stages.compose_fix_call(
        label="fix", phase_title="Grind", row_id_js="row.rowId", locked_files_js="row.declaredFiles",
        feedback_js="(row.verifyFeedback ? (' Verifier feedback from your last attempt: ' + row.verifyFeedback) : '')",
        close_note_js=(
            "(row.closeResult ? (' This row is already closed at ' + row.closeResult.new + "
            "'; amend the fix only, do not run `backlog-grind-assemble grind-row close` again.') : '')"
        ),
        refute_note_js=(
            "(row.refuteNote ? (' The refuter judged this defect live at HEAD; reason: ' + "
            "row.refuteNote) : '')"
        ),
        profile=profile.name, profile_dir_js="PROFILE_DIR", row_path_js="row.path", digest_js="row.digest",
        run_id_js="RUN_ID", agent_type_host=agent_type_host, preamble_expr=preamble_expr,
    )
    lines.append("async function _fixCall(row) {\n" + _indent_block(_capture(fix_raw, "fix"), "  ") + "\n}")

    verify_agent_raw = stages.compose_verify_agent_call(
        label="verify-agent", phase_title="Grind",
        row_id_js="row.rowId", row_path_js="row.path", touched_files_js="row.touchedFiles",
        evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        agent_type_host=agent_type_host, preamble_expr=preamble_expr,
    )
    verify_op_raw = stages.compose_verify_op_call(
        label="verify-op", phase_title="Grind", run_dir=run_dir_s, op_js="spec.op", batch_id_js="row.batchId",
        agent_type_host=agent_type_host,
    )
    lines.append(
        "async function _verifyCall(row) {\n"
        "  const spec = ((VERIFY_SPEC[row.node] || {})[row.batchKey]) || { mode: 'agent', op: null };\n"
        "  if (spec.mode === 'op') {\n"
        + _indent_block(
            _capture(
                verify_op_raw, "verify",
                tail=(
                    "  const _failing = (_result.output && _result.output.failing_ids) || [];\n"
                    "  const _pass = _result.exit_code === 0 || "
                    "(Array.isArray(_failing) && _failing.length > 0 && "
                    "!_failing.includes(row.rowId) && !_failing.includes(row.path));\n"
                    "  return { outcome: _pass ? 'pass' : 'fail', reason: JSON.stringify(_result.output) };"
                ),
            ),
            "    ",
        )
        + "\n  }\n"
        + _indent_block(_capture(verify_agent_raw, "verify"), "  ")
        + "\n}"
    )

    commit_raw = stages.compose_commit_call(
        label="commit", phase_title="Grind", profile=profile.name, row_id_js="row.rowId",
        outcome_js="row.lastOutcome || 'settled'",
        touched_files_js="row.touchedFiles", removed_files_js="row.removedFiles.concat([_ledgerPathFor(row.rowId)])",
        repo_root=".", agent_type_host=agent_type_host,
    )
    lines.append("async function _commitCall(row) {\n" + _indent_block(_capture(commit_raw, "commit"), "  ") + "\n}")

    undo_raw = stages.compose_undo_call(
        label="undo", phase_title="Grind",
        touched_files_js="row.touchedFiles.concat(row.closeResult ? [row.closeResult.old] : [])",
        created_files_js="row.createdFiles.concat(row.closeResult ? [row.closeResult.new] : [])",
        agent_type_host=agent_type_host, preamble_expr=preamble_expr,
    )
    lines.append("async function _undoCall(row) {\n" + _indent_block(_capture(undo_raw, "undo"), "  ") + "\n}")

    lines.append(
        "function _counts() {\n  const by_type = {};\n  for (const h of _handedBack) { by_type[h.type] = (by_type[h.type] || 0) + 1; }\n  const by_outcome = {};\n  for (const s of _settled) { by_outcome[s.outcome] = (by_outcome[s.outcome] || 0) + 1; }\n  return { by_type, by_outcome, by_refute_origin: _byRefuteOrigin };\n}\nfunction _spend() {\n  return { output_tokens: budget.spent() - _startSpent, agent_calls_total: _callCount, agent_calls_by_stage_kind: _agentCallsByStageKind };\n}\nfunction _runCostRecord() {\n  return { profile: PROFILE_NAME, appetite: APPETITE_NAME, run_id: RUN_ID, resolved_knobs: RESOLVED_KNOBS, manifest_digest: MANIFEST_DIGEST, counts: _counts(), spend: _spend() };\n}"
    )
    lines.append(
        "const _rows = {};\n"
        "for (const b of BATCHES) {\n"
        "  for (const r of b.rows) {\n"
        "    const _entry = QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r);\n"
        "    const _path = _entry.path;\n"
        "    _rows[r] = { rowId: r, batchId: b.id, batchKey: b.batch_key, path: _path, digest: _entry.digest, node: null, "
        "declaredFiles: [_path], touchedFiles: [_path], removedFiles: [], createdFiles: [], "
        "evidence: '', verifyFeedback: '', fixPlan: '', fixNode: null, closeResult: null, lastOutcome: '', "
        "origin: '', refuteNote: '', "
        "widened: false, verifyRetried: false, onFailUsed: false, done: false, sha: '' };\n"
        "  }\n"
        "}"
    )

    lines.append(
        "function _pendingRow(batch) {\n"
        "  for (const r of batch.rows) { const row = _rows[r]; if (!row.done && row.node) return r; }\n"
        "  return null;\n"
        "}\n"
        "function _batchDone(batch) { return batch.rows.every((r) => _rows[r].done); }\n"
        "function _batchUnsettledRows(batch) { return batch.rows.filter((r) => _rows[r].done && "
        "!_settled.some((s) => s.row === r) && !_ledgerCommitted.has(r) && !_rows[r].closeResult); }"
    )

    lines.append(
        "async function _triageBatch(batchState) {\n"
        "  const batch = BATCHES.find((b) => b.id === batchState.id);\n"
        "  const rowsMeta = batch.rows.map((r) => "
        "({ row_id: r, path: (QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r) || {}).path }));\n"
        "  const _out = await _triageCall(batchState.id, batchState.batchKey, batch.rows);\n"
        "  batchState.triaged = true;\n"
        "  const _seen = new Set();\n"
        "  const _toResize = [];\n"
        "  for (const rec of (_out.rows || [])) {\n"
        "    const recRow = _normalizeRowRef(rowsMeta, rec.row);\n"
        "    if (!batch.rows.includes(recRow)) continue;\n"
        "    const row = _rows[recRow];\n"
        "    if (!row) continue;\n"
        "    _seen.add(recRow);\n"
        "    row.evidence = rec.evidence || '';\n"
        "    row.fixPlan = rec.fix_plan || '';\n"
        "    if (rec.declared_files && rec.declared_files.length) { row.declaredFiles = rec.declared_files; }\n"
        "    row.origin = `triage:${rec.verdict}`;\n"
        "    const tradeoff = rec.has_tradeoff ? (rec.tradeoff || '') : '';\n"
        "    const route = routeAfterTriage(rec.verdict, rec.tshirt_size, tradeoff);\n"
        "    if (route.kind === 'handback' && route.value === 'baton') { _toResize.push(recRow); continue; }\n"
        "    applyRoute(row, recRow, route, `triage verdict ${rec.verdict}`);\n"
        "  }\n"
        "  for (const staleRef of (_out.stale || [])) {\n"
        "    const staleId = _normalizeRowRef(rowsMeta, staleRef);\n"
        "    if (!batch.rows.includes(staleId)) continue;\n"
        "    const row = _rows[staleId];\n"
        "    if (!row || row.done || _seen.has(staleId)) continue;\n"
        "    _seen.add(staleId);\n"
        "    row.done = true;\n"
        "    _handBack(staleId, 'manifest-stale', 'grind-row check reported this row stale/vanished');\n"
        "  }\n"
        "  for (const r of batch.rows) {\n"
        "    if (!_seen.has(r) && !_rows[r].done && !_rows[r].node) {\n"
        "      _rows[r].done = true;\n"
        "      _handBack(r, 'stage-dead', 'triage never returned a record for this row');\n"
        "    }\n"
        "  }\n"
        "  if (_toResize.length) {\n"
        "    const _answers = await _resizeCall(_toResize);\n"
        "    const _byRow = {};\n"
        "    for (const a of _answers) { _byRow[a.row] = a; }\n"
        "    for (const rid of _toResize) {\n"
        "      const row = _rows[rid];\n"
        "      const a = _byRow[rid];\n"
        "      const answer = a && a.answer;\n"
        "      if (answer === 'focused-fix' && RESIZE_FIX_TARGET && ROUTING[RESIZE_FIX_TARGET]) {\n"
        "        row.origin = 'resize:focused-fix';\n"
        "        row.node = RESIZE_FIX_TARGET;\n"
        "      } else if (answer === 'not-reproduced' && RESIZE_CLOSE_TARGET && ROUTING[RESIZE_CLOSE_TARGET]) {\n"
        "        row.origin = 'resize:not-reproduced';\n"
        "        row.node = RESIZE_CLOSE_TARGET;\n"
        "      } else {\n"
        "        row.done = true;\n"
        "        _handBack(rid, 'baton', (a && a.design_question) || 'resize confirmed plan-weight');\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )

    lines.append(
        "async function _closeBatch(batchState) {\n"
        "  const closeNodeIds = Object.keys(ROUTING).filter((n) => ROUTING[n].kind === 'refute-close');\n"
        "  const batch = BATCHES.find((b) => b.id === batchState.id);\n"
        "  const proposalIds = batch.rows.filter((r) => closeNodeIds.includes(_rows[r].node));\n"
        "  const proposals = proposalIds.map((r) => {\n"
        "    const row = _rows[r];\n"
        "    return { row_id: r, path: row.path, digest: (QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r) || {}).digest, evidence: row.evidence, origin: row.origin };\n"
        "  });\n"
        "  const result = await _closeCall(proposals);\n"
        "  batchState.closeCalled = true;\n"
        "  for (const item of (result.confirmed || [])) {\n"
        "    const itemRow = _normalizeRowRef(proposals, item.row);\n"
        "    const row = _rows[itemRow];\n"
        "    if (!row || row.done || !row.node) continue;\n"
        "    _bumpRefuteOrigin(row.origin, 'confirmed');\n"
        "    row.removedFiles = row.removedFiles.concat([row.path]);\n"
        "    row.touchedFiles = row.touchedFiles.concat([item.new_path]);\n"
        "    row.closeResult = { old: row.path, new: item.new_path };\n"
        "    row.lastOutcome = 'confirmed';\n"
        "    const route = followEdge(row.node, 'confirmed', row);\n"
        "    applyRoute(row, itemRow, route, 'refute-close confirmed');\n"
        "    if (route.kind === 'handback') {\n"
        "      const _cresult = await withLock(['@commit'], async () => _commitCall(row));\n"
        "      if (_cresult.outcome !== 'committed') { _handBack(itemRow, 'commit-failed', \"close's archive-move commit did not land\" + _commitReason(_cresult) + ' -- settle ledgers with grind-row sweep; never commit them'); }\n"
        "      else { row.sha = _cresult.sha || ''; _ledgerCommitted.add(itemRow); "
        "_settled.push({ row: itemRow, outcome: 'committed', sha: row.sha }); }\n"
        "    }\n"
        "  }\n"
        "  for (const entry of (result.refuted || [])) {\n"
        "    const entryRow = _normalizeRowRef(proposals, entry.row);\n"
        "    const row = _rows[entryRow];\n"
        "    if (!row || row.done || !row.node) continue;\n"
        "    _bumpRefuteOrigin(row.origin, 'refuted');\n"
        "    row.refuteNote = entry.reason || '';\n"
        "    row.lastOutcome = 'refuted';\n"
        "    applyRoute(row, entryRow, followEdge(row.node, 'refuted', row), 'refute-close refuted');\n"
        "  }\n"
        "  for (const staleRef of (result.stale || [])) {\n"
        "    const staleId = _normalizeRowRef(proposals, staleRef);\n"
        "    const row = _rows[staleId];\n"
        "    if (!row || row.done || !row.node) continue;\n"
        "    row.done = true; _handBack(staleId, 'manifest-stale', 'refute-close close exited 3 (digest mismatch)');\n"
        "  }\n"
        "  for (const r of proposalIds) {\n"
        "    const row = _rows[r];\n"
        "    if (!row.done && closeNodeIds.includes(row.node)) {\n"
        "      row.done = true;\n"
        "      _handBack(r, 'stage-dead', 'refute-close left this proposal unresolved (absent from confirmed/refuted/stale)');\n"
        "    }\n"
        "  }\n"
        "}"
    )

    lines.append(
        "async function _fixStage(rowId) {\n"
        "  const row = _rows[rowId];\n"
        "  const lockKeys = _lockKeysFor(row, rowId);\n"
        "  const _priorNode = row.node;\n"
        "  const result = await withLock(lockKeys, async () => _fixCall(row));\n"
        "  const tradeoff = result.has_tradeoff ? (result.tradeoff || '') : '';\n"
        "  const outcome = result.outcome;\n"
        "  row.lastOutcome = outcome;\n"
        "  if (result.touched_files && result.touched_files.length) { row.touchedFiles = result.touched_files; }\n"
        "  else if (outcome === 'done') { row.touchedFiles = row.declaredFiles; }\n"
        "  row.createdFiles = result.created_files || [];\n"
        "  if (outcome === 'done' && result.close_result) {\n"
        "    row.removedFiles = row.removedFiles.concat([result.close_result.old || row.path]);\n"
        "    row.touchedFiles = row.touchedFiles.concat([result.close_result.new]);\n"
        "    row.closeResult = result.close_result;\n"
        "  }\n"
        "  if (tradeoff) { row.done = true; _handBack(rowId, 'needs-judgment', 'fix reported a tradeoff'); return; }\n"
        "  if (outcome === 'NEEDS_PLAN') { row.done = true; _handBack(rowId, 'baton', 'fix reported NEEDS_PLAN'); return; }\n"
        "  if (outcome === 'PEER_DIRTY') { row.done = true; _handBack(rowId, 'peer-dirty', 'fix reported PEER_DIRTY'); return; }\n"
        "  if (outcome === 'MANIFEST_STALE') { row.done = true; _handBack(rowId, 'manifest-stale', 'fix reported MANIFEST_STALE'); return; }\n"
        "  if (outcome === 'NEEDS_WIDER_SCOPE') {\n"
        "    if (!row.widened) { row.widened = true; row.declaredFiles = row.declaredFiles.concat(result.extra_files || []); return; }\n"
        "    row.done = true; _handBack(rowId, 'widen-exhausted', 'second NEEDS_WIDER_SCOPE'); return;\n"
        "  }\n"
        "  row.fixNode = _priorNode;\n"
        "  applyRoute(row, rowId, followEdge(row.node, outcome, row), `fix outcome ${outcome}`);\n"
        "}"
    )

    lines.append(
        "async function _verifyStage(rowId) {\n"
        "  const row = _rows[rowId];\n"
        "  const lockKeys = _lockKeysFor(row, rowId);\n"
        "  const result = await withLock(lockKeys, async () => _verifyCall(row));\n"
        "  if (result.outcome === 'fail') {\n"
        "    if (!row.verifyRetried) {\n"
        "      row.verifyRetried = true;\n"
        "      row.verifyFeedback = result.reason || '';\n"
        "      row.node = row.fixNode || row.node;\n"
        "      return;\n"
        "    }\n"
        "    await withLock(lockKeys, async () => _undoCall(row));\n"
        "    row.done = true; _handBack(rowId, 'rejected-after-retry', 'verify failed twice'); return;\n"
        "  }\n"
        "  applyRoute(row, rowId, followEdge(row.node, result.outcome, row), `verify outcome ${result.outcome}`);\n"
        "}"
    )

    lines.append(
        "async function _commitStage(rowId) {\n"
        "  const row = _rows[rowId];\n"
        "  const result = await withLock(['@commit'], async () => _commitCall(row));\n"
        "  if (result.outcome !== 'committed') { row.done = true; _handBack(rowId, 'commit-failed', 'commit did not land' + _commitReason(result) + ' -- settle ledgers with grind-row sweep; never commit them'); return; }\n"
        "  row.done = true; row.sha = result.sha || '';\n"
        "  _settled.push({ row: rowId, outcome: 'committed', sha: row.sha });\n"
        "}"
    )

    lines.append(
        "async function _dispatchRow(rowId, batchState) {\n"
        "  const row = _rows[rowId];\n"
        "  const kind = ROUTING[row.node].kind;\n"
        "  if (kind === 'fix') { await _fixStage(rowId); }\n"
        "  else if (kind === 'verify') { await _verifyStage(rowId); }\n"
        "  else if (kind === 'commit') { await _commitStage(rowId); }\n"
        "  else if (kind === 'refute-close') {\n"
        "    if (!batchState.closeCalled) { await _closeBatch(batchState); }\n"
        "    else { row.done = true; _handBack(rowId, 'stage-dead', 'refute-close already called for this batch'); }\n"
        "  }\n"
        "  else { row.done = true; _handBack(rowId, 'stage-dead', `unhandled node kind ${kind}`); }\n"
        "}"
    )

    lines.append(
        _ledger_commit_block(
            "_finishBatch(batchState)",
            unsettled_expr="_batchUnsettledRows(BATCHES.find((b) => b.id === batchState.id))",
            is_drain=False,
            profile_name=profile.name,
            agent_type_host=agent_type_host,
        )
    )
    lines.append(
        _ledger_commit_block(
            "_drainCommit()",
            unsettled_expr="Object.values(_rows).filter((r) => r.done && "
            "!_settled.some((s) => s.row === r.rowId) && !_ledgerCommitted.has(r.rowId) && "
            "!r.closeResult).map((r) => r.rowId)",
            is_drain=True,
            profile_name=profile.name,
            agent_type_host=agent_type_host,
        )
    )

    lines.append(
        "const _admitted = {};\n"
        "const _queue = BATCHES.map((b) => b.id);\n"
        "function _anyAdmittedHasPendingDownstream() {\n"
        "  return Object.values(_admitted).some((bs) => !_batchDone(BATCHES.find((b) => b.id === bs.id)) && "
        "_pendingRow(BATCHES.find((b) => b.id === bs.id)) !== null);\n"
        "}\n"
        "async function _runBatchWorker(batchState) {\n"
        "  const batch = BATCHES.find((b) => b.id === batchState.id);\n"
        "  try {\n"
        "    if (!batchState.triaged) { await _triageBatch(batchState); }\n"
        "    while (!_batchDone(batch)) {\n"
        "      const pending = _pendingRow(batch);\n"
        "      if (pending === null) break;\n"
        "      await _dispatchRow(pending, batchState);\n"
        "    }\n"
        "  } catch (err) {\n"
        "    const _msg = err && err.message ? err.message : String(err);\n"
        "    for (const r of batch.rows) {\n"
        "      if (!_rows[r].done) { _rows[r].done = true; _handBack(r, 'stage-dead', `batch worker threw: ${_msg}`); }\n"
        "    }\n"
        "  }\n"
        "  await _finishBatch(batchState);\n"
        "  delete _admitted[batchState.id];\n"
        "}\n"
        "async function runGrind() {\n"
        "  const workers = [];\n"
        "  while (_queue.length || workers.length) {\n"
        "    if (_queue.length && workers.length < WINDOW && !_anyAdmittedHasPendingDownstream()) {\n"
        "      if (MAX_AGENT_CALLS !== null && _callCount >= MAX_AGENT_CALLS) { _exhausted = true; }\n"
        "      else {\n"
        "        const _spentDelta = budget.spent() - _startSpent;\n"
        "        if (BUDGET_TOKENS !== null && _spentDelta + RESERVE > BUDGET_TOKENS) { _exhausted = true; }\n"
        "        else if (budget.total !== undefined && budget.total !== null && (budget.remaining() ?? 0) <= RESERVE) { _exhausted = true; }\n"
        "      }\n"
        "      if (!_exhausted) {\n"
        "        const bid = _queue.shift();\n"
        "        const batchState = { id: bid, batchKey: BATCHES.find((b) => b.id === bid).batch_key, triaged: false, closeCalled: false };\n"
        "        _admitted[bid] = batchState;\n"
        "        workers.push(_runBatchWorker(batchState));\n"
        "        continue;\n"
        "      }\n"
        "    }\n"
        "    if (!workers.length) break;\n"
        "    const idx = await Promise.race(workers.map((w, i) => w.then(() => i)));\n"
        "    workers.splice(idx, 1);\n"
        "  }\n"
        "  if (_exhausted) {\n"
        "    for (const bid of _queue) { for (const r of BATCHES.find((b) => b.id === bid).rows) "
        "_handBack(r, 'budget-exhausted', 'admission ceiling reached'); }\n"
        "  }\n"
        "  await _drainCommit();\n"
        "}\n"
        "await runGrind();"
    )

    lines.append(
        "const HANDBACK = {\n"
        "  schema: 'queue-grind-handback/1',\n"
        "  profile: PROFILE_NAME,\n"
        "  appetite: APPETITE_NAME,\n"
        "  handed_back: _handedBack,\n"
        "  settled: _settled,\n"
        "  counts: _counts(),\n"
        "  spend: _spend(),\n"
        "};"
    )
    lines.append("return HANDBACK;")

    return "\n\n".join(lines) + "\n"
