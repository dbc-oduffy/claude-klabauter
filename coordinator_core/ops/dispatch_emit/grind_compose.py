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
accounting, and the hand-back document. Emit-time admission
(``grind_admission``, via ``queue_emit``) owns the ceiling; the runtime
gates here are a backstop.

**Live routing.** ``ROUTING`` is the profile's own graph, rendered once as
data; a small generic interpreter (``routeAfterTriage``/``followEdge`` in
the emitted script, mirrored by ``route_after_triage``/``follow_edge`` in
the pure-Python model below) reads it at RUN time against whatever the
triage/fix/verify/refute-close agent actually returns, per ROW -- two rows
in the same triage batch can diverge to different graph nodes depending
on their own verdict/size/tradeoff.

**Engine-fixed routing** (DR-404 § Run authority, not a profile knob): a
row triage sizes ``>= grind_vocab.PLAN_WEIGHT_FLOOR`` is sized afresh by the
``coordinator:sizing-scout`` (one call per row): a size below the floor sends
it to ``fix``, and at or above it the row is handed back as ``baton`` naming
its sizing, which the scout's own closing command writes (with the baton
and the row's ``route-to-plan`` ledger mark). A row
below plan weight with a non-empty tradeoff
routes to ``needs-judgment`` unless the row carries an engine-read ``pm_ruling``: the ruling accepted
(``tradeoff_disposition``) continues to fix, and a ruling that rejects it is handed back
``repeat-refused`` naming the ruling (checked at triage-exit AND fix-exit, since a
fixer can surface a tradeoff triage missed); ``NEEDS_PLAN`` maps to
``baton``; ``NEEDS_WIDER_SCOPE`` gets exactly one release-and-reacquire
retry over the union of its locked files plus every extra file, then
``widen-exhausted``; ``PEER_DIRTY`` hands back ``peer-dirty`` directly; a
``verify`` failure gets exactly one retry then ``undo`` then
``rejected-after-retry`` -- or ``stage-dead`` (``undo-failed:``) when
``undo`` does not return ``undone``, so a row whose edits may remain in the
tree is never reported as an ordinary rejection; a node's ``on_fail`` back-edge is traversed at
most once per row.

