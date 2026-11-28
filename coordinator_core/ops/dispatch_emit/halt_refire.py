"""The one-refire halt envelope: builder, validator and the emitted JS finaliser body.

Leaf module: no I/O, no git, and it never imports ask_compose (ask_compose imports it).
A halt's `next_action` names exactly one engine call and never a hand edit; a decision the
refire reads rides under `input`.
"""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_DESIGN_REVIEW
from coordinator_core.ops.dispatch_emit.phase_ledger import HALT_PHASE, PHASES
from coordinator_core.ops.dispatch_emit.write_baseline import (
    DIRTY_BEFORE_RUN_KEY,
    MEANT_TO_LAND_KEY,
    NO_CHANGE_REFUSAL,
    NON_REFIRE_REFUSALS,
)
from coordinator_core.ops.dispatch_emit.run_truth_contract import MutationReply

REFIRE_KIND = "refire"
APPLIED_KEY = "applied"
REFUSED_KEY = "refused"
EXIT_KIND = "exit"
ROUTE_REVIEW_ONLY = "review-only"
ROUTE_REVISE = "revise"
ROUTE_ADOPT = "adopt"
ROUTE_LANDED = "landed"
EXIT_ROUTES = (ROUTE_REVIEW_ONLY, ROUTE_REVISE, ROUTE_ADOPT, ROUTE_LANDED)
# Twin of review_mint.execute_review.NO_SLICES_HALT (this leaf cannot import it); a test pins equality.
NO_SLICES_HALT_PREFIX = "review prep returned no slices"
# plan-blitz's `agentIncidents[].kind` for a roster it could not read: a refire re-enters the same read.
ROSTER_UNREADABLE_KIND = "roster-unreadable"
# plan-blitz `trackAgent` labels and incident kinds of a reviewer reply with no usable verdict; a refire can clear it,
# so it never joins the unchanged-cause set (`_causeUnchangedByRefire` stays roster-only).
REVIEWER_UNREADY_ROLES = ("reviewer", "readiness-host")
REVIEWER_UNREADY_KINDS = ("missing-tldr", "null-return", "errored")
REVIEW_ONLY_CALL = (
    "emit-dispatch-workflow --inventory <inv> --review-only --rows <DONE ids> --run-base <sha>"
)
OP_ENGINE_ASK = "engine-ask"
OP_WORKFLOW_RESUME = "workflow-resume"
OPS = (OP_ENGINE_ASK, OP_WORKFLOW_RESUME)

# `stage` is the resume handle from halt-keeps-work (hnd for 05b3e6-1-halt-keeps-work). This
# module does not know its shape: it is copied as-is from the `stage` argument and never read.
RESUME_KEYS = ("run_id", "sizing", "halted", "stage")
# Optional beside RESUME_KEYS: halts recorded before it existed lack it, and a halt kind outside
# HALT_PHASE omits it (a full pass).
RESUME_PHASE_KEY = "phase"

# A halt whose fix lies outside the plan's frozen footprint: a refire replays the same plan and halts again, so
# the envelope names the replan (resume phase `plan`) and carries the scope the replan must mint rows for.
REPLAN_KIND = "replan"
HALT_PREMISE_WIDENED = "premise_widened"
HALT_OUTSIDE_FOOTPRINT = "outside_footprint"
REPLAN_HALTS = (HALT_PREMISE_WIDENED, HALT_OUTSIDE_FOOTPRINT)
# ask_stage's `PREMISE-WIDENED:` refusal prefix, spelled out because this leaf must not import ask_stage.
PREMISE_WIDENED_MARKER = "PREMISE-WIDENED:"
OUTSIDE_FOOTPRINT_BLOCKER = r"gate-blocker:\s*outside-footprint"
# `self_correction.status` of a halt-fix loop that needed a path outside the run footprint.
SELF_CORRECTION_SCOPE_STATUS = "scope"
SELF_CORRECTION_KEY = "self_correction"
HALT_TEXT_KEYS = ("reason", "error", "detail")

INPUT_KINDS = ("rule", "repin", "touchpoint", "em-discharge", "wait", REPLAN_KIND)
LEGACY_INPUT_KIND = {
    "rule_and_refire": "rule",
    "rule_on_blocking_rows": "rule",
    "repin_register": "repin",
}

