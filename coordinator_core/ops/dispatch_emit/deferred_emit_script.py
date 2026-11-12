"""The deferred-emit workflow script: renders a run script that emits one `dispatch.emit` params set through an
op-relay agent, then follows the launcher chain hand-off and answers every `stage_pending` with the staged twin.

Shared by the submit hook (a premint deferral, an emit that outran its budget, a fan child) and the EM-facing re-emit op."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

# The emit-relay constants and helper shared by the deferred and direct-start scripts, which both relay a chain emit.
# Shared by the deferred-emit stage loop and the fan launcher: one copy, so the twin check cannot drift.
TWIN_CHECK_JS = r"""// A refire recomposes `warp-<sizing stem>-<stamp>-<hex>` under a fresh stamp, so the twin the halted run staged names the same run under another stamp: the family is the id without its stamp.
function _runFamily(id) {
  const m = /^(warp-.+)-[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$/.exec(id);
  return m ? m[1] : id;
}
// A stage reply that names its staged twin wins over a path derived from the handle sha, which a restage leaves stale. null: none named; false: named but not this run's twin.
function _replyTwinRel(pending, stem) {
  const t = pending.twin_path;
  if (t === undefined || t === null) return null;
  if (typeof t !== 'string' || !t.startsWith(TWIN_PREFIX) || !t.endsWith(TWIN_SUFFIX)) return false;
  const shaAt = t.length - TWIN_SUFFIX.length - 64;
  const midAt = shaAt - TWIN_MIDDLE.length;
  if (midAt <= TWIN_PREFIX.length || t.slice(midAt, shaAt) !== TWIN_MIDDLE || !/^[0-9a-f]{64}$/.test(t.slice(shaAt, shaAt + 64))) return false;
  const own = t.slice(TWIN_PREFIX.length, midAt);
  return /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(own) && _runFamily(own) === _runFamily(stem) ? t : false;
}
"""


_EMIT_RELAY_JS = r"""const INVOKE = __INVOKE__;
const EMIT_OP = __EMIT_OP__;
const EMIT_SCHEMA = {
  type: 'object',
  required: ['path', 'run_id', 'fire_args'],
  properties: { path: { type: 'string' }, run_id: { type: 'string' }, fire_args: { type: 'object' }, error: { type: 'string' }, status: { type: 'string' }, retry_after: { type: 'number' } },
};
// Trap: the relay can answer a quoted-empty path (`""` or `''`), which is not the empty string.
function _blankPath(p) {
  return typeof p !== 'string' || p.replace(/["'\s]/g, '') === '';
}
"""


_DEFERRED_JS = r"""export const meta = { name: 'engine-ask-deferred-emit', description: __DESCRIPTION__ }
const REPO_ROOT = __REPO_ROOT__;
const PARAMS_FILE = __PARAMS_FILE__;
""" + _EMIT_RELAY_JS + r"""// Trap: ask text carries quotes and `$(`; the command names a params file and never inlines the payload. Trap: a subagent relay is denied recursive or forced removal, so the prompt carries no removal and the file is left in scratch/warp.
const EMIT_PROMPT = __AUTHORITY__
  + ' Run `' + INVOKE + ' ' + EMIT_OP + ' --params-file "' + PARAMS_FILE + '"` once, with the command word exactly as written. '
  + 'Do not delete or modify any file. Then '
  + 'return path, run_id and fire_args from its JSON reply verbatim; when the reply carries status or retry_after, return those verbatim too. When the command is denied or fails, '
  + "return the command's error text verbatim in the error field, with path as an empty string and fire_args as {}; on success omit error. "
  + 'If you do not run the command for any reason, put that reason verbatim in the error field the same way.';
// Trap: handover_pending is a reply with no path and no error, taken only across stop-and-copy; wait it out, bounded.
const HANDOVER_MAX_ATTEMPTS = 3;
const HANDOVER_WAIT_FALLBACK_S = 5;
const HANDOVER_WAIT_CAP_S = 30;
function _isHandover(r) {
  return !!r && _blankPath(r.path) && (r.status === 'handover_pending' || (typeof r.error === 'string' && r.error.indexOf('handover_pending') !== -1));
}
let reply;
for (let attempt = 1; attempt <= HANDOVER_MAX_ATTEMPTS; attempt++) {
  // Trap: a headless runner supplies engineOp and calls the op in-process; the agent relay is the harness path.
  reply = typeof engineOp === 'function'
    ? await engineOp(EMIT_OP, { paramsFile: PARAMS_FILE })
    : await agent(
      EMIT_PROMPT,
      { label: 'deferred-emit', phase: 'emit', agentType: 'coordinator:op-relay', model: 'haiku', effort: 'low', schema: EMIT_SCHEMA },
    );
  if (!_isHandover(reply) || attempt === HANDOVER_MAX_ATTEMPTS) break;
  const waitS = (typeof reply.retry_after === 'number' && reply.retry_after > 0) ? Math.min(reply.retry_after, HANDOVER_WAIT_CAP_S) : HANDOVER_WAIT_FALLBACK_S;
  await new Promise((res) => setTimeout(res, waitS * 1000));
}
if (!reply || _blankPath(reply.path)) {
  return { status: 'emit_refused', error: (reply && reply.error) || (_isHandover(reply) ? 'handover_pending persisted across ' + HANDOVER_MAX_ATTEMPTS + ' emit attempts' : 'the deferred emit returned no script path') };
}
// Trap: the relay re-serializes the reply path, so a Windows path can arrive with doubled backslashes or repo-relative.
const norm = reply.path.replace(/\\/g, '/').replace(/\/{2,}/g, '/');
const scriptPath = (norm[0] === '/' || /^[A-Za-z]:\//.test(norm)) ? norm : REPO_ROOT + '/' + norm.replace(/^\.\//, '');
__START__
"""


# The start block of a deferred script run at depth 1: the ask is emitted as a launcher child, so an M+ ask hands its
# chain back (CHAIN_HANDOFF_KEY) instead of calling workflow() from depth 2, and this script starts the chained run.
# Trap: a launcher-child script holds no workflow() reader, so a stage_pending return is answered with the staged twin.
_STAGE_LOOP_JS = r"""const MAX_STAGE_ROUNDS = __MAX_ROUNDS__;
const TWIN_PREFIX = __TWIN_PREFIX__;
const TWIN_MIDDLE = __TWIN_MIDDLE__;
const TWIN_SUFFIX = __TWIN_SUFFIX__;
const RUN_SCRIPT_SUFFIX = __RUN_SCRIPT_SUFFIX__;
const START_ARGS = __START_ARGS__;
function _normPath(p) {
  const s = p.replace(/\\/g, '/').replace(/\/{2,}/g, '/');
  return (s[0] === '/' || /^[A-Za-z]:\//.test(s)) ? s : REPO_ROOT + '/' + s.replace(/^\.\//, '');
}
""" + TWIN_CHECK_JS + r"""async function _startLoop(firstPath) {
  let path = firstPath;
  let lastSha = null;
  if (typeof firstPath !== 'string' || !firstPath.endsWith('.mjs')) return { ok: false, error: 'refusing to start a non-script path: ' + String(firstPath) };
  for (let round = 0; round <= MAX_STAGE_ROUNDS; round++) {
    const result = START_ARGS === null ? await workflow({ scriptPath: path }) : await workflow({ scriptPath: path, args: START_ARGS });
    const pending = (result !== null && typeof result === 'object') ? result.__PENDING_KEY__ : null;
    if (!pending) return { ok: true, result: result };
    if (round >= MAX_STAGE_ROUNDS) return { ok: false, error: 'stage_pending after ' + MAX_STAGE_ROUNDS + ' stage rounds' };
    const base = firstPath.split('/').pop();
    if (!base.endsWith(RUN_SCRIPT_SUFFIX) || base.length === RUN_SCRIPT_SUFFIX.length || base.indexOf('.staged-') !== -1) {
      return { ok: false, error: 'stage resume path is not a run script: ' + firstPath };
    }
    const stem = base.slice(0, base.length - RUN_SCRIPT_SUFFIX.length);
    const named = _replyTwinRel(pending, stem);
    if (named === false) return { ok: false, error: 'stage_pending twin_path is not a staged twin of this run: ' + String(pending.twin_path) };
    let twinRel = named;
    if (named === null) {
      const sha = pending.handle && typeof pending.handle === 'object' ? pending.handle.sha256 : undefined;
      if (typeof sha !== 'string' || !/^[0-9a-f]{64}$/.test(sha)) return { ok: false, error: 'stage_pending handle sha256 is not 64 lowercase hex' };
      twinRel = TWIN_PREFIX + stem + TWIN_MIDDLE + sha + TWIN_SUFFIX;
    }
    if (lastSha === twinRel) return { ok: false, error: 'stage_pending repeated for a handle already supplied' };
    lastSha = twinRel;
    path = REPO_ROOT + '/' + twinRel;
  }
  return { ok: false, error: 'stage rounds exhausted' };
}
"""

# The chain hop both start scripts share; it needs ASK_RUN_ID, ASK_SCRIPT_PATH and `first` (the ask run's `_startLoop` outcome).
_CHAIN_FOLLOW_JS = r"""// Trap: a relaying agent mis-copies run_id (the ask's own id); the script path's basename is the run id, so never read the relayed one.
function _pathRunId(p) {
  const b = p.replace(/\\/g, '/').split('/').pop();
  return (b.endsWith(RUN_SCRIPT_SUFFIX) && b.indexOf('.staged-') === -1) ? b.slice(0, b.length - RUN_SCRIPT_SUFFIX.length) : '';
}
function _isScriptPath(p) {
  return typeof p === 'string' && p.trim().replace(/["']+$/, '').endsWith('.mjs');
}
if (!first.ok) return { status: 'failed', run_id: ASK_RUN_ID, error: first.error };
const handoff = (first.result !== null && typeof first.result === 'object') ? first.result.__CHAIN_KEY__ : null;
if (handoff === null || typeof handoff !== 'object') return first.result;
// Trap: one hop only. A gate agent that already ran dispatch.emit leaves its reply keys in the hand-off; those are never emit params.
const { path: _rp, run_id: _rr, ok: _rk, error: _re, fire_args: _rf, ...chainParams } = handoff;
const _fail = (why) => ({ status: 'failed', run_id: ASK_RUN_ID, error: 'the chained run did not complete (' + why + '); the ask run ' + ASK_RUN_ID + ' stays resumable' });
// Trap: the payload rides a quoted heredoc on stdin; JSON.stringify escapes every newline, so the body is one line and cannot equal the tag.
const _chainTag = 'CHAIN_PARAMS_JSON';
// Trap: the hand-off the ask script returns names the sizing and no EM brief, so the chained run would compose without it; the deferred params' brief keys ride the hop.
const _briefCarry = __BRIEF_CARRY__;
const _chainJson = JSON.stringify({ ...chainParams, ..._briefCarry, __LAUNCHER_PARAM__: true });
let chain;
if (typeof _re === 'string' && _re !== '') return _fail('emit failed: ' + _re);
// Trap: a hand-off path that is not a `.mjs` script (the L-sized ask's handoff .md) is never started; the hop emits its own chained run.
if (!_blankPath(_rp) && _isScriptPath(_rp)) {
  chain = { path: _rp, run_id: _rr, fire_args: (_rf !== null && typeof _rf === 'object') ? _rf : {} };
} else {
  chain = await agent(
    __AUTHORITY__
    + ' Run this command once, with the command word exactly as written, and copy the heredoc body byte for byte:\n'
    + INVOKE + ' ' + EMIT_OP + ' --params-file - <<\'' + _chainTag + '\'\n' + _chainJson + '\n' + _chainTag + '\n\n'
    + 'Then '
    + 'return path, run_id and fire_args from its JSON reply verbatim. When the command is denied or fails, '
    + "return the command's error text verbatim in the error field, with path as an empty string and fire_args as {}; on success omit error. "
    + 'If you do not run the command for any reason, put that reason verbatim in the error field the same way.',
    { label: 'chain-emit', phase: 'emit', agentType: 'coordinator:op-relay', model: 'haiku', effort: 'low', schema: EMIT_SCHEMA },
  );
}
if (!chain || _blankPath(chain.path)) return _fail((chain && chain.error) || 'the chain emit returned no script path');
if (!_isScriptPath(chain.path)) return _fail('the chain emit returned a non-script path ' + JSON.stringify(chain.path));
// Trap: the path basename is the trusted run id; the emit's minted run_id stands in only when the basename yields none (a path the
// suffix/staged check rejects), and the ask-equal refusal below still catches a relay that copied the ask's own id.
let _chainRun = _pathRunId(chain.path);
if (_chainRun === '' && typeof chain.run_id === 'string') _chainRun = chain.run_id.trim();
if (_chainRun === '' || _chainRun === ASK_RUN_ID || _chainRun === _pathRunId(ASK_SCRIPT_PATH)) return _fail('chain emit path run id ' + JSON.stringify(_chainRun) + ' is not a fresh run id (path ' + JSON.stringify(chain.path) + ')');
const chained = await _startLoop(_normPath(chain.path));
if (!chained.ok) return _fail(chained.error);
const r = chained.result;
if (r !== null && typeof r === 'object' && r.__CHAIN_KEY__) return _fail('the chained run returned a second hand-off');
return (r !== null && typeof r === 'object') ? { ...r, chained_from: ASK_RUN_ID } : { chained: r, chained_from: ASK_RUN_ID };
"""
_DEFERRED_START_JS = _STAGE_LOOP_JS + r"""const ASK_RUN_ID = reply.run_id;
const ASK_SCRIPT_PATH = scriptPath;
const first = await _startLoop(ASK_SCRIPT_PATH);
""" + _CHAIN_FOLLOW_JS


#: Longest inline `ask_brief` a deferred script re-types through its relay agent; a longer one rides a repo file.
#: Trap: the relay agent re-types inline params and can mangle a key (seen: `ask_brief_placeholder`); the bar stays low to shrink that surface.
DEFERRED_BRIEF_INLINE_MAX = 200

def _spill_long_brief(root: Path, params: dict) -> dict:
    """`params` with an inline `ask_brief` past `DEFERRED_BRIEF_INLINE_MAX` moved to `scratch/warp/` as `ask_brief_file`,
    so the relay's copied command stays short. Trap: a relay re-typing a long brief mangles the params keys."""
    brief = params.get("ask_brief")
    if not isinstance(brief, str) or len(brief) <= DEFERRED_BRIEF_INLINE_MAX or params.get("ask_brief_file"):
        return params
    spill = root / "scratch" / "warp" / f"defer-brief-{uuid.uuid4().hex[:12]}.md"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(brief, encoding="utf-8", newline="\n")
    return {**{k: v for k, v in params.items() if k != "ask_brief"}, "ask_brief_file": spill.relative_to(root).as_posix()}


def _stage_loop_values(start_args: Optional[dict] = None) -> dict:
    """The `_STAGE_LOOP_JS` placeholders; `start_args` rides every `workflow()` start the loop makes."""
    import json

    from coordinator_core.ops.dispatch_emit.fan_launcher import MAX_STAGE_ROUNDS, _twin_affixes
    from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX, STAGE_PENDING_KEY

    prefix, middle, suffix = _twin_affixes()
    return {
        "__MAX_ROUNDS__": str(MAX_STAGE_ROUNDS),
        "__TWIN_PREFIX__": json.dumps(prefix),
        "__TWIN_MIDDLE__": json.dumps(middle),
        "__TWIN_SUFFIX__": json.dumps(suffix),
        "__RUN_SCRIPT_SUFFIX__": json.dumps(RUN_SCRIPT_SUFFIX),
        "__START_ARGS__": json.dumps(start_args) if start_args else "null",
        "__PENDING_KEY__": STAGE_PENDING_KEY,
    }


def _brief_carry(params: dict) -> dict:
    """The EM-brief keys of `params` a chained run must receive unchanged: the brief (or its spill file) and its digest."""
    from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_DIGEST_PARAM

    return {k: params[k] for k in ("ask_brief", "ask_brief_file", EM_CONTEXT_DIGEST_PARAM) if params.get(k)}


def _chain_follow_values(brief_carry: Optional[dict] = None) -> dict:
    """The `_CHAIN_FOLLOW_JS` and `_EMIT_RELAY_JS` placeholders shared by both start scripts; `brief_carry` rides the chain hop."""
    import json

    from coordinator_core.ops.dispatch_emit.fan_launcher import _EMIT_OP, _INVOKE, EMIT_RELAY_AUTHORITY, LAUNCHER_CHILD_EMIT_PARAM
    from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

    return {
        "__CHAIN_KEY__": CHAIN_HANDOFF_KEY,
        "__BRIEF_CARRY__": json.dumps(brief_carry or {}, ensure_ascii=True),
        "__LAUNCHER_PARAM__": LAUNCHER_CHILD_EMIT_PARAM,
        "__AUTHORITY__": json.dumps(EMIT_RELAY_AUTHORITY),
        "__INVOKE__": json.dumps(_INVOKE),
        "__EMIT_OP__": json.dumps(_EMIT_OP),
    }


#: The launcher meta description per deferral branch, shown to the EM as the workflow's title.
BRANCH_DESCRIPTIONS = {
    "premint": "Emits and starts one submission whose run id the hook minted; the emit runs here by design",
    "overrun": "Emits and starts one submission whose emit outran the hook budget",
    "fan_child": "Emits and starts one fan child; the launcher defers each child's emit",
    "reemit": "Re-emits and starts one submission by hand through dispatch.reemit",
}


def _deferral_branch(params: dict, branch: Optional[str]) -> str:
    """The deferral branch of `params`: `branch` when the caller names it, else read from the params themselves.
    Trap: params cannot tell a re-emit from an unminted overrun, so only the re-emit op passes `branch`."""
    from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM

    if branch is not None:
        return branch
    if params.get(LAUNCHER_CHILD_EMIT_PARAM) is True:
        return "fan_child"
    return "premint" if params.get("preminted_run_id") else "overrun"


def write_deferred_script(root: Path, params: dict, *, branch: Optional[str] = None) -> Path:
    """A script under `scratch/warp/` that re-runs `dispatch.emit(params)` through an op-relay agent and starts what it emits.

    `branch` (a `BRANCH_DESCRIPTIONS` key) picks the launcher's meta description; None derives it from `params`."""
    import json

    branch = _deferral_branch(params, branch)
    if branch not in BRANCH_DESCRIPTIONS:
        raise ValueError(f"unknown deferral branch {branch!r}")

    from coordinator_core.ops.dispatch_emit.fan_launcher import (
        _EMIT_OP, _INVOKE, DEFERRED_EMIT_KEY, EMIT_RELAY_AUTHORITY, LAUNCHER_CHILD_EMIT_PARAM,
    )

    # A launcher starts this script as its child, and a child cannot call workflow(): it hands the emit back instead.
    # Trap: the relay runs `dispatch.emit` over the CLI, which is repo-unkeyed; the in-hook emit got the root
    # in-process, so a raw ask carries none and the CLI emit refuses "requires repo_root or target_root".
    if not params.get("target_root"):
        params = {**params, "target_root": Path(root).as_posix()}
    params = _spill_long_brief(root, params)
    if params.get(LAUNCHER_CHILD_EMIT_PARAM) is True:
        start = f"return {{ {DEFERRED_EMIT_KEY}: {{ path: scriptPath, run_id: reply.run_id, fire_args: reply.fire_args || {{}} }} }};"
    else:
        params = {**params, LAUNCHER_CHILD_EMIT_PARAM: True}
        start_values = {
            **_stage_loop_values(),
            **_chain_follow_values(_brief_carry(params)),
        }
        start = re.sub("|".join(start_values), lambda m: start_values[m.group(0)], _DEFERRED_START_JS)
    params_json = json.dumps(params, default=str, ensure_ascii=True)
    spill = root / "scratch" / "warp" / f"defer-params-{uuid.uuid4().hex[:12]}.json"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(params_json, encoding="utf-8", newline="\n")
    params_file = spill.as_posix()
    values = {
        "__DESCRIPTION__": json.dumps(BRANCH_DESCRIPTIONS[branch]),
        "__START__": start,
        "__REPO_ROOT__": json.dumps(Path(root).as_posix()),
        "__PARAMS_FILE__": json.dumps(params_file),
        "__INVOKE__": json.dumps(_INVOKE),
        "__EMIT_OP__": json.dumps(_EMIT_OP),
        "__AUTHORITY__": json.dumps(EMIT_RELAY_AUTHORITY),
    }
    script = root / "scratch" / "warp" / f"defer-{uuid.uuid4().hex[:12]}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    body = re.sub("|".join(values), lambda m: values[m.group(0)], _DEFERRED_JS)
    # Encoded once and written raw, so the receipt hashes what landed without a read-back.
    script_bytes = body.encode("utf-8")
    script.write_bytes(script_bytes)
    from coordinator_core.ops.workflow_fire.receipt import write_fire_receipt

    write_fire_receipt(script, script_bytes=script_bytes)
    return script