**Every non-landing row is undone.** A row handed back after a fix stage reported writes
(``widen-exhausted``, ``needs-judgment``, ``baton``, ``manifest-stale``,
``entangled``, ``stage-dead``, and a thrown run's unfinished rows) runs the existing verify-op
reject mode over its accumulated in-root touched, created and removed paths, under its lock, and
keeps its type with ``; undone`` appended -- or becomes ``stage-dead`` (``undo-failed:``) when the
undo does not return ``undone``. ``commit-failed`` is exempt: ``grind-row settle-run`` lands
those rows. ``peer-dirty`` is exempt too: undo withholds only paths a live peer *claims*, and
PEER_DIRTY reports unclaimed dirt, so its reason names the paths left in the tree. A row whose
fix reported no writes makes no undo call.

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
``grind_stages.py``'s ``compose_fix_call``/``compose_verify_op_call``/
``compose_ledger_sweep_call`` each take an
optional ``*_js`` parameter naming a JS runtime expression; when supplied,
the composed prompt is built as static, escaped literal pieces
concatenated (via ``+``) with that live expression, never a static
per-row manifest-path stand-in. This module always supplies the live
expression: a fix's lock-key/"files you hold" clause reads the row's
runtime ``declaredFiles``; an undo runs one ``grind-row undo`` verb
call over the union of the live touched and created paths; the drain's
ledger sweep reads the live ``RUN_ID`` in scope. A finished row never
commits on its own: it queues on ``_pendingCommit`` with its runtime
``touchedFiles``/``createdFiles``/``removedFiles``, and ONE terminal
``dispatch.land_run`` call lands every queued row after the grind
(in a ``finally``, so a halted or throwing run still lands what finished).
After review and adjudication an outer ``finally`` runs ``_runClose()``: one
drain call whose verb sweeps, then writes the whole run record whatever the sweep did, so a throw
still writes the whole run record and lands the run's own writes.
The mutex lock keys
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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.contract import grind_vocab as vocab
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
from coordinator_core.ops.dispatch_emit import grind_stages as stages
from coordinator_core.ops.dispatch_emit import pm_adjudication
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.landing_integrity import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import (
    PARAMS_BLOCK_JS,
    PARAMS_FILE_NAME,
    READ_CAP_BYTES,
)
from coordinator_core.ops.dispatch_emit.emit import (
    ACCEPTED_ROSTER_SCHEMA_VERSIONS,
    NoReviewStageError,
    _AGENT_TYPE_HOST_DEGRADED,
    _BRIEF_PRECEDENCE_CLAUSE,
    _meta_block,
)
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops._workflow_contract import scrub
from coordinator_core.ops.dispatch_emit.fanout_cost import AGENT_CALL_RE, FanoutCost, with_fanout
from coordinator_core.ops.dispatch_emit.grind_profile import Profile
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry
from coordinator_core.ops.workflow_scaffold import _js_string_literal

__all__ = [
    "STAGE_OUTPUT_TOKENS",
    "batch_reserve",
    "build_routing_table",
    "pre_fix_verify_nodes",
    "dedupe_flip_route",
    "route_after_triage",
    "triage_fix_verdict",
    "triage_fix_target",
    "size_fail_reason",
    "follow_edge",
    "Row",
    "run_admission",
    "compose_grind_script",
]


_REVIEW_FIX_PHASE = "Review fixes"

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

def _verify_failed_twice(reason: Any) -> str:
    """Hand-back reason for a row whose verify failed on the retry; carries the verifier's own reason.
    Twin: ``_why`` in ``_verifyStage``."""
    return "verify failed twice" + (f": {str(reason)[:400]}" if reason else "")


def dedupe_flip_route(routing: Mapping[str, dict], triage_node_id: str) -> Optional[str]:
    """The node a retire-verdict ``dedupe`` enters from triage, or None when the profile routes it to a
    hand-back. Twin: ``routeAfterTriage('dedupe', ...)`` in ``_verifyStage``."""
    action = route_after_triage(routing, triage_node_id, "dedupe", None, "")
    return action[1] if action[0] == "node" else None


def pre_fix_verify_nodes(routing: Mapping[str, dict]) -> frozenset[str]:
    """Verify nodes no fix node can reach through ``edges`` or ``on_fail``: they run before any fix,
    so they judge routing (pass leads to fix, refute-close, a hand-back), not the fix.
    Twin: ``VERIFY_PRE_FIX`` in the emitted script."""
    reached: set[str] = set()
    stack = [n for n, node in routing.items() if node["kind"] == "fix"]
    while stack:
        node = routing[stack.pop()]
        for target in (*node["edges"].values(), node.get("on_fail")):
            if target in routing and target not in reached:
                reached.add(target)
                stack.append(target)
    return frozenset(n for n, node in routing.items() if node["kind"] == "verify" and n not in reached)


def _triage_node_id(profile: Profile) -> str:
    for node_id, node in profile.graph.items():
        if node.kind == "triage":
            return node_id
    raise ValueError("profile graph has no triage node")

def _out_of_root_paths(paths: Optional[Sequence[str]]) -> list[str]:
    """Paths that lexically escape the repo root: absolute, or `..` below depth 0.

    Pure string walk, no filesystem access. Twin: ``_outOfRootPaths`` in
    ``_ROUTING_HELPERS`` -- keep the two in lockstep.
    """
    out: list[str] = []
    for p in paths or []:
        s = str(p).replace("\\", "/")
        if s.startswith("/") or re.match(r"^[A-Za-z]:/", s):
            out.append(p)
            continue
        depth = 0
        for seg in s.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                depth -= 1
                if depth < 0:
                    out.append(p)
                    break
            else:
                depth += 1
    return out


_REPEAT_REFUSED_HEAD = "repeat hand-back refused: this row's earlier ruling already answered this tradeoff: "


def tradeoff_disposition(tradeoff: str, pm_ruling: str, ruling_on_tradeoff: str) -> str:
    """How a reported tradeoff is handled: ``none`` (no tradeoff), ``accepted`` (the row's engine-read
    ``pm_ruling`` accepts it), ``needs-judgment`` (unruled, or the ruling is silent on it) or
    ``repeat-refused`` (ruled and not accepted: fails closed, never re-asks). An agent's claim alone never
    accepts: ``pm_ruling`` must be non-empty. Twin: ``_tradeoffDisposition``."""
    if not tradeoff:
        return "none"
    if not pm_ruling:
        return "needs-judgment"
    if ruling_on_tradeoff == "accepts":
        return "accepted"
    if ruling_on_tradeoff == "silent":
        return "needs-judgment"
    return "repeat-refused"


def route_after_triage(
    routing: Mapping[str, dict],
    triage_node_id: str,
    verdict: str,
    tshirt_size: Optional[str],
    tradeoff: str,
    pm_ruling: str = "",
    ruling_on_tradeoff: str = "",
) -> tuple[str, str]:
    if verdict not in vocab.RETIRE_VERDICTS and _tshirt_rank(tshirt_size) >= _PLAN_WEIGHT_FLOOR_RANK:
        return ("handback", "baton")
    disposition = tradeoff_disposition(tradeoff, pm_ruling, ruling_on_tradeoff)
    if disposition in ("needs-judgment", "repeat-refused"):
        return ("handback", disposition)
    node = routing[triage_node_id]
    target = node["edges"].get(verdict)
    if target is None or target not in routing:
        return ("handback", target or "stage-dead")
    return ("node", target)

def triage_fix_target(routing: Mapping[str, dict], triage_node_id: str) -> Optional[str]:
    """The one node a triage edge enters on the way to a fix node (directly, or through verify nodes' `pass`
    edges); None when there are none or several. A below-plan-weight sizing re-enters the graph there."""
    def reaches_fix(node_id: str, seen: frozenset) -> bool:
        node = routing.get(node_id)
        if node is None or node_id in seen:
            return False
        if node["kind"] == "fix":
            return True
        nxt = node["edges"].get("pass") if node["kind"] == "verify" else None
        return nxt is not None and reaches_fix(nxt, seen | {node_id})

    targets = {t for t in routing[triage_node_id]["edges"].values() if reaches_fix(t, frozenset())}
    return next(iter(targets)) if len(targets) == 1 else None


def size_fail_reason(scouted: Mapping[str, Any]) -> str:
    """The failed size stage's cause: the exit code (or its absence) and the stderr tail. Twin: ``_sizeFailReason``."""
    code = scouted.get("exit_code")
    tail = str(scouted.get("stderr") or "").strip()[-300:] or "(no stderr)"
    if code is None:
        return f"exit missing: the scout ran no size-chain command; {tail}"
    return f"exit {code}: {tail}"


def triage_fix_verdict(routing: Mapping[str, dict], triage_node_id: str) -> Optional[str]:
    """The one triage verdict whose edge reaches a fix-kind node; None when there are none or several."""
    edges = routing[triage_node_id]["edges"]
    fix_verdicts = [v for v, t in edges.items() if t in routing and routing[t]["kind"] == "fix"]
    return fix_verdicts[0] if len(fix_verdicts) == 1 else None


_SIZE_CHECK_KEYS = (
    "triaged",
    "triage_handback",
    "size_confirmed",
    "size_collapsed",
    "fix_needs_plan",
    "fix_not_reproduced",
    "fix_late_tradeoff",
    "ruled_tradeoff",
    "repeat_refused",
)


def _slug(text: Any) -> str:
    """Lowercase ``text`` reduced to ``[a-z0-9-]``. Twin: ``_slug`` in the emitted script."""
    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")


def _sizing_path(run_id: str, scouted: Mapping[str, Any], row_id: str) -> str:
    """Repo-relative sizing path for one row. Twin: ``_sizingPath`` in the emitted script."""
    d = str(run_id).replace("-", "")[:8]
    return f"state/sizings/{d[:4]}-{d[4:6]}-{d[6:8]}-{_slug(scouted.get('name')) or 'sizing'}-{_slug(row_id) or 'row'}.yaml"


#: The params block rides the spawn transcript's computed-task record, which `read_spawn_prompt` caps at
#: `READ_CAP_BYTES`. The transcript stores the prompt JSON-escaped, so the budget is measured on the
#: twice-encoded params, less room for the prompt's own text. Over it, the rows are handed back, never
#: truncated.
PARAMS_ESCAPED_BUDGET = READ_CAP_BYTES - 4096

_SETTLE_HINT_TWIN =" -- land the finished rows with `grind-row settle-run`"

_UNDO_FAILED_HEAD = "undo-failed: verify rejected the fix twice and its edits may remain in the tree"
_UNDO_FAILED_TAIL = " -- the row stays open; restore the touched files by hand"
_NOT_UNDONE_PEER = "not undone (a peer's edits sit on these paths)"
_HISTORICAL_UNDO_FAILED_HEAD = "undo-failed: the fix wrote historical records and its edits may remain in the tree"


def _norm_rel(path: Any) -> str:
    """A reported path as repo-relative POSIX. Twin: ``_normRel`` in the emitted script."""
    return re.sub(r"^(\./)+", "", str(path).replace("\\", "/"))


def _historical_paths(paths: Sequence[str]) -> list[str]:
    """The reported paths a fix stage may not write. Twin: ``_historicalPaths`` in the emitted script."""
    return [p for p in paths if contract.is_historical_record(_norm_rel(p))]


def _union(*lists: Optional[Sequence[str]]) -> list[str]:
    return list(dict.fromkeys(p for lst in lists for p in (lst or [])))


#: Hand-back types that never run an undo. ``commit-failed``: ``grind-row settle-run`` lands those
#: rows. ``peer-dirty``: undo withholds only live-peer *claims*, and PEER_DIRTY reports unclaimed
#: dirt. Emitted into the script as ``_UNDO_EXEMPT``.
_UNDO_EXEMPT = ("commit-failed", "peer-dirty")


def _has_writes(row: Row) -> bool:
    """Did this row write anything that must be put back? Twin: ``_hasWrites``."""
    return bool(row.removed_files or row.created_files or row.fix_reported)


def _undo_paths(row: Row) -> list[str]:
    """The in-root paths a row's undo restores. Twin: ``_undoPaths``."""
    return [p for p in _union(row.touched_files, row.created_files, row.removed_files) if not _out_of_root_paths([p])]


def _nonlanding_undo_failed_head(htype: str) -> str:
    """Head of the stage-dead reason when a non-landing row's undo did not report `undone`."""
    return f"undo-failed: the row was handed back {htype} and its edits may remain in the tree"


def _undo_failed_reason(undo_result: Mapping[str, Any], head: str = _UNDO_FAILED_HEAD) -> str:
    detail = undo_result.get("reason")
    shown = detail or ("" if undo_result.get("outcome") else "the verify stage returned no undo reply")
    return head + (f" ({shown})" if shown else "") + _UNDO_FAILED_TAIL


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
    declared_files: list = field(default_factory=list)
    widened: bool = False
    verify_retried: bool = False
    dedupe_flipped: bool = False
    on_fail_used: bool = False
    done: bool = False
    sha: str = ""
    origin: str = ""
    refute_note: str = ""
    fix_reported: bool = False
    sizing_path: str = ""
    evidence: str = ""
    pm_ruling: str = ""
    ruled_once: bool = False
    adjudication: Optional[dict] = None

@dataclass
class _Batch:
    batch_id: str
    row_ids: list[str]
    triaged: bool = False
    #: Rows already proposed to a refute-close call: the guard is once per ROW, because rows reach
    #: a refute-close node at different times and a later arrival needs its own call.
    closed_rows: set[str] = field(default_factory=set)


_EXHAUSTED_REASON = (
    "emit-admitted row; runtime ceiling reached (per-row cost above the measured "
    "admission cost, or runtime budget bound)"
)


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
    run_id: str = "20260101T000000Z",
    profile: str = "profile",
    has_run_token: bool = True,
    commit_params_sink: Optional[list] = None,
    post_stages: Sequence[str] = (),
    run_close_sink: Optional[list] = None,
    undo_paths_sink: Optional[list] = None,
    pm_rulings: Optional[Mapping[str, str]] = None,
) -> dict:
    """Drive the admission policy over ``batches`` (``(batch_id,
    [row_id, ...])`` pairs, in queue order).

    Emit-time admission (``grind_admission``, via ``queue_emit``) owns the
    ceiling; the gates here are a runtime backstop. A row handed back
    ``budget-exhausted`` was emit-admitted and outran its measured cost.

    ``budget_tokens``, when given, mirrors the rendered `.mjs`'s
    ``BUDGET_TOKENS`` ceiling: admission halts once the next batch's
    ``reserve`` would carry cumulative spend past it -- independent of
    ``budget.total``/``remaining()`` below.

    ``agent(stage_kind, unit_id) -> result`` is the caller's stub. ``unit_id``
    is a batch id for ``triage``/``refute-close``, a row id for
    ``fix``/``verify``/``commit``/``size``. It is expected to call
    ``budget.spend(n)`` itself (mirroring the real runtime, where every
    agent call is what advances ``budget.spent()``).

    For ``triage`` the stub returns a list of
    ``{"row": id, "verdict": v, "tshirt_size": s, "tradeoff": t}``. For
    ``refute-close`` it returns ``{"confirmed": [...], "refuted": [{"row":
    id, "reason": r}, ...]}``. For ``fix``/``verify`` it returns
    a dict carrying at least ``"outcome"`` (and ``"tradeoff"``/
    ``"extra_files"`` where relevant). A triage record may carry
    ``"declared_files"``. No row commits on its own: a finished row queues, and
    one ``agent("commit", "run")`` call at the end lands the queue, replying
    ``{"committed": bool, "sha", "rows": [{"row_id", "outcome", "sha"}],
    "dropped": [{"row_id", "path", "reason"}], "error", "refused"}``. The params
    that call carries are appended to ``commit_params_sink`` when one is given,
    and a stage that raises still lands the rows finished so far before the
    error propagates. ``post_stages`` names the stage kinds (review, review-fix,
    adjudication) called once, in order, after that commit when the grind did not
    throw. An outer ``finally`` then runs the run close: one ``ledger-sweep`` call (retried once
    if it raises) whose reply is ``{"outcome": "recorded"|"record-failed", "swept", "sweep_reason"}``
    -- a record-failed reply whose reason holds ``RELAY_CORRUPT_HEAD`` re-drives the call once --
    the verb sweeps and writes the record whole; the record and declared paths it carries are
    appended to ``run_close_sink`` before the call, with a ``swept: false`` reply's stage-dead
    appended to the sunk record. A non-landing row with reported writes runs ``verify`` and reads
    its ``undo``; each such call first appends ``{"row", "paths"}`` (the paths the undo receives) to
    ``undo_paths_sink``.
    For ``size`` it returns the
    scout's ``{tshirt, intent, name, exit_code, ...}`` (or ``{}`` for a refusal), ``exit_code``
    being its closing sizing chain's. A ``verify`` that fails a row's final verification also
    carries ``"undo": {"outcome", "reason"}``.

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
            rows[rid] = Row(
                row_id=rid, path=rid, batch_id=batch_id, declared_files=[rid],
                pm_ruling=(pm_rulings or {}).get(rid, ""),
            )
            order.append(rid)

    call_log: list[tuple[str, str]] = []
    calls_by_kind: dict[str, int] = {}
    handed_back: list[dict] = []
    settled: list[dict] = []
    admitted: dict[str, _Batch] = {}
    exhausted = False
    pending_commit: list[dict] = []
    run_dirty: set[str] = set()
    terminal_commit_dropped: list[dict] = []
    terminal_params: Optional[dict] = None
    by_refute_origin: dict[str, dict[str, int]] = {}
    by_triage_check: dict[str, int] = {k: 0 for k in _SIZE_CHECK_KEYS}
    pre_fix_verify = pre_fix_verify_nodes(routing)
    fix_target = triage_fix_target(routing, triage_node_id)

    def _bump_refute_origin(origin: str, key: str) -> None:
        entry = by_refute_origin.setdefault(origin, {"confirmed": 0, "refuted": 0})
        entry[key] += 1

    def _record_call(kind: str, unit_id: str) -> Any:
        result = agent(kind, unit_id)
        call_log.append((kind, unit_id))
        calls_by_kind[kind] = calls_by_kind.get(kind, 0) + 1
        return result

    def _handback(row: Row, htype: str, reason: str, tradeoff: str = "", ruling: str = "") -> None:
        row.done = True
        if row.refute_note:
            reason = f"{reason} (revived from a refuted close proposal, origin {row.origin!r}: {row.refute_note!r})"
        if any(h["row"] == row.row_id and h["type"] == htype for h in handed_back):
            return
        entry = {"row": row.row_id, "type": htype, "reason": reason}
        if tradeoff:
            entry["tradeoff"] = tradeoff
        if ruling:
            entry["ruling"] = ruling
        if row.adjudication and htype in pm_adjudication.PM_BOUND_HANDBACK_TYPES:
            entry["adjudication"] = row.adjudication
        handed_back.append(entry)

    def _apply_route(row: Row, action: tuple[str, str], reason: str, tradeoff: str = "", ruling: str = "") -> None:
        kind, value = action
        if kind == "handback":
            _handback(row, value, reason, tradeoff, ruling)
        else:
            row.node = value

    def _adjudicate(row: Row) -> dict:
        """Rule one PM-bound hand-back; fails closed to pmOnly. Twin: ``_adjudicateHandback``."""
        try:
            verdict = _record_call("adjudicate", row.row_id)
        except Exception:  # noqa: BLE001 -- a throwing adjudicator is a missing verdict
            verdict = None
        if not isinstance(verdict, Mapping) or not verdict.get("verdict"):
            verdict = None
        gated = bool(verdict) and re.search(
            pm_adjudication.IRREVERSIBLE_GATE_SOURCE, str(verdict.get("externalAction") or ""), re.IGNORECASE
        ) is not None
        pm_only = verdict is None or verdict.get("pmOnly") is True or gated
        return {
            "adjudicator": pm_adjudication.ADJUDICATOR_AGENT_TYPE,
            "verdict": verdict["verdict"] if verdict else "unavailable",
            "pmOnly": pm_only,
            "ruling": verdict.get("ruling") if verdict else None,
            "actionable": bool(verdict and verdict.get("actionable")) and not pm_only,
        }

    def _ruled_fix_node(row: Row) -> Optional[str]:
        """The node a ruled row re-enters: its triage verdict's target, else the fix target. Twin: ``_ruledFixNode``."""
        verdict = row.origin.removeprefix("triage:") if row.origin.startswith("triage:") else ""
        if verdict:
            kind, value = route_after_triage(routing, triage_node_id, verdict, None, "", row.pm_ruling, "")
            if kind == "node":
                return value
        return fix_target if fix_target and fix_target in routing else None

    def _handback_row(row: Row, htype: str, reason: str, tradeoff: str = "", ruling: str = "") -> None:
        """Hand back a non-landing row, undoing its writes first unless its type is exempt. A PM-bound
        type is adjudicated once per row; an actionable ruling re-enters fix. Twin: ``_handBackRow``."""
        row.done = True
        reloop_node: Optional[str] = None
        if htype in pm_adjudication.PM_BOUND_HANDBACK_TYPES and not row.ruled_once:
            row.ruled_once = True
            row.adjudication = _adjudicate(row)
            if row.adjudication["actionable"] and row.adjudication["ruling"]:
                reloop_node = _ruled_fix_node(row)

        def _reloop(undone: bool) -> None:
            row.pm_ruling = row.adjudication["ruling"]
            if undone:
                row.touched_files, row.created_files, row.removed_files = [], [], []
                row.fix_reported = False
            row.done = False
            row.node = reloop_node

        undo_set = _undo_paths(row)
        if htype in _UNDO_EXEMPT or not _has_writes(row) or not undo_set:
            if reloop_node:
                _reloop(False)
                return
            if htype == "peer-dirty" and _has_writes(row) and undo_set:
                reason = f"{reason}; {_NOT_UNDONE_PEER}: {', '.join(undo_set)}"
            _handback(row, htype, reason, tradeoff, ruling)
            return
        if undo_paths_sink is not None:
            undo_paths_sink.append({"row": row.row_id, "paths": list(undo_set)})
        try:
            undo_result = (_record_call("verify", row.row_id) or {}).get("undo") or {}
        except Exception as err:  # noqa: BLE001 -- an undo that throws is an undo that did not land
            undo_result = {"outcome": "undo-failed", "reason": str(err)}
        if undo_result.get("outcome") != "undone":
            _handback(row, "stage-dead", _undo_failed_reason(undo_result, _nonlanding_undo_failed_head(htype)))
            return
        if reloop_node:
            _reloop(True)
            return
        _handback(row, htype, f"{reason}; undone", tradeoff, ruling)

    def _apply_route_row(row: Row, action: tuple[str, str], reason: str) -> None:
        kind, value = action
        if kind == "handback":
            _handback_row(row, value, reason)
        else:
            row.node = value

    def _queue_commit(row: Row) -> None:
        row.done = True
        paths = list(dict.fromkeys([*row.touched_files, *row.created_files]))
        deleted = list(dict.fromkeys(row.removed_files))
        pending_commit.append({"row_id": row.row_id, "outcome": "settled", "paths": paths, "deleted_paths": deleted})
        run_dirty.update(paths, deleted)
        settled.append({"row": row.row_id, "outcome": "committed", "sha": ""})

    def _unsettle(row_id: str) -> None:
        for i, entry in enumerate(settled):
            if entry["row"] == row_id and entry["outcome"] == "committed":
                del settled[i]
                return

    def _entangle(row: Row, written: Sequence[str]) -> bool:
        hit = {p for p in written if p in run_dirty}
        if not hit:
            return False
        row.done = True
        shared = ", ".join(sorted(hit))
        collided = [q for q in pending_commit if hit & {*q["paths"], *q["deleted_paths"]}]
        for q in collided:
            pending_commit.remove(q)
            _unsettle(q["row_id"])
            _handback_row(
                rows[q["row_id"]], "entangled",
                f"a later fix wrote paths this row queued for the terminal commit: {shared}",
            )
        run_dirty.clear()
        for q in pending_commit:
            run_dirty.update(q["paths"], q["deleted_paths"])
        _handback_row(row, "entangled", f"this fix wrote paths already queued for the terminal commit: {shared}")
        return True

    def _halt_reason(grind_err: Optional[BaseException]) -> Optional[str]:
        if exhausted:
            return "budget-exhausted"
        return (str(grind_err) or "error") if grind_err else None

    def _terminal_commit(grind_err: Optional[BaseException]) -> None:
        nonlocal terminal_params, terminal_commit_dropped
        if not pending_commit:
            return
        queued = sorted(pending_commit, key=lambda q: q["row_id"])
        pending_commit.clear()
        run_dirty.clear()

        def _fail_all(reason: str) -> None:
            for q in queued:
                _unsettle(q["row_id"])
                _handback(rows[q["row_id"]], "commit-failed", reason)

        if not has_run_token:
            _fail_all("no run token in the fire args: the terminal commit cannot run" + _SETTLE_HINT_TWIN)
            return
        halted = _halt_reason(grind_err)
        terminal_params = {"profile": profile, "rows": queued, "halted": halted}
        full_params = {"run_id": run_id, "grind": terminal_params}
        size = len(json.dumps(json.dumps(full_params, separators=(",", ":"))))
        if size > PARAMS_ESCAPED_BUDGET:
            _fail_all(
                f"terminal commit params are {size} bytes escaped, over the {PARAMS_ESCAPED_BUDGET} "
                "the spawn transcript can carry; the rows stay on disk" + _SETTLE_HINT_TWIN
            )
            return
        if commit_params_sink is not None:
            commit_params_sink.append(full_params)
        reply = _record_call("commit", "run") or {}
        if reply.get("committed") is not True:
            why = reply.get("refused") or reply.get("error")
            _fail_all("terminal commit did not land" + (f": {why}" if why else "") + _SETTLE_HINT_TWIN)
            return
        terminal_commit_dropped = list(reply.get("dropped") or [])
        has_rows = isinstance(reply.get("rows"), list)
        landed = {r["row_id"]: r for r in reply["rows"]} if has_rows else {}
        unlanded = {u["row_id"]: u for u in (reply.get(contract.UNLANDED_KEY) or [])}
        for q in queued:
            hit = landed.get(q["row_id"]) if has_rows else {"sha": reply.get("sha")}
            row = rows[q["row_id"]]
            if hit is None and q["row_id"] in unlanded:
                _unsettle(q["row_id"])
                why_unlanded = unlanded[q["row_id"]].get("reason") or "the already-landed commit did not carry this row"
                _handback(row, "commit-failed", why_unlanded + _SETTLE_HINT_TWIN)
                continue
            if hit is None:
                drops = [f"{d['path']} ({d['reason']})" for d in terminal_commit_dropped if d["row_id"] == q["row_id"]]
                _unsettle(q["row_id"])
                _handback(
                    row, "commit-failed",
                    "every path of this row was dropped from the terminal commit"
                    + (": " + ", ".join(drops) if drops else "") + _SETTLE_HINT_TWIN,
                )
                continue
            row.sha = hit.get("sha") or reply.get("sha") or ""
            for entry in settled:
                if entry["row"] == q["row_id"] and entry["outcome"] == "committed":
                    entry["sha"] = row.sha

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
        to_size: list[Row] = []
        for rec in result or []:
            if rec["row"] not in batch.row_ids:
                continue
            row = rows[rec["row"]]
            seen.add(rec["row"])
            by_triage_check["triaged"] += 1
            if rec.get("declared_files"):
                row.declared_files = list(rec["declared_files"])
            row.origin = f"triage:{rec['verdict']}"
            tradeoff = rec.get("tradeoff", "") if rec.get("has_tradeoff") else ""
            on_tradeoff = rec.get("ruling_on_tradeoff", "")
            action = route_after_triage(
                routing, triage_node_id, rec["verdict"], rec.get("tshirt_size"), tradeoff,
                row.pm_ruling, on_tradeoff,
            )
            if action == ("handback", "baton"):
                to_size.append(row)
                continue
            if action[0] == "handback":
                by_triage_check["triage_handback"] += 1
            if tradeoff and action == ("handback", "repeat-refused"):
                by_triage_check["repeat_refused"] += 1
                _apply_route(row, action, _REPEAT_REFUSED_HEAD + row.pm_ruling, tradeoff, row.pm_ruling)
            elif tradeoff and action == ("handback", "needs-judgment"):
                _handback_row(row, "needs-judgment", f"triage reported a tradeoff: {tradeoff}", tradeoff)
            else:
                if tradeoff:
                    by_triage_check["ruled_tradeoff"] += 1
                if action[0] == "handback" and action[1] in pm_adjudication.PM_BOUND_HANDBACK_TYPES:
                    _handback_row(row, action[1], f"triage verdict {rec['verdict']!r}")
                else:
                    _apply_route(row, action, f"triage verdict {rec['verdict']!r}")
        for rid in batch.row_ids:
            row = rows[rid]
            if rid not in seen and not row.done and row.node is None:
                _handback(row, "stage-dead", "triage never returned a record for this row")
        for row in to_size:
            _size_row(row)

    def _size_row(row: Row) -> None:
        scouted = _record_call("size", row.row_id) or {}
        tshirt = scouted.get("tshirt")
        if not tshirt:
            reason = scouted.get("reason")
            _handback(row, "baton", "size scout returned no size" + (f": {reason}" if reason else ""))
            return
        below_floor = _tshirt_rank(tshirt) < _PLAN_WEIGHT_FLOOR_RANK
        own_target = routing[triage_node_id]["edges"].get(row.origin.removeprefix("triage:"))
        if below_floor and fix_target and fix_target in routing and own_target == fix_target:
            by_triage_check["size_collapsed"] += 1
            row.origin = "size:below-plan-weight"
            row.node = fix_target
            return
        if not below_floor:
            by_triage_check["size_confirmed"] += 1
        row.done = True
        intent = scouted.get("intent", "")
        if scouted.get("exit_code") == 0:
            row.sizing_path = _sizing_path(run_id, scouted, row.row_id)
            _handback(row, "baton", f"sized at {row.sizing_path}: {intent}")
        else:
            _handback(row, "baton", f"{intent} (size step failed: {size_fail_reason(scouted)})")

    def _close_batch(batch: _Batch) -> None:
        close_node_ids = {nid for nid, node in routing.items() if node["kind"] == "refute-close"}
        proposal_ids = [
            rid for rid in batch.row_ids
            if rows[rid].node in close_node_ids and not rows[rid].done and rid not in batch.closed_rows
        ]
        result = _record_call("refute-close", batch.batch_id) or {}
        batch.closed_rows.update(proposal_ids)
        for item in result.get("confirmed", []):
            row = rows[item["row"]]
            if row.node is None or row.done:
                continue
            _bump_refute_origin(row.origin, "confirmed")
            row.removed_files = list(row.removed_files) + [row.path]
            row.touched_files = list(row.touched_files) + [item.get("new_path", "")]
            action = follow_edge(routing, row.node, "confirmed", row)
            _apply_route(row, action, "refute-close confirmed")
            # Parity with the rendered `.mjs`: a confirmed-close row whose
            # route is a direct handback (no explicit `commit` node) queues its
            # archive-move for the terminal commit.
            if action[0] == "handback":
                _queue_commit(row)
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
        for rid in proposal_ids:
            row = rows[rid]
            if not row.done and row.node in close_node_ids:
                _handback(row, "stage-dead", "refute-close left this proposal unresolved (absent from confirmed/refuted/stale)")

    def _fix_row(row: Row) -> None:
        overlap = [p for p in row.declared_files if p in run_dirty]
        if overlap:
            _handback(
                row, "overlap-deferred",
                "declared files overlap rows queued for the terminal commit: "
                + ", ".join(overlap) + "; its ledger is left for the next grind",
            )
            return
        prior_node = row.node
        result = _record_call("fix", row.row_id) or {}
        tradeoff = result.get("tradeoff", "") if result.get("has_tradeoff") else ""
        outcome = result.get("outcome")
        if result.get("touched_files"):
            row.touched_files = _union(row.touched_files if row.fix_reported else [], result["touched_files"])
            row.fix_reported = True
        row.created_files = _union(row.created_files, result.get("created_files"))
        close_result = result.get("close_result")
        if outcome == "done" and close_result:
            row.removed_files = list(row.removed_files) + [close_result.get("old", row.path)]
            row.touched_files = list(row.touched_files) + [close_result.get("new", "")]
        disposition = tradeoff_disposition(tradeoff, row.pm_ruling, result.get("ruling_on_tradeoff", ""))
        if disposition == "repeat-refused":
            by_triage_check["repeat_refused"] += 1
            _handback_row(row, "repeat-refused", _REPEAT_REFUSED_HEAD + row.pm_ruling, tradeoff, row.pm_ruling)
            return
        if disposition == "needs-judgment":
            by_triage_check["fix_late_tradeoff"] += 1
            _handback_row(row, "needs-judgment", f"fix reported a tradeoff: {tradeoff}", tradeoff)
            return
        if disposition == "accepted":
            by_triage_check["ruled_tradeoff"] += 1
        if outcome == "NEEDS_PLAN":
            by_triage_check["fix_needs_plan"] += 1
            _handback_row(row, "baton", "fix reported NEEDS_PLAN")
            return
        if outcome == "PEER_DIRTY":
            _handback_row(row, "peer-dirty", "fix reported PEER_DIRTY")
            return
        if outcome == "MANIFEST_STALE":
            _handback_row(row, "manifest-stale", "fix reported MANIFEST_STALE")
            return
        if outcome == "NEEDS_WIDER_SCOPE":
            if not row.widened:
                row.widened = True
                return
            _handback_row(row, "widen-exhausted", "second NEEDS_WIDER_SCOPE")
            return
        escaped = _out_of_root_paths(list(result.get("touched_files") or []) + list(result.get("created_files") or []))
        if escaped:
            _handback_row(
                row,
                "needs-judgment",
                "fix touched files outside repo_root (a cross-repo commit needs per-run PM assent; left uncommitted): "
                + ", ".join(escaped),
            )
            return
        written = [*(result.get("touched_files") or []), *(result.get("created_files") or [])]
        historical = _historical_paths(written)
        if historical:
            if undo_paths_sink is not None:
                undo_paths_sink.append({"row": row.row_id, "paths": _undo_paths(row)})
            undo_result = (_record_call("verify", row.row_id) or {}).get("undo") or {}
            if undo_result.get("outcome") != "undone":
                _handback(row, "stage-dead", _undo_failed_reason(undo_result, _HISTORICAL_UNDO_FAILED_HEAD))
                return
            _handback(
                row, "needs-judgment",
                "fix wrote historical records, undone: " + ", ".join(historical),
            )
            return
        if _entangle(row, written):
            return
        if outcome == "NOT_REPRODUCED":
            by_triage_check["fix_not_reproduced"] += 1
        row.fix_node = prior_node
        _apply_route_row(row, follow_edge(routing, row.node, outcome, row), f"fix outcome {outcome!r}")

    def _verify_row(row: Row) -> None:
        result = _record_call("verify", row.row_id) or {}
        outcome = result.get("outcome")
        if outcome == "fail":
            flip_to = dedupe_flip_route(routing, triage_node_id)
            if (
                result.get("covered_by") and row.node in pre_fix_verify and not row.dedupe_flipped
                and row.origin != "triage:dedupe" and flip_to is not None
            ):
                row.dedupe_flipped = True
                row.verify_retried = True
                row.origin = "triage:dedupe"
                row.evidence = f"already covered at {result['covered_by']}"
                row.node = flip_to
                return
            if not row.verify_retried:
                row.verify_retried = True
                row.verify_feedback = result.get("reason", "")
                row.node = row.fix_node or row.node
                return
            if row.node not in pre_fix_verify:
                if undo_paths_sink is not None:
                    undo_paths_sink.append({"row": row.row_id, "paths": _undo_paths(row)})
                undo_result = result.get("undo") or {}
                if undo_result.get("outcome") != "undone":
                    _handback(row, "stage-dead", _undo_failed_reason(undo_result))
                    return
            _handback(row, "rejected-after-retry", _verify_failed_twice(result.get("reason")))
            return
        _apply_route_row(row, follow_edge(routing, row.node, outcome, row), f"verify outcome {outcome!r}")

    def _commit_row(row: Row) -> None:
        _queue_commit(row)

    def _dispatch_row(row: Row, batch: _Batch) -> None:
        kind = routing[row.node]["kind"]
        if kind == "fix":
            _fix_row(row)
        elif kind == "verify":
            _verify_row(row)
        elif kind == "commit":
            _commit_row(row)
        elif kind == "refute-close":
            if row.row_id not in batch.closed_rows:
                _close_batch(batch)
            else:
                _handback(row, "stage-dead", "refute-close already called for this batch")
        else:
            _handback(row, "stage-dead", f"unhandled node kind {kind!r}")

    def _finish_batch(batch: _Batch) -> None:
        del admitted[batch.batch_id]

    def _run_grind() -> None:
        nonlocal exhausted
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
            # Parity with the rendered `.mjs`'s
            # BUDGET_TOKENS ceiling (a caller-supplied hard cap, distinct from
            # budget.total/remaining() below): halts admission once the next
            # batch's reserve would carry cumulative spend past it.
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
                    handed_back.append({"row": rid, "type": "budget-exhausted", "reason": _EXHAUSTED_REASON})

    def _run_close(grind_err: Optional[BaseException]) -> None:
        def _stage_dead(reason: str) -> None:
            handed_back.append({"row": run_id, "type": "stage-dead", "reason": reason})

        record_path = f"state/queue-grind/{profile}/runs/{run_id}.json"
        sweep_dead = f"{contract.SWEEP_FAILED_HEAD}: {record_path}"
        by_type: dict[str, int] = {}
        for h in handed_back:
            by_type[h["type"]] = by_type.get(h["type"], 0) + 1
        halted = _halt_reason(grind_err)
        record = {
            "profile": profile,
            "run_id": run_id,
            "counts": {"by_type": by_type, "by_outcome": {"committed": len(settled)} if settled else {}},
            "spend": {
                "output_tokens": budget.spent() - start_spent,
                "agent_calls_total": len(call_log),
                "agent_calls_by_stage_kind": dict(calls_by_kind),
            },
            "handed_back": list(handed_back),
            "halted": halted is not None,
            "halted_reason": halted,
            "terminal_commit_dropped": terminal_commit_dropped,
        }
        declared = list(dict.fromkeys(
            [r.path for r in rows.values() if r.origin] + [r.sizing_path for r in rows.values() if r.sizing_path]
        ))
        if run_close_sink is not None:
            run_close_sink.append({"record": record, "declared": declared})
        drained: Optional[Mapping[str, Any]] = None
        threw = ""
        for _ in range(2):
            try:
                drained = _record_call("ledger-sweep", "run") or {}
                break
            except Exception as err:  # noqa: BLE001 -- surfaced on the hand-back, never masks the grind's own error
                threw = str(err)
        if (
            drained is not None
            and drained.get("outcome") == "record-failed"
            and contract.RELAY_CORRUPT_HEAD in str(drained.get("reason") or "")
        ):
            try:
                drained = _record_call("ledger-sweep", "run") or {}
            except Exception as err:  # noqa: BLE001 -- surfaced on the hand-back, never masks the grind's own error
                drained, threw = None, str(err)
        if drained is None:
            _stage_dead(f"{sweep_dead}: {threw}")
            _stage_dead(f"run record may be missing or incomplete: {record_path}: {threw}")
            return
        if drained.get("swept") is False:
            reason = drained.get("sweep_reason")
            sweep_entry = {"row": run_id, "type": "stage-dead", "reason": sweep_dead + (f": {reason}" if reason else "")}
            if run_close_sink is not None:
                sunk = run_close_sink[-1]["record"]
                sunk["handed_back"].append(sweep_entry)
                sunk["counts"]["by_type"]["stage-dead"] = sunk["counts"]["by_type"].get("stage-dead", 0) + 1
            handed_back.append(sweep_entry)
        if drained.get("outcome") != "recorded":
            reason = drained.get("reason")
            _stage_dead(f"run record may be missing or incomplete: {record_path}" + (f": {reason}" if reason else ""))

    grind_err: Optional[BaseException] = None
    try:
        try:
            _run_grind()
        except BaseException as err:
            grind_err = err
            raise
        finally:
            _terminal_commit(grind_err)
            if grind_err is not None:
                for row in rows.values():
                    if not row.done and _has_writes(row):
                        _handback_row(row, "stage-dead", f"run threw: {grind_err}")
        for kind in post_stages:
            _record_call(kind, "run")
    except BaseException as err:
        grind_err = err
        raise
    finally:
        _run_close(grind_err)

    end_spent = budget.spent()
    return {
        "halted": _halt_reason(None) is not None,
        "call_log": call_log,
        "handed_back": handed_back,
        "settled": settled,
        "terminal_commit": terminal_params,
        "terminal_commit_dropped": terminal_commit_dropped,
        "by_refute_origin": by_refute_origin,
        "by_triage_check": by_triage_check,
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
            # `-`, not `:` -- this id is embedded verbatim as a
            # `records/<batch-id>.json` filename in the composed triage and
            # verify-op stage prompts (grind_stages.py::compose_triage_call,
            # compose_verify_op_call). `:` is NTFS-illegal (the write guard's
            # own `block_illegal_filename` denies it, per
            # coordinator_core/bash_guards/_helpers.py::_ILLEGAL_CHARS_ORDER)
            # and is never parsed back out (grind_rows.py::_extract_batch_rows
            # matches the id by exact string equality), so the separator
            # carries no meaning worth an NTFS-illegal char.
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
    "function _outOfRootPaths(paths) {\n"
    "  const out = [];\n"
    "  for (const p of (paths || [])) {\n"
    "    const s = String(p).replace(/\\\\/g, '/');\n"
    "    if (s.startsWith('/') || /^[A-Za-z]:\\//.test(s)) { out.push(p); continue; }\n"
    "    let depth = 0;\n"
    "    for (const seg of s.split('/')) {\n"
    "      if (seg === '' || seg === '.') continue;\n"
    "      if (seg === '..') { depth -= 1; if (depth < 0) { out.push(p); break; } } else { depth += 1; }\n"
    "    }\n"
    "  }\n"
    "  return out;\n"
    "}\n"
    "function _tradeoffDisposition(tradeoff, pmRuling, onTradeoff) {\n"
    "  if (!tradeoff) return 'none';\n"
    "  if (!pmRuling) return 'needs-judgment';\n"
    "  if (onTradeoff === 'accepts') return 'accepted';\n"
    "  if (onTradeoff === 'silent') return 'needs-judgment';\n"
    "  return 'repeat-refused';\n"
    "}\n"
    "function routeAfterTriage(verdict, size, tradeoff, pmRuling, onTradeoff) {\n"
    "  if (!RETIRE_VERDICTS.includes(verdict) && _tshirtRank(size) >= PLAN_WEIGHT_FLOOR_RANK) return { kind: 'handback', value: 'baton' };\n"
    "  const _disp = _tradeoffDisposition(tradeoff, pmRuling, onTradeoff);\n"
    "  if (_disp === 'needs-judgment' || _disp === 'repeat-refused') return { kind: 'handback', value: _disp };\n"
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
    "function applyRoute(row, rowId, route, reason, tradeoff, ruling) {\n"
    "  if (route.kind === 'handback') { row.done = true; _handBack(rowId, route.value, reason, tradeoff, ruling); }\n"
    "  else { row.node = route.value; }\n"
    "}\n"
    "function _lockKeysFor(row, rowId) { return row.declaredFiles.concat([`ledger:${rowId}`]); }\n"
    "function _sizeFailReason(s) {\n"
    "  const tail = String(s.stderr || '').trim().slice(-300) || '(no stderr)';\n"
    "  return s.exit_code === undefined || s.exit_code === null ? 'exit missing: the scout ran no size-chain command; ' + tail : 'exit ' + s.exit_code + ': ' + tail;\n"
    "}\n"
    "function _commitReason(result) { return result && result.reason ? `: ${result.reason}` : ''; }"
)

