"""The deferred-emit workflow script: renders a run script that emits one `dispatch.emit` params set through an
op-relay agent, then follows the launcher chain hand-off and answers every `stage_pending` with the staged twin.

Shared by the submit hook (an emit that outran its budget) and the EM-facing re-emit op."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

# The emit-relay constants and helper shared by the deferred and direct-start scripts, which both relay a chain emit.
_EMIT_RELAY_JS = r"""const INVOKE = __INVOKE__;
const EMIT_OP = __EMIT_OP__;
const EMIT_SCHEMA = {
  type: 'object',
  required: ['path', 'run_id', 'fire_args'],
  properties: { path: { type: 'string' }, run_id: { type: 'string' }, fire_args: { type: 'object' }, error: { type: 'string' } },
};
// Trap: the relay can answer a quoted-empty path (`""` or `''`), which is not the empty string.
function _blankPath(p) {
  return typeof p !== 'string' || p.replace(/["'\s]/g, '') === '';
}
"""


_DEFERRED_JS = r"""export const meta = { name: 'engine-ask-deferred-emit', description: 'Emits and starts one submission whose emit outran the hook budget' }
const REPO_ROOT = __REPO_ROOT__;
const PARAMS_FILE = __PARAMS_FILE__;
""" + _EMIT_RELAY_JS + r"""// Trap: ask text carries quotes and `$(`; the command names a params file and never inlines the payload. Trap: a subagent relay is denied recursive or forced removal, so the prompt carries no removal and the file is left in scratch/warp.
const reply = await agent(
  __AUTHORITY__
  + ' Run `' + INVOKE + ' ' + EMIT_OP + ' --params-file "' + PARAMS_FILE + '"` once, with the command word exactly as written. '
  + 'Do not delete or modify any file. Then '
  + 'return path, run_id and fire_args from its JSON reply verbatim. When the command is denied or fails, '
  + "return the command's error text verbatim in the error field, with path as an empty string and fire_args as {}; on success omit error. "
  + 'If you do not run the command for any reason, put that reason verbatim in the error field the same way.',
  { label: 'deferred-emit', phase: 'emit', agentType: 'coordinator:op-relay', model: 'haiku', effort: 'low', schema: EMIT_SCHEMA },
);
if (!reply || _blankPath(reply.path)) {
  return { status: 'emit_refused', error: (reply && reply.error) || 'the deferred emit returned no script path' };
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
async function _startLoop(firstPath) {
  let path = firstPath;
  let lastSha = null;
  for (let round = 0; round <= MAX_STAGE_ROUNDS; round++) {
    const result = START_ARGS === null ? await workflow({ scriptPath: path }) : await workflow({ scriptPath: path, args: START_ARGS });
    const pending = (result !== null && typeof result === 'object') ? result.__PENDING_KEY__ : null;
    if (!pending) return { ok: true, result: result };
    if (round >= MAX_STAGE_ROUNDS) return { ok: false, error: 'stage_pending after ' + MAX_STAGE_ROUNDS + ' stage rounds' };
    const sha = pending.handle && typeof pending.handle === 'object' ? pending.handle.sha256 : undefined;
    if (typeof sha !== 'string' || !/^[0-9a-f]{64}$/.test(sha)) return { ok: false, error: 'stage_pending handle sha256 is not 64 lowercase hex' };
    if (lastSha === sha) return { ok: false, error: 'stage_pending repeated for a handle already supplied' };
    lastSha = sha;
    const base = firstPath.split('/').pop();
    if (!base.endsWith(RUN_SCRIPT_SUFFIX) || base.length === RUN_SCRIPT_SUFFIX.length || base.indexOf('.staged-') !== -1) {
      return { ok: false, error: 'stage resume path is not a run script: ' + firstPath };
    }
    path = REPO_ROOT + '/' + TWIN_PREFIX + base.slice(0, base.length - RUN_SCRIPT_SUFFIX.length) + TWIN_MIDDLE + sha + TWIN_SUFFIX;
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
if (!first.ok) return { status: 'failed', run_id: ASK_RUN_ID, error: first.error };
const handoff = (first.result !== null && typeof first.result === 'object') ? first.result.__CHAIN_KEY__ : null;
if (handoff === null || typeof handoff !== 'object') return first.result;
// Trap: one hop only. A gate agent that already ran dispatch.emit leaves its reply keys in the hand-off; those are never emit params.
const { path: _rp, run_id: _rr, ok: _rk, error: _re, fire_args: _rf, ...chainParams } = handoff;
const _fail = (why) => ({ status: 'failed', run_id: ASK_RUN_ID, error: 'the chained run did not complete (' + why + '); the ask run ' + ASK_RUN_ID + ' stays resumable' });
// Trap: the payload rides a quoted heredoc on stdin; JSON.stringify escapes every newline, so the body is one line and cannot equal the tag.
const _chainTag = 'CHAIN_PARAMS_JSON';
const _chainJson = JSON.stringify({ ...chainParams, __LAUNCHER_PARAM__: true });
let chain;
if (typeof _re === 'string' && _re !== '') return _fail('emit failed: ' + _re);
if (!_blankPath(_rp)) {
  chain = { path: _rp, fire_args: (_rf !== null && typeof _rf === 'object') ? _rf : {} };
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
const _chainRun = _pathRunId(chain.path);
if (_chainRun === '' || _chainRun === ASK_RUN_ID || _chainRun === _pathRunId(ASK_SCRIPT_PATH)) return _fail('chain emit path run id ' + JSON.stringify(_chainRun) + ' is not a fresh run id');
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


def _chain_follow_values() -> dict:
    """The `_CHAIN_FOLLOW_JS` and `_EMIT_RELAY_JS` placeholders shared by both start scripts."""
    import json

    from coordinator_core.ops.dispatch_emit.fan_launcher import _EMIT_OP, _INVOKE, EMIT_RELAY_AUTHORITY, LAUNCHER_CHILD_EMIT_PARAM
    from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

    return {
        "__CHAIN_KEY__": CHAIN_HANDOFF_KEY,
        "__LAUNCHER_PARAM__": LAUNCHER_CHILD_EMIT_PARAM,
        "__AUTHORITY__": json.dumps(EMIT_RELAY_AUTHORITY),
        "__INVOKE__": json.dumps(_INVOKE),
        "__EMIT_OP__": json.dumps(_EMIT_OP),
    }


def write_deferred_script(root: Path, params: dict) -> Path:
    """A script under `scratch/warp/` that re-runs `dispatch.emit(params)` through an op-relay agent and starts what it emits."""
    import json

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
            **_chain_follow_values(),
        }
        start = re.sub("|".join(start_values), lambda m: start_values[m.group(0)], _DEFERRED_START_JS)
    params_json = json.dumps(params, default=str, ensure_ascii=True)
    spill = root / "scratch" / "warp" / f"defer-params-{uuid.uuid4().hex[:12]}.json"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(params_json, encoding="utf-8", newline="\n")
    params_file = spill.as_posix()
    values = {
        "__START__": start,
        "__REPO_ROOT__": json.dumps(Path(root).as_posix()),
        "__PARAMS_FILE__": json.dumps(params_file),
        "__INVOKE__": json.dumps(_INVOKE),
        "__EMIT_OP__": json.dumps(_EMIT_OP),
        "__AUTHORITY__": json.dumps(EMIT_RELAY_AUTHORITY),
    }
    script = root / "scratch" / "warp" / f"defer-{uuid.uuid4().hex[:12]}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(re.sub("|".join(values), lambda m: values[m.group(0)], _DEFERRED_JS), encoding="utf-8")
    from coordinator_core.ops.workflow_fire.receipt import write_fire_receipt

    write_fire_receipt(script)
    return script