# ask_compose's HALT_USAGE_LIMIT, spelled out because this module must not import ask_compose.
HALT_USAGE_LIMIT = "usage_limit"
INPUT_BY_HALT = {HALT_DESIGN_REVIEW: "touchpoint", HALT_USAGE_LIMIT: "wait"}

ENVELOPE_EXEMPT_KINDS = ("parked", "no-op")
FANNED_KINDS = ("split", "research")

HAND_REPAIR_MARKERS = (
    "fix the",
    "hand-finish",
    "hand-repair",
    "edit the",
    "apply the unapplied",
)

ALL_OWED_PREFIX = "Everything left is EM or operator work:"
# A PARTIAL holder whose needed row waits on the holder's own dependents: only an EM discharge ends the hold.
NEEDS_DOWNSTREAM_PREFIX = "Held rows need a row downstream of them:"

EM_RULE_KEY = "em_rule"
# Per-run record file mapping each withheld path to the blob of HEAD plus only the run's own LF change.
WITHHELD_OWN_DELTA_FILE = "withheld-own-delta.json"
# The one rule for when the EM may act after a halt; the halt next_action emits it verbatim (same text, no paraphrase).
EM_ACTION_AFTER_HALT_RULE = (
    "The engine is an affordance, not a mandate: it earns use by being the pleasanter path, and hand work stays "
    "legitimate. After a halt, a refire or workflow.revise resumes the run with the work already done kept. "
    "Landing this run's withheld paths by hand commit is also fine: a halted run holds nothing. "
    f"Land a withheld path's `blob` from the run record's {WITHHELD_OWN_DELTA_FILE} (HEAD plus only this run's "
    "LF change); the raw worktree or kept.json blob can carry a peer's hunks and CRLF."
)


def refire_next_action(
    *,
    call: str,
    run_id: str,
    sizing: str,
    halted: str,
    op: str = OP_ENGINE_ASK,
    input: dict | None = None,
    detail: str | None = None,
    stage=None,
    host: str | None = None,
) -> dict:
    """The envelope `{kind, op, call, resume, input, detail}`; `stage` is copied unchanged.

    `resume.phase` is HALT_PHASE[halted] and absent for a kind that map does not name. `host` defaults to
    `detect_host()`; an engine-ask `call` is rendered host-native by `host_native_call`. A `halted` in
    REPLAN_HALTS makes the envelope `kind` REPLAN_KIND; every other field keeps the refire shape.
    """
    call = host_native_call(call, detect_host() if host is None else host) if op == OP_ENGINE_ASK else call
    resume = {"run_id": run_id, "sizing": sizing, "halted": halted, "stage": stage}
    phase = HALT_PHASE.get(halted)
    if phase is not None:
        resume[RESUME_PHASE_KEY] = phase
    return {
        "kind": REPLAN_KIND if halted in REPLAN_HALTS else REFIRE_KIND,
        "op": op,
        "call": call,
        "resume": resume,
        "input": input,
        "detail": detail,
        EM_RULE_KEY: EM_ACTION_AFTER_HALT_RULE,
        APPLIED_KEY: refire_applied(run_id, halted, phase),
        REFUSED_KEY: [],
    }


def refire_applied(run_id: str, halted: str, phase: str | None) -> list[str]:
    """What the refire re-arms: the halted run, its halt kind and the phase it resumes at."""
    where = f" at phase {phase}" if phase is not None else " as a full pass"
    verb = "replan" if halted in REPLAN_HALTS else "re-arm"
    return [f"{verb} {halted} halt of run {run_id}{where}"]


def replan_halt_kind(halted: object) -> str | None:
    """HALT_PREMISE_WIDENED or HALT_OUTSIDE_FOOTPRINT when `halted` (a halt record) is replan-class, else None.

    Replan-class: a `PREMISE-WIDENED:` stage refusal in `reason`/`error`/`detail`, a `self_correction` that
    ended `scope`, or a `gate-blocker: outside-footprint` line in those fields. Twin of `_replanKind` in the
    emitted finaliser.
    """
    if not isinstance(halted, dict):
        return None
    texts = [halted[k] for k in HALT_TEXT_KEYS if isinstance(halted.get(k), str)]
    if any(PREMISE_WIDENED_MARKER in t for t in texts):
        return HALT_PREMISE_WIDENED
    sc = halted.get(SELF_CORRECTION_KEY)
    if isinstance(sc, dict) and sc.get("status") == SELF_CORRECTION_SCOPE_STATUS:
        return HALT_OUTSIDE_FOOTPRINT
    if any(re.search(OUTSIDE_FOOTPRINT_BLOCKER, t) for t in texts):
        return HALT_OUTSIDE_FOOTPRINT
    return None