#: The runner chains each `agent()` call's replay key on the call issued before it, so a resume
#: serves its cached prefix only while the script issues calls in the same order. `_issue` makes
#: that order a function of results alone: a call waits at the gate until every live worker is
#: at the gate (or finished), new batches are admitted only at that quiescent point, and the
#: waiting calls are then issued in (batch rank, row id) order. The gate sits outside `withLock`
#: so a lock holder is never parked at it. Twin: `run_admission` runs one batch at a time.
_ISSUE_GATE_JS = (
    "const _gateWaiting = [];\n"
    "let _gateHold = 0;\n"
    "let _gateResolve = null;\n"
    "let _grindOpen = false;\n"
    "let _workerFailure = null;\n"
    "function _gateKey(batchId, rowId) {\n"
    "  const i = BATCHES.findIndex((b) => b.id === batchId);\n"
    "  return String(i < 0 ? 999999 : i).padStart(6, '0') + ':' + (rowId || '');\n"
    "}\n"
    "function _issue(batchId, rowId, fn) {\n"
    "  return new Promise((resolve, reject) => {\n"
    "    _gateWaiting.push({ key: _gateKey(batchId, rowId), fn, resolve, reject });\n"
    "    _gateTick();\n"
    "  });\n"
    "}\n"
    "function _admit() {\n"
    "  while (_grindOpen && _queue.length && Object.keys(_admitted).length < WINDOW && !_anyAdmittedHasPendingDownstream()) {\n"
    "    if (MAX_AGENT_CALLS !== null && _callCount >= MAX_AGENT_CALLS) { _exhausted = true; }\n"
    "    else {\n"
    "      const _spentDelta = budget.spent() - _startSpent;\n"
    "      if (BUDGET_TOKENS !== null && _spentDelta + RESERVE > BUDGET_TOKENS) { _exhausted = true; }\n"
    "      else if (budget.total !== undefined && budget.total !== null && (budget.remaining() ?? 0) <= RESERVE) { _exhausted = true; }\n"
    "    }\n"
    "    if (_exhausted) break;\n"
    "    const bid = _queue.shift();\n"
    "    const batchState = { id: bid, batchKey: BATCHES.find((b) => b.id === bid).batch_key, triaged: false, closedRows: new Set() };\n"
    "    _admitted[bid] = batchState;\n"
    "    _runBatchWorker(batchState).catch((err) => {\n"
    "      if (!_workerFailure) { _workerFailure = { err }; }\n"
    "      delete _admitted[bid];\n"
    "      _gateTick();\n"
    "    });\n"
    "  }\n"
    "}\n"
    "function _gateTick() {\n"
    "  if (_gateHold > 0) return;\n"
    "  if (_gateWaiting.length < Object.keys(_admitted).length) return;\n"
    "  _gateHold += 1;\n"
    "  try { _admit(); } finally { _gateHold -= 1; }\n"
    "  if (_gateWaiting.length < Object.keys(_admitted).length) return;\n"
    "  if (_gateWaiting.length === 0) {\n"
    "    if (_gateResolve) { const done = _gateResolve; _gateResolve = null; done(); }\n"
    "    return;\n"
    "  }\n"
    "  const round = _gateWaiting.splice(0, _gateWaiting.length).sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));\n"
    "  for (const e of round) {\n"
    "    try { Promise.resolve(e.fn()).then(e.resolve, e.reject); } catch (err) { e.reject(err); }\n"
    "  }\n"
    "}\n"
)

#: The queue-and-land half of the run: finished rows queue on `_pendingCommit`, one
#: `_terminalCommit` lands them after the grind. `_runDirty` is the union of queued
#: rows' paths; a fix that collides with it is undone together with the rows it
#: collided with, which leave the queue first, so the undo never reverts a row
#: still queued.
#: Twin: `run_admission`'s `_queue_commit` / `_entangle` / `_terminal_commit`.
_TERMINAL_COMMIT_JS = (
    "function _queueCommit(rowId) {\n"
    "  const row = _rows[rowId];\n"
    "  const paths = Array.from(new Set(row.touchedFiles.concat(row.createdFiles || [])"
    ".concat(row.closeResult ? [row.closeResult.new] : [])));\n"
    "  const deleted = Array.from(new Set(row.removedFiles));\n"
    "  _pendingCommit.push({ row_id: rowId, outcome: row.lastOutcome || 'settled', paths, deleted_paths: deleted });\n"
    "  paths.concat(deleted).forEach((p) => _runDirty.add(p));\n"
    "  _settled.push({ row: rowId, outcome: 'committed', sha: '' });\n"
    "}\n"
    "function _unsettle(rowId) {\n"
    "  const j = _settled.findIndex((s) => s.row === rowId && s.outcome === 'committed');\n"
    "  if (j >= 0) { _settled.splice(j, 1); }\n"
    "}\n"
    "async function _entangle(rowId, written) {\n"
    "  const hit = new Set(written.filter((p) => _runDirty.has(p)));\n"
    "  if (hit.size === 0) return false;\n"
    "  _rows[rowId].done = true;\n"
    "  const shared = Array.from(hit).join(', ');\n"
    "  const collided = _pendingCommit.filter((q) => q.paths.concat(q.deleted_paths).some((p) => hit.has(p)));\n"
    "  for (const q of collided) {\n"
    "    _pendingCommit.splice(_pendingCommit.indexOf(q), 1);\n"
    "    _unsettle(q.row_id);\n"
    "  }\n"
    "  _runDirty.clear();\n"
    "  for (const q of _pendingCommit) { q.paths.concat(q.deleted_paths).forEach((p) => _runDirty.add(p)); }\n"
    "  for (const q of collided) {\n"
    "    await _handBackRow(q.row_id, 'entangled', 'a later fix wrote paths this row queued for the terminal commit: ' + shared);\n"
    "  }\n"
    "  await _handBackRow(rowId, 'entangled', 'this fix wrote paths already queued for the terminal commit: ' + shared);\n"
    "  return true;\n"
    "}\n"
    "function _terminalReason(reply) {\n"
    "  const why = reply && (reply.refused || reply.error);\n"
    "  return why ? `: ${why}` : '';\n"
    "}\n"
    "async function _terminalCommit() {\n"
    "  if (_pendingCommit.length === 0) return;\n"
    "  const queued = _pendingCommit.splice(0, _pendingCommit.length).sort((x, y) => (x.row_id < y.row_id ? -1 : x.row_id > y.row_id ? 1 : 0));\n"
    "  _runDirty.clear();\n"
    "  const _failAll = (reason) => {\n"
    "    for (const q of queued) { _unsettle(q.row_id); _handBack(q.row_id, 'commit-failed', reason); }\n"
    "  };\n"
    "  if (!RUN_TOKEN) { _failAll('no run token in the fire args: the terminal commit cannot run' + SETTLE_HINT); return; }\n"
    "  const _halted = _haltReason();\n"
    "  const _params = { script_path: SCRIPT_PATH, run_id: RUN_ID, run_token: RUN_TOKEN, "
    "grind: { profile: PROFILE_NAME, rows: queued, halted: _halted } };\n"
    "  const _size = JSON.stringify(JSON.stringify(_params)).length;\n"
    "  if (_size > PARAMS_BUDGET) { _failAll(`terminal commit params are ${_size} bytes escaped, over the ${PARAMS_BUDGET} the spawn transcript can carry; the rows stay on disk` + SETTLE_HINT); return; }\n"
    "  let reply;\n"
    "  try {\n"
    "    reply = await _terminalCommitCall(_params);\n"
    "  } catch (err) {\n"
    "    _failAll('terminal commit call threw: ' + (err && err.message ? err.message : String(err)) + SETTLE_HINT);\n"
    "    return;\n"
    "  }\n"
    "  if (!reply || reply.committed !== true) { _failAll('terminal commit did not land' + _terminalReason(reply) + SETTLE_HINT); return; }\n"
    "  _terminalCommitDropped = reply.dropped || [];\n"
    "  const _landed = {};\n"
    "  const _hasRows = Array.isArray(reply.rows);\n"
    "  for (const r of (_hasRows ? reply.rows : [])) { _landed[r.row_id] = r; }\n"
    "  const _unlanded = {};\n"
    "  for (const u of (Array.isArray(reply." + contract.UNLANDED_KEY + ") ? reply." + contract.UNLANDED_KEY + " : [])) { _unlanded[u.row_id] = u; }\n"
    "  for (const q of queued) {\n"
    "    const hit = _hasRows ? _landed[q.row_id] : { sha: reply.sha };\n"
    "    if (!hit && _unlanded[q.row_id]) {\n"
    "      _unsettle(q.row_id);\n"
    "      _handBack(q.row_id, 'commit-failed', (_unlanded[q.row_id].reason || 'the already-landed commit did not carry this row') + SETTLE_HINT);\n"
    "      continue;\n"
    "    }\n"
    "    if (!hit) {\n"
    "      const drops = _terminalCommitDropped.filter((d) => d.row_id === q.row_id).map((d) => d.path + ' (' + d.reason + ')');\n"
    "      _unsettle(q.row_id);\n"
    "      _handBack(q.row_id, 'commit-failed', 'every path of this row was dropped from the terminal commit' + (drops.length ? ': ' + drops.join(', ') : '') + SETTLE_HINT);\n"
    "      continue;\n"
    "    }\n"
    "    const sha = hit.sha || reply.sha || '';\n"
    "    _rows[q.row_id].sha = sha;\n"
    "    const s = _settled.find((x) => x.row === q.row_id && x.outcome === 'committed');\n"
    "    if (s) { s.sha = sha; }\n"
    "  }\n"
    "}"
)