def replan_input(halted: object) -> dict:
    """The `input` of a replan envelope: `added_scope` (the widened-premise reason text) and `needs_paths`.

    `needs_paths` is the halt's self-correction `needs_paths`, strings only; the replan mints rows for them.
    """
    rec = halted if isinstance(halted, dict) else {}
    sc = rec.get(SELF_CORRECTION_KEY)
    paths = sc.get("needs_paths") if isinstance(sc, dict) else None
    scope = []
    for key in HALT_TEXT_KEYS:
        text = rec.get(key)
        if isinstance(text, str) and PREMISE_WIDENED_MARKER in text:
            scope.append(text.split(PREMISE_WIDENED_MARKER, 1)[1].strip())
            break
    return {
        "kind": REPLAN_KIND,
        "added_scope": scope,
        "needs_paths": [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else [],
    }


def refire_reply(envelope: dict) -> MutationReply:
    """The MutationReply for a refire envelope: its `applied`/`refused` lists, `target` the halted run."""
    resume = envelope.get("resume") if isinstance(envelope, dict) else None
    target = resume.get("run_id") if isinstance(resume, dict) else None
    return MutationReply(
        applied=tuple(envelope.get(APPLIED_KEY) or ()) if isinstance(envelope, dict) else (),
        refused=tuple(envelope.get(REFUSED_KEY) or ()) if isinstance(envelope, dict) else (),
        target=target,
    )


WAVE_COMMIT_FAILED_HEAD = "wave commit failed, dependents held ("
_WAVE_COMMIT_FAILED_TAIL = "): "
_WAVE_REFUSAL_END = " [an unedited Workflow resume"
_WAVE_SEAM_NOTE = "; seam drift also recorded"
_WAVE_SPLIT = re.compile(r"; (?=wave \d+: )")
_WAVE_SEGMENT = re.compile(r"wave (\d+): .*", re.S)


def commit_retry_from_reason(reason: object) -> dict:
    """``{wave: verbatim refusal}`` parsed from a halt marker's ``reason`` that records failed wave commits.

    The text is the one the emitted finaliser writes (``wave commit failed, dependents held (...): wave N: <why>; ...``);
    any other reason, or one with no ``wave N:`` segment, yields ``{}``.
    """
    if not isinstance(reason, str) or not reason.startswith(WAVE_COMMIT_FAILED_HEAD):
        return {}
    _, sep, body = reason.partition(_WAVE_COMMIT_FAILED_TAIL)
    if not sep:
        return {}
    body = body.split(_WAVE_REFUSAL_END, 1)[0].split(_WAVE_SEAM_NOTE, 1)[0]
    out: dict = {}
    for segment in _WAVE_SPLIT.split(body):
        match = _WAVE_SEGMENT.fullmatch(segment.strip())
        if match:
            out[int(match.group(1))] = match.group(0)
    return out


def legacy_input(legacy: dict) -> dict:
    """Nest a legacy judgment next_action under `input`, fields intact, legacy kind kept."""
    out = dict(legacy)
    legacy_kind = legacy.get("kind")
    out["kind"] = LEGACY_INPUT_KIND.get(legacy_kind, legacy_kind)
    out["legacy_kind"] = legacy_kind
    return out


def _carries_hand_repair(text: object) -> bool:
    if not isinstance(text, str):
        return False
    low = text.lower()
    return any(marker in low for marker in HAND_REPAIR_MARKERS)


INVOKE_SUBMIT = "coordinator-invoke engine_ask.submit"
RESUME_RUN_ID_ARG = "resume_run_id"
# `Workflow(...)` has no counterpart on a host without the Workflow tool; the engine-ask refire there is the CLI submit.
_ENGINE_ASK_WORKFLOW_CALL = re.compile(r'Workflow\(\{name: "coordinator:engine-ask", args: (\{.*\})\}\)', re.S)


def detect_host(env: dict | None = None) -> str | None:
    """The submitting host's vendor when it has no `Workflow` tool (codex, agy, grok), else None.

    Reads the launcher identity the codex_runner contract carries (`identity_env.IDENTITY_ENV`), through
    `agent_provenance`; a Claude session or an unrecognised env is None.
    """
    import os

    from coordinator_core.git.agent_provenance import detect_agent_provenance
    from coordinator_core.warp_fire import NO_WORKFLOW_HOSTS

    prov = detect_agent_provenance(os.environ if env is None else env)
    return prov.vendor if prov is not None and prov.vendor in NO_WORKFLOW_HOSTS else None


def host_native_call(call: str, host: str | None) -> str:
    """`call` as the host runs it: an engine-ask `Workflow(...)` call becomes the CLI submit on a Workflow-less host.

    Any other call, or a Claude host, comes back unchanged. `resume_run_id` is not an `engine_ask.submit` param
    and is dropped; the halted run resumes through `workflow.revise`.
    """
    from coordinator_core.warp_fire import NO_WORKFLOW_HOSTS

    match = _ENGINE_ASK_WORKFLOW_CALL.fullmatch(call) if host in NO_WORKFLOW_HOSTS else None
    if match is None:
        return call
    try:
        args = json.loads(match.group(1))
    except ValueError:
        return call
    if not isinstance(args, dict):
        return call
    args.pop(RESUME_RUN_ID_ARG, None)
    return f"{INVOKE_SUBMIT} '{json.dumps(args, separators=(',', ':'))}'"


def _call_is_one_engine_call(op: str, call: str, host: str | None = None) -> bool:
    if op == OP_ENGINE_ASK:
        if call.count(INVOKE_SUBMIT) == 1 and "Workflow" not in call:
            return True
        return host is None and call.count("Workflow(") == 1
    return "resumeFromRunId" in call and call.count("Workflow") <= 1


def is_refire_next_action(obj: object, host: str | None = None) -> bool:
    """True only for an envelope naming one engine call and no hand-repair step; a replan envelope qualifies.

    `host` names a Workflow-less submitter (see `detect_host`): its engine-ask call must be the CLI submit.
    `input` is not scanned: it carries reviewer and ruling text verbatim.
    """
    if not isinstance(obj, dict) or obj.get("kind") not in (REFIRE_KIND, REPLAN_KIND):
        return False
    op = obj.get("op")
    if op not in OPS:
        return False
    call = obj.get("call")
    if not isinstance(call, str) or not call.strip() or not _call_is_one_engine_call(op, call, host):
        return False
    resume = obj.get("resume")
    if not isinstance(resume, dict) or set(resume) - {RESUME_PHASE_KEY} != set(RESUME_KEYS):
        return False
    if RESUME_PHASE_KEY in resume and resume[RESUME_PHASE_KEY] not in PHASES:
        return False
    if not isinstance(resume.get("run_id"), str) or not resume["run_id"]:
        return False
    inp = obj.get("input")
    if inp is not None and (not isinstance(inp, dict) or inp.get("kind") not in INPUT_KINDS):
        return False
    return not (_carries_hand_repair(call) or _carries_hand_repair(obj.get("detail")))


def exit_next_action(*, reason: str, route: str, call: str) -> dict:
    """The exit object `{kind, reason, route, call}` a halt records instead of a same-sizing refire."""
    return {"kind": EXIT_KIND, "reason": reason, "route": route, "call": call}


def is_exit_next_action(obj: object) -> bool:
    """True only for an exit object whose `call` is not an engine-ask/Workflow refire."""
    if not isinstance(obj, dict) or obj.get("kind") != EXIT_KIND or set(obj) != {"kind", "reason", "route", "call"}:
        return False
    call, reason = obj.get("call"), obj.get("reason")
    return (
        obj.get("route") in EXIT_ROUTES
        and isinstance(reason, str)
        and bool(reason.strip())
        and isinstance(call, str)
        and bool(call.strip())
        and "Workflow" not in call
    )


def reviewer_unready_js(blitz_js: str, retry_re_js: str) -> str:
    """JS boolean expression: true when `[blitz, blitz.regate]` shows a reviewer that returned no usable verdict.

    That is an `agentIncidents` entry whose role (text before any ':') is in REVIEWER_UNREADY_ROLES and kind in
    REVIEWER_UNREADY_KINDS, or a `pulled` entry whose `String(reason) + String(prepGateReport)` matches
    `retry_re_js` (a JS regex literal). `blitz_js` is a JS expression; pure, no I/O.
    """
    j = json.dumps
    return (
        f"([{blitz_js}, ({blitz_js}) && ({blitz_js}).regate].some((_b) => _b && ("
        f"(Array.isArray(_b.agentIncidents) && _b.agentIncidents.some((_i) => _i && "
        f"{j(list(REVIEWER_UNREADY_ROLES))}.includes(String(_i.role).split(':')[0]) && "
        f"{j(list(REVIEWER_UNREADY_KINDS))}.includes(_i.kind))) || "
        f"(Array.isArray(_b.pulled) && _b.pulled.some((_p) => _p && {retry_re_js}.test(String(_p.reason) + String(_p.prepGateReport)))))))"
    )


def _exit_route_js(run_id_js: str) -> str:
    """JS `_exitRoute(refused)` returning the exit object for a NON_REFIRE refusal."""
    j = json.dumps
    return "\n".join(
        [
            "  const _exitRoute = (_ref) => {",
            "    const _list = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);",
            f"    const _meant = _list(_halted[{j(MEANT_TO_LAND_KEY)}]);",
            f"    const _dirty = _list(_halted[{j(DIRTY_BEFORE_RUN_KEY)}]);",
            f"    if (_ref === {j(NO_CHANGE_REFUSAL)} && _dirty.length) {{",
            f"      return {{ kind: {j(EXIT_KIND)}, route: {j(ROUTE_REVIEW_ONLY)}, call: {j(REVIEW_ONLY_CALL)},",
            "        reason: 'The executor wrote nothing; these paths were already dirty before the run: ' + _dirty.join(', ') + '. A refire reproduces the refusal: land exactly them review-only.' };",
            "    }",
            f"    if (_ref === {j(NO_CHANGE_REFUSAL)}) {{",
            f"      return {{ kind: {j(EXIT_KIND)}, route: {j(ROUTE_REVISE)},",
            f"        call: 'workflow.revise {{run_id: ' + {run_id_js} + ', note: <name the undelivered rows>}}',",
            "        reason: 'The executor wrote nothing for ' + (_meant.length ? _meant.join(', ') : 'its declared paths') + '. A refire reproduces the refusal: revise the run to name the undelivered rows.' };",
            "    }",
            f"    return {{ kind: {j(EXIT_KIND)}, route: {j(ROUTE_REVIEW_ONLY)}, call: {j(REVIEW_ONLY_CALL)},",
            "      reason: 'The run produced no review, so its commit refuses as unreviewed. A refire reproduces the refusal: re-emit it review-only.' };",
            "  };",
            "  const _causeUnchangedByRefire = () => [_halted.blitz, _halted.blitz && _halted.blitz.regate].some((_b) =>",
            f"    _b && Array.isArray(_b.agentIncidents) && _b.agentIncidents.some((_i) => _i && _i.kind === {j(ROSTER_UNREADABLE_KIND)}));",
            "  const _emptyPrepLandedExit = () => ({",
            f"    kind: {j(EXIT_KIND)}, route: {j(ROUTE_LANDED)},",
            f"    call: 'dispatch.record_halted_run_landed {{run_id: ' + {run_id_js} + ', sha: <landing sha>}}',",
            "    reason: 'Review prep froze an empty diff with no error: the product is already at HEAD. A refire reproduces the halt; record the run landed.' });",
            "  const _rosterExit = () => ({",
            f"    kind: {j(EXIT_KIND)}, route: {j(ROUTE_REVISE)},",
            f"    call: 'workflow.revise {{run_id: ' + {run_id_js} + ', note: <name the plan_review roster fix>}}',",
            "    reason: 'plan-blitz read no reviewer roster although this script passed its plan_review block. A refire reproduces the refusal: revise the run to repair the roster fragment.' });",
        ]
    )


# A `{sizing}` refire call of a run that carries a footprint names it as `writes`: the XS arm refuses a
# sizing without one. `_writes` is the script's footprint; a script without it leaves the call as built.
_WITH_FOOTPRINT_JS = "\n".join(
    [
        "function _withFootprint(_call) {",
        "  if (typeof _call !== 'string' || typeof _writes === 'undefined' || !Array.isArray(_writes) || !_writes.length) return _call;",
        "  if (_call.includes('\"writes\"')) return _call;",
        "  return _call.replace(/(args: \\{\"sizing\": \"[^\"]*\")\\}\\}\\)$/, (_m, _head) => _head + ', \"writes\": ' + JSON.stringify(_writes) + '}})');",
        "}",
    ]
)


def _replan_js() -> str:
    """JS twins of `replan_halt_kind` and `replan_input`: `_replanKind(_h)` and `_replanInput(_h)`."""
    j = json.dumps
    return "\n".join(
        [
            "function _replanKind(_h) {",
            "  if (!_h || typeof _h !== 'object') return null;",
            f"  const _t = {j(list(HALT_TEXT_KEYS))}.map((_k) => _h[_k]).filter((_v) => typeof _v === 'string');",
            f"  if (_t.some((_v) => _v.includes({j(PREMISE_WIDENED_MARKER)}))) return {j(HALT_PREMISE_WIDENED)};",
            f"  const _sc = _h[{j(SELF_CORRECTION_KEY)}];",
            f"  if (_sc && typeof _sc === 'object' && _sc.status === {j(SELF_CORRECTION_SCOPE_STATUS)}) return {j(HALT_OUTSIDE_FOOTPRINT)};",
            f"  if (_t.some((_v) => new RegExp({j(OUTSIDE_FOOTPRINT_BLOCKER)}).test(_v))) return {j(HALT_OUTSIDE_FOOTPRINT)};",
            "  return null;",
            "}",
            "function _replanInput(_h) {",
            f"  const _sc = _h[{j(SELF_CORRECTION_KEY)}];",
            "  const _scope = [];",
            f"  for (const _k of {j(list(HALT_TEXT_KEYS))}) {{",
            f"    if (typeof _h[_k] === 'string' && _h[_k].includes({j(PREMISE_WIDENED_MARKER)})) {{ _scope.push(_h[_k].split({j(PREMISE_WIDENED_MARKER)}).slice(1).join({j(PREMISE_WIDENED_MARKER)}).trim()); break; }}",
            "  }",
            f"  return {{ kind: {j(REPLAN_KIND)}, added_scope: _scope,",
            "    needs_paths: (_sc && Array.isArray(_sc.needs_paths) ? _sc.needs_paths : []).filter((_p) => typeof _p === 'string') };",
            "}",
        ]
    )


# JS twin of `host_native_call`, emitted only for a Workflow-less submitter so a Claude script is unchanged.
_HOST_NATIVE_JS = "\n".join(
    [
        "function _hostNativeCall(_call) {",
        "  const _m = typeof _call === 'string' && /^Workflow\\(\\{name: \"coordinator:engine-ask\", args: (\\{[\\s\\S]*\\})\\}\\)$/.exec(_call);",
        "  if (!_m) return _call;",
        "  let _a;",
        "  try { _a = JSON.parse(_m[1]); } catch (_e) { return _call; }",
        "  if (!_a || typeof _a !== 'object' || Array.isArray(_a)) return _call;",
        f"  delete _a[{json.dumps(RESUME_RUN_ID_ARG)}];",
        f"  return {json.dumps(INVOKE_SUBMIT + ' ')} + \"'\" + JSON.stringify(_a) + \"'\";",
        "}",
    ]
)


def refire_next_action_js(
    call_js: str,
    legacy_js: str,
    kind_js: str,
    detail_js: str,
    stage_js: str = "null",
    run_id_js: str = "_runId",
    sizing_js: str = "_sizingRel",
    host: str | None = None,
) -> str:
    """JS source of `function _refireNextAction()`, which finalises `_halted.next_action` in place.

    Every argument is a JS expression: `call_js` the one engine call string, `legacy_js` the
    halt's current next_action, `kind_js` its halted kind, `detail_js` the default detail,
    `stage_js` the resume handle rendered into `resume.stage`. Idempotent; leaves exempt and
    already-fanned halts alone. `host` defaults to `detect_host()` at emit time: a Workflow-less
    submitter gets the engine-ask call as the CLI submit (`_hostNativeCall`).
    """
    j = json.dumps
    native = (detect_host() if host is None else host) is not None
    return "\n".join(
        [
            _WITH_FOOTPRINT_JS,
            _replan_js(),
            *([_HOST_NATIVE_JS] if native else []),
            "function _refireNextAction() {",
            "  if (!_halted || typeof _halted !== 'object') return;",
            _exit_route_js(run_id_js),
            f"  if ({j(list(NON_REFIRE_REFUSALS))}.includes(_halted.refused)) {{",
            f"    if (!(_halted.next_action && _halted.next_action.kind === {j(EXIT_KIND)})) _halted['next_action'] = _exitRoute(_halted.refused);",
            "    return;",
            "  }",
            "  if (_causeUnchangedByRefire()) {",
            f"    if (!(_halted.next_action && _halted.next_action.kind === {j(EXIT_KIND)})) _halted['next_action'] = _rosterExit();",
            "    return;",
            "  }",
            f"  if (String(_halted.halted ?? '').startsWith({j(NO_SLICES_HALT_PREFIX)}) && _halted.prep && !_halted.prep.error && "
            "(_halted.prep.product_files ?? 0) === 0 && "
            "!(Array.isArray(_halted.prep.slices) ? _halted.prep.slices : []).some((_s) => (_s?.files ?? []).length)) {",
            f"    if (!(_halted.next_action && _halted.next_action.kind === {j(EXIT_KIND)})) _halted['next_action'] = _emptyPrepLandedExit();",
            "    return;",
            "  }",
            f"  let _hk = {kind_js};",
            f"  if ({j(list(ENVELOPE_EXEMPT_KINDS))}.includes(_hk)) return;",
            f"  if ({j(list(FANNED_KINDS))}.includes(_hk) && Array.isArray(_halted.fire) && _halted.fire.length) return;",
            f"  const _legacy = {legacy_js};",
            f"  if (_legacy && typeof _legacy === 'object' && _legacy.kind === {j(REFIRE_KIND)}) return;",
            f"  if (_legacy && typeof _legacy === 'object' && _legacy.kind === {j(REPLAN_KIND)}) return;",
            "  const _rk = typeof _hk === 'string' && _hk !== " + j(HALT_USAGE_LIMIT) + " ? _replanKind(_halted) : null;",
            f"  const _legacyKind = {j(LEGACY_INPUT_KIND)};",
            f"  const _byHalt = {j(INPUT_BY_HALT)};",
            f"  const _haltPhase = {j(HALT_PHASE)};",
            "  let _input = null;",
            "  let _detail = null;",
            "  if (typeof _legacy === 'string') {",
            "    _detail = _legacy;",
            f"    if ({j([ALL_OWED_PREFIX, NEEDS_DOWNSTREAM_PREFIX])}.some((_p) => _legacy.startsWith(_p))) _input = {{ kind: 'em-discharge' }};",
            "  } else if (_legacy && typeof _legacy === 'object') {",
            "    _input = Object.assign({}, _legacy, { kind: _legacyKind[_legacy.kind] || _legacy.kind, legacy_kind: _legacy.kind });",
            f"    _detail = {detail_js};",
            "  }",
            "  if (!_input && _byHalt[_hk]) _input = { kind: _byHalt[_hk] };",
            "  if (_rk) { _input = Object.assign({}, _input && typeof _input === 'object' ? _input : {}, _replanInput(_halted)); _hk = _rk; }",
            "  _halted.next_action = {",
            f"    kind: _rk ? {j(REPLAN_KIND)} : {j(REFIRE_KIND)},",
            f"    op: _hk === {j(HALT_USAGE_LIMIT)} ? {j(OP_WORKFLOW_RESUME)} : {j(OP_ENGINE_ASK)},",
            f"    call: {'_hostNativeCall(' if native else ''}_withFootprint({call_js}){')' if native else ''},",
            f"    resume: Object.assign({{ run_id: {run_id_js}, sizing: {sizing_js}, halted: _hk, stage: {stage_js} }}, Object.prototype.hasOwnProperty.call(_haltPhase, _hk) ? {{ {j(RESUME_PHASE_KEY)}: _haltPhase[_hk] }} : {{}}),",
            "    input: _input,",
            "    detail: _detail === undefined ? null : _detail,",
            f"    {EM_RULE_KEY}: {j(EM_ACTION_AFTER_HALT_RULE)},",
            f"    {APPLIED_KEY}: [(_rk ? 'replan ' : 're-arm ') + _hk + ' halt of run ' + {run_id_js} + (Object.prototype.hasOwnProperty.call(_haltPhase, _hk) ? ' at phase ' + _haltPhase[_hk] : ' as a full pass')],",
            f"    {REFUSED_KEY}: [],",
            "  };",
            "}",
        ]
    )