def _manifest_const(manifest: Manifest) -> str:
    entries = [
        {"row_id": e.row_id, "path": e.path, "digest": e.digest, "batch_key": e.batch_key,
         **({"pm_ruling": e.pm_ruling} if e.pm_ruling else {})}
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


def _run_close_block(
    *,
    profile_name: str,
    queue_dirs: Sequence[str],
    agent_type_host: Optional[str],
    run_key: str,
    models: Optional[Mapping[str, str]] = None,
    preamble_expr: Optional[str] = None,
) -> str:
    """`_runClose()`: one drain call (`grind-row run-record --sweep`) that sweeps and writes the run
    record whole, retried once if it throws. Twin: ``run_admission``'s ``_run_close``."""
    drain_raw = stages.compose_ledger_sweep_call(
        models=models,
        label="ledger-sweep:drain",
        phase_title="Grind",
        profile=profile_name,
        queue_dirs=queue_dirs,
        run_id_js="RUN_ID",
        record_js="JSON.stringify(_runCostRecord())",
        declared_js="_declaredWrites()",
        decisions_js="args.decisions_file || ''",
        agent_type_host=agent_type_host, run_key=run_key,
    )
    record_path = f"state/queue-grind/{profile_name}/runs/${{RUN_ID}}.json"
    sweep_dead = f"{contract.SWEEP_FAILED_HEAD}: {record_path}"
    return (
        "async function _drainCall() {\n" + _indent_block(_capture(drain_raw, "ledger-sweep"), "  ") + "\n}\n"
        "async function _runClose() {\n"
        "  let _drained;\n"
        "  let _threw;\n"
        "  try { _drained = await _drainCall(); } catch (err) {\n"
        "    try { _drained = await _drainCall(); } catch (err2) { _threw = err2 && err2.message ? err2.message : String(err2); }\n"
        "  }\n"
        f"  if (_threw === undefined && _drained.outcome === 'record-failed' && String(_drained.reason || '').includes({_js_string_literal(contract.RELAY_CORRUPT_HEAD)})) {{\n"
        "    try { _drained = await _drainCall(); } catch (err3) { _threw = err3 && err3.message ? err3.message : String(err3); }\n"
        "  }\n"
        "  if (_threw !== undefined) {\n"
        f"    _handBack(RUN_ID, 'stage-dead', `{sweep_dead}: ${{_threw}}`);\n"
        f"    _handBack(RUN_ID, 'stage-dead', `run record may be missing or incomplete: {record_path}: ${{_threw}}`);\n"
        "    return;\n"
        "  }\n"
        "  if (_drained.swept === false) {\n"
        f"    _handBack(RUN_ID, 'stage-dead', `{sweep_dead}` + (_drained.sweep_reason ? `: ${{_drained.sweep_reason}}` : ''));\n"
        "  }\n"
        "  if (_drained.outcome !== 'recorded') {\n"
        f"    _handBack(RUN_ID, 'stage-dead', `run record may be missing or incomplete: {record_path}` + _commitReason(_drained));\n"
        "  }\n"
        "}"
    )


def _verify_mode_for_key(profile: Profile, node_id: str, batch_key: str) -> tuple[str, Optional[str]]:
    node = profile.graph[node_id]
    verify_map = node.verify or {}
    mode = verify_map.get(batch_key, verify_map.get("default"))
    if isinstance(mode, Mapping):
        return "op", mode.get("op")
    return (mode or "agent"), None

#: Fail-fast check on the fire-time ``args`` before any agent spends a token. A
#: missing arg, or a ``run_stamp`` without the leading ``YYYYMMDD`` that
#: ``grind-row close`` dates rows from, otherwise surfaces rows later as one
#: ``carries no YYYYMMDD date`` refusal per close. The emitter prints the
#: well-formed call.
_FIRE_ARGS_CHECK = (
    "if (!args || !args.run_stamp || !args.script_path || !args.profile_dir || !args.repo_root"
    " || !/^\\d{8}/.test(String(args.run_stamp).replace(/-/g, ''))) {"
    " throw new Error('queue-grind fire args: need {run_stamp, script_path, profile_dir, repo_root},"
    " run_stamp starting YYYYMMDD (e.g. 20260922T221000Z); got '"
    " + JSON.stringify(args ?? null)"
    " + '. Re-fire with the Workflow call emit-dispatch-workflow printed.'); }"
)


#: JS port of `grind_rows.archive_destination`; the commit stage's archive
#: path is computed here, never relayed from the close stage's agent text.
ARCHIVE_DESTINATION_JS = (
    "function _archiveDestination(rowPath) {\n"
    "  const d = String(RUN_ID).replace(/-/g, '').slice(0, 8);\n"
    "  return ARCHIVE_PATH.replace(/\\/+$/, '') + '/' + d.slice(0, 4) + '-' + d.slice(4, 6) + '/' + String(rowPath).split(/[\\\\/]/).pop();\n"
    "}"
)


#: JS twin of ``_slug`` / ``_sizing_path``; the path is built in-script from RUN_ID.
_SIZE_PATH_JS = (
    "function _slug(text) { return String(text == null ? '' : text).toLowerCase()"
    ".replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, ''); }\n"
    "function _sizingPathFor(nameSlug, rowId) {\n"
    "  const d = String(RUN_ID).replace(/-/g, '').slice(0, 8);\n"
    "  return 'state/sizings/' + d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8)"
    " + '-' + nameSlug + '-' + (_slug(rowId) || 'row') + '.yaml';\n"
    "}\n"
    "function _sizingPath(scouted, rowId) { return _sizingPathFor(_slug(scouted.name) || 'sizing', rowId); }"
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
    queue_dirs: Sequence[str] = (),
    commit_trailers: Sequence[str] = (),
    review_roster_fragment: Optional[dict] = None,
    review_stage_schemas: Optional[dict] = None,
    review_run_base_sha: str = "",
    stage_models: Optional[Mapping[str, str]] = None,
    targeted_runner: bool = False,
) -> str:
    """Compose one top-level `.mjs` Workflow script implementing § Design §
    Composer over ``manifest``/``profile``/``knobs``. Pure function of its
    arguments: no clock, no random, no disk read. Assumes the Workflow
    runtime provides ``agent``, ``phase``, ``budget`` (``spent()``/``total``/
    ``remaining()``) and ``args`` globals; the runtime mutex is defined
    in-script (no ``lock`` global is assumed).

    Fire-time ``args`` contract (the launcher supplies these; the script
    bakes none of them, so its bytes never vary with the emitting host
    except through ``commit_trailers``, an explicit emit-time input):
    ``run_stamp`` (run id and the only timestamp), ``script_path`` (this
    script's path, which ``grind-row check --manifest`` reads) and
    ``profile_dir`` (the profile directory, which may live in another repo
    per DR-404's DoE-owned profiles) and ``repo_root`` (the absolute repo
    the manifest was emitted against, binding every stage's cwd and commit).

    ``queue_dirs`` are the repo-relative queue dirs the manifest was
    selected from -- the full set, since the drain's ledger sweep reads a
    queue left out as closed and settles its live rows' ledgers.

    ``commit_trailers`` are host-required trailer lines passed verbatim to
    the review-fix commit's prompt; empty leaves the prompt unchanged. The
    terminal commit takes none: ``dispatch.land_run`` writes each
    row's own ``Grind-Row`` trailer.

    ``review_roster_fragment`` / ``review_stage_schemas`` carry the execute_review
    wave inputs; either being None, or a fragment not at an accepted schema_version (5 or 6),
    raises ``NoReviewStageError`` before anything is composed. The wave runs
    after ``runGrind()`` over the union of committed rows' touched files, and
    is skipped at run time when no row committed. ``review_run_base_sha`` is
    the reviewed range's base, resolved by the caller (this function reads no
    disk); empty leaves the prompt's ``run_base_sha`` placeholder.

    ``targeted_runner`` makes triage, fix and verify-agent run tests only through the
    targeted test runner and makes agent-mode verify honour the runner's outcome:
    `failed|refused|no-runner` force `fail`, with the failing ids as feedback.
    Off, every prompt and the verify tail are unchanged.

    ``preamble`` (optional) is a run-wide posture block declared
    ONCE as a ``const PREAMBLE`` and prepended, at RUN time via a bare
    ``PREAMBLE +`` expression, to every general-purpose (executor-tier)
    stage prompt -- triage, refute-close, size, fix, verify-agent, verify-op.
    Never inlined per call site (mirrors the ``_shared`` write-set array's
    own discipline, ``emit.py``'s ``SharedBlocks``), and never spliced into
    ``verify-op`` or ``commit``, which are not executor prompts. A no-op
    when omitted."""
    if review_roster_fragment is None or review_stage_schemas is None:
        raise NoReviewStageError("no review roster fragment or stage schemas were supplied")
    if review_roster_fragment.get("schema_version") not in ACCEPTED_ROSTER_SCHEMA_VERSIONS:
        raise NoReviewStageError(
            "review roster fragment is schema_version "
            f"{review_roster_fragment.get('schema_version')!r}, not one of "
            f"{sorted(ACCEPTED_ROSTER_SCHEMA_VERSIONS)}"
        )
    review = parse_execute_review(review_roster_fragment)
    review_blocks = compose_execute_review(
        review,
        stage_schemas=review_stage_schemas,
        plan_path="",
        run_base_sha=review_run_base_sha,
        declared_paths_js="_reviewPaths",
        executor_rows_js="_settled.filter((s) => s.outcome === 'committed').length",
        prompt_head=_BRIEF_PRECEDENCE_CLAUSE,
        run_key=Path(run_dir).name,
        host_degraded=agent_type_host == _AGENT_TYPE_HOST_DEGRADED,
        reverify=True,
    )
    review_phase_titles = [title for title, _ in review_blocks]
    run_dir_s = Path(run_dir).as_posix()
    run_key = Path(run_dir).name
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
    size_fix_target = triage_fix_target(routing_table, triage_node_id)

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
    pre_fix_nodes = sorted(pre_fix_verify_nodes(routing_table))
    hand_back_nodes = [n for n in pre_fix_nodes if n in stages.ROUTING_VERIFY_HAND_BACK_NODES]
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
    meta_text = _meta_block("queue-grind:" + profile.name, f"Queue grind over profile {profile.name!r}.", ["Grind", *review_phase_titles, _REVIEW_FIX_PHASE, pm_adjudication.ADJUDICATE_PHASE_TITLE])
    meta_index = len(lines)
    lines.append(meta_text)
    once_per_run_legs: list[str] = []
    lines.append(_manifest_const(manifest))
    lines.append(
        "const BATCHES = " + json.dumps(batches_const, sort_keys=True) + ";"
    )
    lines.append(
        "const ROUTING = " + json.dumps(routing_table, sort_keys=True) + ";"
    )
    lines.append(f"const TRIAGE_NODE_ID = {_js_string_literal(triage_node_id)};")
    lines.append(f"const PLAN_WEIGHT_FLOOR_RANK = {_PLAN_WEIGHT_FLOOR_RANK};")
    lines.append(f"const RETIRE_VERDICTS = {json.dumps(sorted(vocab.RETIRE_VERDICTS))};")
    lines.append(f"const SIZE_FIX_TARGET = {json.dumps(size_fix_target)};")
    lines.append(_MUTEX_HELPERS)
    lines.append(_ROUTING_HELPERS)

    lines.append(f"const WINDOW = {window};")
    lines.append(f"const RESERVE = {batch_reserve(reserve_batch_size)};")
    lines.append(f"const MAX_AGENT_CALLS = {json.dumps(max_agent_calls)};")
    lines.append(f"const BUDGET_TOKENS = {json.dumps(budget_tokens)};")
    # the guard MUST run before any `const ... = args.*` read below --
    # `args` is a fire-time global the Workflow runner supplies, absent
    # entirely on a bare `Workflow({scriptPath})` fire with no `args`. A
    # read ahead of this check throws a raw `TypeError: Cannot read
    # properties of undefined` instead of `_FIRE_ARGS_CHECK`'s named,
    # actionable refusal -- exactly the defect this ordering fixes.
    lines.append(_FIRE_ARGS_CHECK)
    lines.append(f"const RUN_ID = args.run_stamp;")
    lines.append(f"const SCRIPT_PATH = args.script_path;")
    lines.append(f"const PROFILE_NAME = {_js_string_literal(profile.name)};")
    lines.append(f"const ARCHIVE_PATH = {_js_string_literal(profile.archive_path)};")
    lines.append(ARCHIVE_DESTINATION_JS)
    lines.append(stages.SHQ_JS)
    lines.append(contract.RELAY_SHA256_JS)
    lines.append("const PROFILE_DIR = args.profile_dir;")
    # Every stage's cwd, `--repo-root` and commit `--repo` bind here, never to the
    # firing session's ambient cwd.
    lines.append("const REPO_ROOT = args.repo_root;")
    lines.append(stages.SIZE_ASSEMBLE_JS)
    lines.append(_SIZE_PATH_JS)
    # The commit-failed hand-back names the runnable settle-run spelling, not the
    # bare verb: the verb is not a binary and takes required flags.
    _settle_flags = "".join(f" --queue {q}" for q in queue_dirs)
    lines.append(PARAMS_BLOCK_JS)
    lines.append("const RUN_TOKEN = args.run_token || null;")
    lines.append(f"const PARAMS_BUDGET = {PARAMS_ESCAPED_BUDGET};")
    lines.append(
        f"const SETTLE_HINT = ' -- land the finished rows from the run evidence with `' + {_js_string_literal(stages.ASSEMBLE_CMD + ' grind-row settle-run --profile-dir ')}"
        f" + _shq(PROFILE_DIR) + {_js_string_literal(f' --profile {profile.name} --run-id ')} + _shq(RUN_ID)"
        f" + {_js_string_literal(f'{_settle_flags} --repo-root ')}"
        " + _shq(REPO_ROOT) + '`';"
    )
    preamble_expr: Optional[str] = None
    if preamble:
        lines.append(f"const PREAMBLE = {_js_string_literal(preamble)};")
        preamble_expr = "PREAMBLE"
    lines.append(f"const APPETITE_NAME = {_js_string_literal(str(appetite))};")
    lines.append(
        f"const RESOLVED_KNOBS = {json.dumps({k: v for k, v in knobs.items() if k != 'appetite'}, sort_keys=True)};"
    )
    stage_models_field = ""
    if stage_models:
        lines.append(f"const STAGE_MODELS_USED = {json.dumps(dict(stage_models), sort_keys=True)};")
        stage_models_field = "stage_models: STAGE_MODELS_USED, "
    lines.append(f"const MANIFEST_DIGEST = {_js_string_literal(manifest.digest)};")
    lines.append("const TRIAGE_DEPTH_BY_KEY = " + json.dumps(triage_depth_by_key, sort_keys=True) + ";")
    lines.append("const VERIFY_SPEC = " + json.dumps(verify_spec_by_node, sort_keys=True) + ";")
    lines.append("const VERIFY_PRE_FIX = " + json.dumps(pre_fix_nodes) + ";")
    lines.append("const VERIFY_HAND_BACK = " + json.dumps(hand_back_nodes) + ";")

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
    lines.append(
        "const _pendingCommit = [];\n"
        "const _runDirty = new Set();\n"
        "let _grindErr = null;\n"
        "let _terminalCommitDropped = [];\n"
        "let _waveNotRun = [];\n"
        "function _haltReason() {\n"
        "  if (_exhausted) return 'budget-exhausted';\n"
        "  return _grindErr ? (String(_grindErr && _grindErr.message ? _grindErr.message : _grindErr) || 'error') : null;\n"
        "}"
    )
    lines.append(f"const HISTORICAL_PREFIXES = {json.dumps(list(contract.HISTORICAL_RECORD_PREFIXES))};")
    lines.append(
        "function _normRel(p) { return String(p).replace(/\\\\/g, '/').replace(/^(\\.\\/)+/, ''); }\n"
        "function _historicalPaths(paths) { return (paths || []).filter((p) => HISTORICAL_PREFIXES.some((h) => _normRel(p).startsWith(h))); }\n"
        "function _union(a, b) { return Array.from(new Set((a || []).concat(b || []))); }"
    )
    lines.append("const _byRefuteOrigin = {};")
    lines.append(
        "const _byTriageCheck = { "
        + ", ".join(f"{k}: 0" for k in _SIZE_CHECK_KEYS)
        + " };"
    )
    lines.append(
        "function _bumpRefuteOrigin(origin, key) {\n"
        "  if (!_byRefuteOrigin[origin]) { _byRefuteOrigin[origin] = { confirmed: 0, refuted: 0 }; }\n"
        "  _byRefuteOrigin[origin][key] += 1;\n"
        "}"
    )
    lines.append(
        f"const REPEAT_REFUSED_HEAD = {json.dumps(_REPEAT_REFUSED_HEAD)};\n"
        "function _handBack(rowId, type, reason, tradeoff, ruling) {\n"
        "  const _row = _rows[rowId];\n"
        "  if (_row && _row.refuteNote) {\n"
        "    reason = `${reason} (revived from a refuted close proposal, origin ${_row.origin}: ${_row.refuteNote})`;\n"
        "  }\n"
        "  if (_handedBack.some((h) => h.row === rowId && h.type === type)) return;\n"
        "  const _entry = tradeoff ? { row: rowId, type, reason, tradeoff } : { row: rowId, type, reason };\n"
        "  if (ruling) { _entry.ruling = ruling; }\n"
        "  if (_row && _row.adjudication && PM_BOUND_TYPES.includes(type)) { _entry.adjudication = _row.adjudication; }\n"
        "  _handedBack.push(_entry);\n"
        "}\n"
        "function _normalizeRowRef(items, ref) {\n"
        "  const hit = items.find((it) => it.path === ref);\n"
        "  return hit ? hit.row_id : ref;\n"
        "}"
    )

    # ONE composed call site per stage kind (verify: one per mode) --
    # row/batch-count-independent. Each function takes the live row/batch
    # object and interpolates its runtime fields via grind_stages' `*_js`
    # params; per-batch-key variation reads TRIAGE_DEPTH_BY_KEY/VERIFY_SPEC.
    triage_raw = stages.compose_triage_call(
        models=stage_models,
        label="triage", phase_title="Grind", run_dir=run_dir_s, profile=profile.name,
        verdicts=profile.verdicts,
        batch_id_js="batchId", triage_depth_js="TRIAGE_DEPTH_BY_KEY[batchKey]",
        rows_js="JSON.stringify(rowsData)", script_path_js="SCRIPT_PATH", run_id_js="RUN_ID",
        agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
        targeted_runner=targeted_runner,
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

    size_raw = stages.compose_size_call(
        models=stage_models,
        label="size", phase_title="Grind",
        row_js="{ row_id: row.rowId, path: row.path, digest: row.digest }",
        evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
    )
    lines.append("async function _sizeCall(row) {\n" + _indent_block(_capture(size_raw, "size"), "  ") + "\n}")

    if has_close_node:
        close_raw = stages.compose_refute_close_call(
            models=stage_models,
            label="close", phase_title="Grind", profile=profile.name,
            profile_dir_js="PROFILE_DIR",
            proposals_js="JSON.stringify(proposals)", run_id_js="RUN_ID", agent_type_host=agent_type_host, run_key=run_key,
            preamble_expr=preamble_expr,
        )
        lines.append(
            "async function _closeCall(proposals) {\n" + _indent_block(_capture(close_raw, "refute-close"), "  ") + "\n}"
        )

    fix_raw = stages.compose_fix_call(
        models=stage_models,
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
        ruling_note_js="(row.adjudication && row.adjudication.actionable ? (' Binding ruling on this row: ' + row.pmRuling) : '')",
        profile=profile.name, profile_dir_js="PROFILE_DIR", row_path_js="row.path", digest_js="row.digest",
        run_id_js="RUN_ID", agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
        targeted_runner=targeted_runner,
    )
    lines.append("async function _fixCall(row) {\n" + _indent_block(_capture(fix_raw, "fix"), "  ") + "\n}")

    lines.append(
        f"const _UNDO_EXEMPT = {json.dumps(list(_UNDO_EXEMPT))};\n"
        "function _undoPaths(row) {\n"
        "  return Array.from(new Set(row.touchedFiles.concat(row.createdFiles || [])"
        ".concat(row.removedFiles || []))).filter((p) => _outOfRootPaths([p]).length === 0);\n"
        "}\n"
        "function _hasWrites(row) { return row.fixReported || (row.createdFiles || []).length > 0 || (row.removedFiles || []).length > 0; }"
    )
    reject_raw = stages.compose_verify_op_call(
        models=stage_models,
        label="verify-op", phase_title="Grind", run_dir=run_dir_s,
        agent_type_host=agent_type_host, run_key=run_key,
        reject_paths_js="_undoPaths(row)",
        reject_reason_js="row.undoReason",
    )
    lines.append(
        "async function _rejectUndoCall(row) {\n" + _indent_block(_capture(reject_raw, "verify"), "  ") + "\n}"
    )
    lines.append(
        "function _ruledFixNode(row) {\n"
        "  const _verdict = String(row.origin || '').startsWith('triage:') ? row.origin.slice(7) : '';\n"
        "  if (_verdict) {\n"
        "    const _r = routeAfterTriage(_verdict, null, '', row.pmRuling, '');\n"
        "    if (_r.kind === 'node') return _r.value;\n"
        "  }\n"
        "  return SIZE_FIX_TARGET && ROUTING[SIZE_FIX_TARGET] ? SIZE_FIX_TARGET : null;\n"
        "}\n"
        "function _reloopRuled(row, node, undone) {\n"
        "  row.pmRuling = row.adjudication.ruling;\n"
        "  if (undone) { row.touchedFiles = [row.path]; row.createdFiles = []; row.removedFiles = []; row.fixReported = false; row.closeResult = null; }\n"
        "  row.done = false;\n"
        "  row.node = node;\n"
        "}\n"
        "async function _handBackRow(rowId, type, reason, tradeoff, ruling) {\n"
        "  const row = _rows[rowId];\n"
        "  row.done = true;\n"
        "  let _reloop = null;\n"
        "  if (PM_BOUND_TYPES.includes(type) && !row.ruledOnce) {\n"
        "    row.ruledOnce = true;\n"
        "    const _entry = tradeoff ? { row: rowId, type, reason, tradeoff } : { row: rowId, type, reason };\n"
        "    row.adjudication = await _issue(row.batchId, rowId, () => _adjudicateHandback(_entry));\n"
        "    if (row.adjudication.actionable && row.adjudication.ruling) { _reloop = _ruledFixNode(row); }\n"
        "  }\n"
        "  if (_UNDO_EXEMPT.includes(type) || !_hasWrites(row) || _undoPaths(row).length === 0) {\n"
        "    if (_reloop) { _reloopRuled(row, _reloop, false); return; }\n"
        "    const _left = _undoPaths(row);\n"
        f"    if (type === 'peer-dirty' && _hasWrites(row) && _left.length > 0) {{ reason = reason + '; ' + {json.dumps(_NOT_UNDONE_PEER)} + ': ' + _left.join(', '); }}\n"
        "    _handBack(rowId, type, reason, tradeoff, ruling); return;\n"
        "  }\n"
        "  row.undoReason = `the row was handed back ${type}: ${reason}`;\n"
        "  let _undone;\n"
        "  try { _undone = ((await _issue(row.batchId, rowId, () => withLock(_lockKeysFor(row, rowId), async () => _rejectUndoCall(row)))) || {}).undo || {}; }\n"
        "  catch (err) { _undone = { outcome: 'undo-failed', reason: err && err.message ? err.message : String(err) }; }\n"
        "  if (_undone.outcome !== 'undone') {\n"
        "    _handBack(rowId, 'stage-dead', 'undo-failed: the row was handed back ' + type + ' and its edits may remain in the tree'"
        " + (_undone.reason ? ` (${_undone.reason})` : (_undone.outcome ? '' : ' (the verify stage returned no undo reply)')) + '" + _UNDO_FAILED_TAIL + "');\n"
        "    return;\n"
        "  }\n"
        "  if (_reloop) { _reloopRuled(row, _reloop, true); return; }\n"
        "  _handBack(rowId, type, reason + '; undone', tradeoff, ruling);\n"
        "}\n"
        "async function _applyRouteRow(row, rowId, route, reason) {\n"
        "  if (route.kind === 'handback') { await _handBackRow(rowId, route.value, reason); }\n"
        "  else { row.node = route.value; }\n"
        "}\n"
        "async function _undoUnfinished() {\n"
        "  if (!_grindErr) return;\n"
        "  const _msg = _grindErr && _grindErr.message ? _grindErr.message : String(_grindErr);\n"
        "  for (const r of Object.keys(_rows)) {\n"
        "    if (!_rows[r].done && _hasWrites(_rows[r])) { await _handBackRow(r, 'stage-dead', `run threw: ${_msg}`); }\n"
        "  }\n"
        "}"
    )
    verify_agent_raw = stages.compose_verify_agent_call(
        models=stage_models,
        label="verify-agent", phase_title="Grind",
        row_id_js="row.rowId", row_path_js="row.path", touched_files_js="row.touchedFiles",
        evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
        targeted_runner=targeted_runner, profile=profile.name, run_id_js="RUN_ID",
        final_js="row.verifyRetried", undo_paths_js="_undoPaths(row)",
        digest_js="row.digest",
    )
    verify_routing_raw = stages.compose_verify_agent_call(
        models=stage_models,
        label="verify-routing", phase_title="Grind",
        row_id_js="row.rowId", row_path_js="row.path", touched_files_js="row.touchedFiles",
        evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
        targeted_runner=targeted_runner, profile=profile.name, run_id_js="RUN_ID",
        routing=True, digest_js="row.digest",
    )
    verify_routing_hand_back_raw = stages.compose_verify_agent_call(
        models=stage_models,
        label="verify-routing-hand-back", phase_title="Grind",
        row_id_js="row.rowId", row_path_js="row.path", touched_files_js="row.touchedFiles",
        evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        agent_type_host=agent_type_host, run_key=run_key, preamble_expr=preamble_expr,
        targeted_runner=targeted_runner, profile=profile.name, run_id_js="RUN_ID",
        routing=True, digest_js="row.digest", hand_back=True,
    )
    _runner_fail = json.dumps([
        o for o in stages.RUNNER_FAIL_OUTCOMES if profile.name == "bug" or o != "no-runner"
    ])
    verify_agent_tail = (
        "  const _forced = " + _runner_fail + ".includes(_result.tests_outcome);\n"
        "  if (_forced) {\n"
        "    const _ids = (_result.tests_failing_ids || []).join(', ');\n"
        "    return { outcome: 'fail', reason: 'targeted tests ' + _result.tests_outcome"
        " + (_ids ? ': ' + _ids : ''), undo: _result.undo };\n"
        "  }\n"
        "  return _result;"
    )
    verify_op_raw = stages.compose_verify_op_call(
        models=stage_models,
        label="verify-op", phase_title="Grind", run_dir=run_dir_s, op_js="spec.op", batch_id_js="row.batchId",
        agent_type_host=agent_type_host, run_key=run_key,
        row_id_js="row.rowId", row_path_js="row.path",
        final_js="row.verifyRetried", undo_paths_js="_undoPaths(row)",
        digest_js="row.digest", profile=profile.name, run_id_js="RUN_ID",
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
                    "  return { outcome: _pass ? 'pass' : 'fail', reason: JSON.stringify(_result.output), undo: _result.undo };"
                ),
            ),
            "    ",
        )
        + "\n  }\n"
        + (
            "  if (VERIFY_PRE_FIX.includes(row.node)) {\n"
            + (
                "    if (VERIFY_HAND_BACK.includes(row.node)) {\n"
                + _indent_block(
                    _capture(
                        verify_routing_hand_back_raw, "verify",
                        tail=verify_agent_tail if targeted_runner else None,
                    ),
                    "    ",
                )
                + "\n    }\n"
                if hand_back_nodes
                else ""
            )
            + _indent_block(
                _capture(verify_routing_raw, "verify", tail=verify_agent_tail if targeted_runner else None),
                "    ",
            )
            + "\n  }\n"
            if pre_fix_nodes
            else ""
        )
        + _indent_block(
            _capture(verify_agent_raw, "verify", tail=verify_agent_tail if targeted_runner else None),
            "  ",
        )
        + "\n}"
    )

    commit_raw = stages.compose_grind_terminal_commit_call(
        models=stage_models,
        label="commit", phase_title="Grind",
        params_file_js="_paramsFile", params_js="params",
        agent_type_host=agent_type_host, run_key=run_key,
    )
    terminal_commit_text = (
        "async function _terminalCommitCall(params) {\n"
        f"  const _paramsFile = REPO_ROOT.replace(/\\/+$/, '') + {_js_string_literal('/' + RUN_DIR_ROOT + '/')}"
        f" + RUN_ID + {_js_string_literal('/' + PARAMS_FILE_NAME)};\n"
        + _indent_block(_capture(commit_raw, "commit"), "  ")
        + "\n}"
    )
    once_per_run_legs.append(terminal_commit_text)
    lines.append(terminal_commit_text)

    lines.append(
        "function _counts() {\n  const by_type = {};\n  for (const h of _handedBack) { by_type[h.type] = (by_type[h.type] || 0) + 1; }\n  const by_outcome = {};\n  for (const s of _settled) { by_outcome[s.outcome] = (by_outcome[s.outcome] || 0) + 1; }\n  return { by_type, by_outcome, by_refute_origin: _byRefuteOrigin, by_triage_check: _byTriageCheck };\n}\nfunction _spend() {\n  return { output_tokens: budget.spent() - _startSpent, agent_calls_total: _callCount, agent_calls_by_stage_kind: _agentCallsByStageKind };\n}\nfunction _runCostRecord() {\n  return { profile: PROFILE_NAME, appetite: APPETITE_NAME, run_id: RUN_ID, resolved_knobs: RESOLVED_KNOBS, " + stage_models_field + "manifest_digest: MANIFEST_DIGEST, counts: _counts(), spend: _spend(), handed_back: _handedBack, halted: _haltReason() !== null, halted_reason: _haltReason(), terminal_commit_dropped: _terminalCommitDropped };\n}\nfunction _declaredWrites() {\n  const out = [];\n  for (const r of Object.keys(_rows)) { if (_rows[r].origin) out.push(_rows[r].path); if (_rows[r].sizingPath) out.push(_rows[r].sizingPath); }\n  return Array.from(new Set(out));\n}"
    )
    lines.append(
        "const _rows = {};\n"
        "for (const b of BATCHES) {\n"
        "  for (const r of b.rows) {\n"
        "    const _entry = QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r);\n"
        "    const _path = _entry.path;\n"
        "    _rows[r] = { rowId: r, batchId: b.id, batchKey: b.batch_key, path: _path, digest: _entry.digest, pmRuling: _entry.pm_ruling || '', node: null, "
        "declaredFiles: [_path], touchedFiles: [_path], removedFiles: [], createdFiles: [], "
        "evidence: '', verifyFeedback: '', fixPlan: '', fixNode: null, closeResult: null, lastOutcome: '', "
        "origin: '', refuteNote: '', fixReported: false, sizingPath: '', undoReason: '', "
        "widened: false, verifyRetried: false, dedupeFlipped: false, onFailUsed: false, done: false, sha: '', ruledOnce: false, adjudication: null };\n"
        "  }\n"
        "}"
    )

    lines.append(_TERMINAL_COMMIT_JS)

    lines.append(
        "function _pendingRow(batch) {\n"
        "  for (const r of batch.rows) { const row = _rows[r]; if (!row.done && row.node) return r; }\n"
        "  return null;\n"
        "}\n"
        "function _batchDone(batch) { return batch.rows.every((r) => _rows[r].done); }"
    )

    lines.append(
        "async function _triageBatch(batchState) {\n"
        "  const batch = BATCHES.find((b) => b.id === batchState.id);\n"
        "  const rowsMeta = batch.rows.map((r) => "
        "({ row_id: r, path: (QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r) || {}).path }));\n"
        "  const _out = await _issue(batchState.id, '', () => _triageCall(batchState.id, batchState.batchKey, batch.rows));\n"
        "  batchState.triaged = true;\n"
        "  const _seen = new Set();\n"
        "  const _toSize = [];\n"
        "  for (const rec of (_out.rows || [])) {\n"
        "    const recRow = _normalizeRowRef(rowsMeta, rec.row);\n"
        "    if (!batch.rows.includes(recRow)) continue;\n"
        "    const row = _rows[recRow];\n"
        "    if (!row) continue;\n"
        "    _seen.add(recRow);\n"
        "    row.evidence = rec.evidence || '';\n"
        "    row.fixPlan = rec.fix_plan || '';\n"
        "    if (rec.declared_files && rec.declared_files.length) { row.declaredFiles = rec.declared_files; }\n"
        "    _byTriageCheck.triaged += 1;\n"
        "    row.origin = `triage:${rec.verdict}`;\n"
        "    const tradeoff = rec.has_tradeoff ? (rec.tradeoff || '') : '';\n"
        "    const route = routeAfterTriage(rec.verdict, rec.tshirt_size, tradeoff, row.pmRuling, rec.ruling_on_tradeoff);\n"
        "    if (route.kind === 'handback' && route.value === 'baton') { _toSize.push(recRow); continue; }\n"
        "    if (route.kind === 'handback') { _byTriageCheck.triage_handback += 1; }\n"
        "    if (tradeoff && route.kind === 'handback' && route.value === 'repeat-refused') { _byTriageCheck.repeat_refused += 1; applyRoute(row, recRow, route, REPEAT_REFUSED_HEAD + row.pmRuling, tradeoff, row.pmRuling); }\n"
        "    else if (tradeoff && route.kind === 'handback' && route.value === 'needs-judgment') { await _handBackRow(recRow, 'needs-judgment', `triage reported a tradeoff: ${tradeoff}`, tradeoff); }\n"
        "    else {\n"
        "      if (tradeoff) { _byTriageCheck.ruled_tradeoff += 1; }\n"
        "      if (route.kind === 'handback' && PM_BOUND_TYPES.includes(route.value)) { await _handBackRow(recRow, route.value, `triage verdict ${rec.verdict}`); }\n"
        "      else { applyRoute(row, recRow, route, `triage verdict ${rec.verdict}`); }\n"
        "    }\n"
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
        "  for (const rid of _toSize) {\n"
        "    const row = _rows[rid];\n"
        "    const scouted = await _issue(batchState.id, rid, () => _sizeCall(row));\n"
        "    if (!scouted.tshirt) {\n"
        "      row.done = true;\n"
        "      _handBack(rid, 'baton', 'size scout returned no size' + (scouted.reason ? ': ' + scouted.reason : ''));\n"
        "      continue;\n"
        "    }\n"
        "    const _belowFloor = _tshirtRank(scouted.tshirt) < PLAN_WEIGHT_FLOOR_RANK;\n"
        "    const _ownTarget = ROUTING[TRIAGE_NODE_ID].edges[String(row.origin).replace(/^triage:/, '')];\n"
        "    if (_belowFloor && SIZE_FIX_TARGET && ROUTING[SIZE_FIX_TARGET] && _ownTarget === SIZE_FIX_TARGET) {\n"
        "      _byTriageCheck.size_collapsed += 1;\n"
        "      row.origin = 'size:below-plan-weight';\n"
        "      row.node = SIZE_FIX_TARGET;\n"
        "      continue;\n"
        "    }\n"
        "    if (!_belowFloor) { _byTriageCheck.size_confirmed += 1; }\n"
        "    row.done = true;\n"
        "    const _intent = scouted.intent || '';\n"
        "    if (scouted.exit_code === 0) { row.sizingPath = _sizingPath(scouted, rid); _handBack(rid, 'baton', 'sized at ' + row.sizingPath + ': ' + _intent); }\n"
        "    else { _handBack(rid, 'baton', _intent + ' (size step failed: ' + _sizeFailReason(scouted) + ')'); }\n"
        "  }\n"
        "}"
    )

    lines.append(
        "async function _closeBatch(batchState) {\n"
        "  const closeNodeIds = Object.keys(ROUTING).filter((n) => ROUTING[n].kind === 'refute-close');\n"
        "  const batch = BATCHES.find((b) => b.id === batchState.id);\n"
        "  const proposalIds = batch.rows.filter((r) => closeNodeIds.includes(_rows[r].node) && !_rows[r].done && !batchState.closedRows.has(r));\n"
        "  const proposals = proposalIds.map((r) => {\n"
        "    const row = _rows[r];\n"
        "    return { row_id: r, path: row.path, digest: (QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r) || {}).digest, evidence: row.evidence, origin: row.origin };\n"
        "  });\n"
        "  const result = await _issue(batchState.id, '', () => _closeCall(proposals));\n"
        "  for (const r of proposalIds) { batchState.closedRows.add(r); }\n"
        "  for (const item of (result.confirmed || [])) {\n"
        "    const itemRow = _normalizeRowRef(proposals, item.row);\n"
        "    const row = _rows[itemRow];\n"
        "    if (!row || row.done || !row.node) continue;\n"
        "    _bumpRefuteOrigin(row.origin, 'confirmed');\n"
        "    row.removedFiles = row.removedFiles.concat([row.path]);\n"
        "    const _archived = _archiveDestination(row.path);\n"
        "    row.touchedFiles = row.touchedFiles.concat([_archived]);\n"
        "    row.closeResult = { old: row.path, new: _archived };\n"
        "    row.lastOutcome = 'confirmed';\n"
        "    const route = followEdge(row.node, 'confirmed', row);\n"
        "    applyRoute(row, itemRow, route, 'refute-close confirmed');\n"
        "    if (route.kind === 'handback') { _queueCommit(itemRow); }\n"
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
        "  const _overlap = row.declaredFiles.filter((p) => _runDirty.has(p));\n"
        "  if (_overlap.length) { row.done = true; _handBack(rowId, 'overlap-deferred', 'declared files overlap rows queued for the terminal commit: ' + _overlap.join(', ') + '; its ledger is left for the next grind'); return; }\n"
        "  const lockKeys = _lockKeysFor(row, rowId);\n"
        "  const _priorNode = row.node;\n"
        "  const result = await _issue(row.batchId, rowId, () => withLock(lockKeys, async () => _fixCall(row)));\n"
        "  const tradeoff = result.has_tradeoff ? (result.tradeoff || '') : '';\n"
        "  const outcome = result.outcome;\n"
        "  row.lastOutcome = outcome;\n"
        "  if (result.touched_files && result.touched_files.length) { row.touchedFiles = _union(row.fixReported ? row.touchedFiles : [], result.touched_files); row.fixReported = true; }\n"
        "  else if (outcome === 'done' && !row.fixReported) { row.touchedFiles = row.declaredFiles; }\n"
        "  row.createdFiles = _union(row.createdFiles, result.created_files);\n"
        "  if (outcome === 'done' && result.close_result) {\n"
        "    const _archived = _archiveDestination(row.path);\n"
        "    row.removedFiles = row.removedFiles.concat([row.path]);\n"
        "    row.touchedFiles = row.touchedFiles.concat([_archived]);\n"
        "    row.closeResult = { old: row.path, new: _archived };\n"
        "  }\n"
        "  const _disp = _tradeoffDisposition(tradeoff, row.pmRuling, result.ruling_on_tradeoff);\n"
        "  if (_disp === 'repeat-refused') { _byTriageCheck.repeat_refused += 1; await _handBackRow(rowId, 'repeat-refused', REPEAT_REFUSED_HEAD + row.pmRuling, tradeoff, row.pmRuling); return; }\n"
        "  if (_disp === 'needs-judgment') { _byTriageCheck.fix_late_tradeoff += 1; await _handBackRow(rowId, 'needs-judgment', 'fix reported a tradeoff: ' + tradeoff, tradeoff); return; }\n"
        "  if (_disp === 'accepted') { _byTriageCheck.ruled_tradeoff += 1; }\n"
        "  if (outcome === 'NEEDS_PLAN') { _byTriageCheck.fix_needs_plan += 1; await _handBackRow(rowId, 'baton', 'fix reported NEEDS_PLAN'); return; }\n"
        "  if (outcome === 'PEER_DIRTY') { await _handBackRow(rowId, 'peer-dirty', 'fix reported PEER_DIRTY'); return; }\n"
        "  if (outcome === 'MANIFEST_STALE') { await _handBackRow(rowId, 'manifest-stale', 'fix reported MANIFEST_STALE'); return; }\n"
        "  if (outcome === 'NEEDS_WIDER_SCOPE') {\n"
        "    if (!row.widened) { row.widened = true; row.declaredFiles = row.declaredFiles.concat(result.extra_files || []); return; }\n"
        "    await _handBackRow(rowId, 'widen-exhausted', 'second NEEDS_WIDER_SCOPE'); return;\n"
        "  }\n"
        "  const _escaped = _outOfRootPaths((result.touched_files || []).concat(result.created_files || []));\n"
        "  if (_escaped.length) { await _handBackRow(rowId, 'needs-judgment', 'fix touched files outside repo_root (a cross-repo commit needs per-run PM assent; left uncommitted): ' + _escaped.join(', ')); return; }\n"
        "  const _written = (result.touched_files || []).concat(result.created_files || []);\n"
        "  const _historical = _historicalPaths(_written);\n"
        "  if (_historical.length) {\n"
        "    row.done = true;\n"
        "    row.undoReason = 'the fix wrote historical records';\n"
        "    const _undone = ((await _issue(row.batchId, rowId, () => withLock(lockKeys, async () => _rejectUndoCall(row)))) || {}).undo || {};\n"
        "    if (_undone.outcome !== 'undone') { _handBack(rowId, 'stage-dead', '" + _HISTORICAL_UNDO_FAILED_HEAD + "' + (_undone.reason ? ` (${_undone.reason})` : (_undone.outcome ? '' : ' (the verify stage returned no undo reply)')) + '" + _UNDO_FAILED_TAIL + "'); return; }\n"
        "    _handBack(rowId, 'needs-judgment', 'fix wrote historical records, undone: ' + _historical.join(', ')); return;\n"
        "  }\n"
        "  if (await _entangle(rowId, _written)) return;\n"
        "  if (outcome === 'NOT_REPRODUCED') { _byTriageCheck.fix_not_reproduced += 1; }\n"
        "  row.fixNode = _priorNode;\n"
        "  await _applyRouteRow(row, rowId, followEdge(row.node, outcome, row), `fix outcome ${outcome}`);\n"
        "}"
    )

    lines.append(
        "async function _verifyStage(rowId) {\n"
        "  const row = _rows[rowId];\n"
        "  const lockKeys = _lockKeysFor(row, rowId);\n"
        "  const result = await _issue(row.batchId, rowId, () => withLock(lockKeys, async () => _verifyCall(row)));\n"
        "  if (result.outcome === 'fail') {\n"
        "    if (result.covered_by && VERIFY_PRE_FIX.includes(row.node) && !row.dedupeFlipped && row.origin !== 'triage:dedupe') {\n"
        "      const _flip = routeAfterTriage('dedupe', null, '');\n"
        "      if (_flip.kind === 'node') {\n"
        "        row.dedupeFlipped = true;\n"
        "        row.verifyRetried = true;\n"
        "        row.origin = 'triage:dedupe';\n"
        "        row.evidence = 'already covered at ' + result.covered_by;\n"
        "        row.node = _flip.value;\n"
        "        return;\n"
        "      }\n"
        "    }\n"
        "    if (!row.verifyRetried) {\n"
        "      row.verifyRetried = true;\n"
        "      row.verifyFeedback = result.reason || '';\n"
        "      row.node = row.fixNode || row.node;\n"
        "      return;\n"
        "    }\n"
        "    row.done = true;\n"
        "    const _why = 'verify failed twice' + (result.reason ? ': ' + String(result.reason).slice(0, 400) : '');\n"
        "    if (VERIFY_PRE_FIX.includes(row.node)) { _handBack(rowId, 'rejected-after-retry', _why); return; }\n"
        "    const _undone = result.undo || {};\n"
        "    if (_undone.outcome !== 'undone') { _handBack(rowId, 'stage-dead', '" + _UNDO_FAILED_HEAD + "' + (_undone.reason ? ` (${_undone.reason})` : (_undone.outcome ? '' : ' (the verify stage returned no undo reply)')) + '" + _UNDO_FAILED_TAIL + "'); return; }\n"
        "    _handBack(rowId, 'rejected-after-retry', _why); return;\n"
        "  }\n"
        "  await _applyRouteRow(row, rowId, followEdge(row.node, result.outcome, row), `verify outcome ${result.outcome}`);\n"
        "}"
    )

    lines.append(
        "async function _commitStage(rowId) {\n"
        "  _rows[rowId].done = true;\n"
        "  _queueCommit(rowId);\n"
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
        "    if (!batchState.closedRows.has(rowId)) { await _closeBatch(batchState); }\n"
        "    else { row.done = true; _handBack(rowId, 'stage-dead', 'refute-close already called for this batch'); }\n"
        "  }\n"
        "  else { row.done = true; _handBack(rowId, 'stage-dead', `unhandled node kind ${kind}`); }\n"
        "}"
    )

    adjudicate_text = pm_adjudication.compose_adjudicate_block(agent_type_host=agent_type_host, run_key=run_key, models=stage_models)
    once_per_run_legs.append(adjudicate_text)
    lines.append(adjudicate_text)

    run_close_text = _run_close_block(profile_name=profile.name, queue_dirs=queue_dirs, agent_type_host=agent_type_host, run_key=run_key, models=stage_models)
    once_per_run_legs.append(run_close_text)
    lines.append(run_close_text)
    lines.append(
        "const _admitted = {};\n"
        "const _queue = BATCHES.map((b) => b.id);\n"
        "function _anyAdmittedHasPendingDownstream() {\n"
        "  return Object.values(_admitted).some((bs) => !_batchDone(BATCHES.find((b) => b.id === bs.id)) && "
        "_pendingRow(BATCHES.find((b) => b.id === bs.id)) !== null);\n"
        "}\n"
        + _ISSUE_GATE_JS
        + "\n"
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
        "      if (!_rows[r].done) { await _handBackRow(r, 'stage-dead', `batch worker threw: ${_msg}`); }\n"
        "    }\n"
        "  }\n"
        "  delete _admitted[batchState.id];\n"
        "  _gateTick();\n"
        "}\n"
        "async function runGrind() {\n"
        "  _grindOpen = true;\n"
        "  try { await new Promise((resolve) => { _gateResolve = resolve; _gateTick(); }); }\n"
        "  finally { _grindOpen = false; }\n"
        "  if (_workerFailure) { throw _workerFailure.err; }\n"
        "  if (_exhausted) {\n"
        "    for (const bid of _queue) { for (const r of BATCHES.find((b) => b.id === bid).rows) "
        "_handBack(r, 'budget-exhausted', " + repr(_EXHAUSTED_REASON) + "); }\n"
        "  }\n"
        "}\n"
        "try {\n"
        "try { await runGrind(); } catch (e) { _grindErr = e; throw e; } finally { await _terminalCommit(); await _undoUnfinished(); }"
    )

    review_fix_raw = stages.compose_review_fix_commit_call(
        models=stage_models,
        touched_files_js="_reviewPaths", label="review-fix-commit", phase_title=_REVIEW_FIX_PHASE,
        profile=profile.name, agent_type_host=agent_type_host, run_key=run_key, trailers=commit_trailers,
    )
    review_fix_call = _capture(review_fix_raw, "review-fix-commit")
    once_per_run_legs.extend(block for _, block in review_blocks)
    once_per_run_legs.append(review_fix_call)
    lines.append(
        "const _reviewPaths = Array.from(new Set(_settled.filter((s) => s.outcome === 'committed')"
        ".flatMap((s) => (_rows[s.row] || {}).touchedFiles || []))).sort();\n"
        "if (_reviewPaths.length === 0) {\n"
        "  log('execute_review skipped: no row committed, nothing landed to review');\n"
        "} else {\n"
        + "\n".join(block for _, block in review_blocks)
        # Trap: the terminal commit already landed the rows in runGrind's finally before review
        # started, so "Review fixes" is the only commit review edits reach; a red re-verify
        # verdict stops them here.
        + "\n  const _reviewFixWithheld = _reviewReverify ? "
        + f"['fail', '{REVERIFY_INDETERMINATE}'].includes(_reviewReverify.status_after ?? _reviewReverify.status_before)"
        + " : !_verifyOnly;\n"
        + "  if (!_reviewFixWithheld) {\n"
        + f"  phase({_js_string_literal(_REVIEW_FIX_PHASE)});\n"
        + "  await (async () => {\n"
        + _indent_block(review_fix_call, "  ")
        + "\n  })();\n"
        + "  } else {\n"
        + "    log('review fixes withheld: targeted-tests-failed');\n"
        + "  }\n}"
    )

    lines.append(
        "await _adjudicatePmBound();\n"
        "} catch (e) { _grindErr = e; throw e; } finally { await _runClose(); }"
    )

    lines.append(
        "const HANDBACK = {\n"
        "  schema: 'queue-grind-handback/1',\n"
        "  profile: PROFILE_NAME,\n"
        "  appetite: APPETITE_NAME,\n"
        "  handed_back: _handedBack,\n"
        "  declined: QUEUE_GRIND_MANIFEST.declined,\n"
        "  settled: _settled,\n"
        "  counts: _counts(),\n"
        "  spend: _spend(),\n"
        "};"
    )
    lines.append("return HANDBACK;")

    # Per-row legs (size/fix/reject/verify/close), a retried drain and one adjudicate call per
    # PM-bound row inside the loop route at run time, so the count is a floor.
    lines[meta_index] = with_fanout(
        meta_text,
        FanoutCost(
            units=len(manifest.entries),
            agents=len(grouped) + sum(len(AGENT_CALL_RE.findall(scrub(leg))) for leg in once_per_run_legs),
            basis="lower-bound",
            groups=tuple(tuple(e.row_id for e in entries) for _, entries in grouped),
        ),
    )
    return "\n\n".join(lines) + "\n"
