"""Engine-authored launcher script that starts every ready run of a multi-ask submission.

Writes one Workflow script plus its emission receipt. Never spawns a process and never fires
a run: the harness runs the script the caller's rewritten tool input points at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit import emission_receipt
from coordinator_core.ops.dispatch_emit.nested_run_contract import (
    LAUNCHER_CHILD_ARG,
    STAGE_PENDING_KEY,
    STAGED_MANIFEST_ARG,
)
from coordinator_core.ops.dispatch_emit.reply_sidecar import READ_STAGE_SIDECAR_JS, VERIFY_REPLY_JS
from coordinator_core.session import claimed_write

# Generator-provenance: writes only the caller-named launcher script and its receipt, under scratch/warp/.
GENERATES = []

# S1 verdict: a run adopts one stage in practice; a second round is the cap, never a loop.
MAX_STAGE_ROUNDS = 2

# Args key echoing a child's stage_pending.legMemo back on re-start; no run script emits legMemo yet.
LEG_MEMO_ARG = "legMemo"

_PARENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EMIT_OP = "dispatch.emit"
_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'


@dataclass(frozen=True)
class LaunchChild:
    """One run the launcher emits and starts; emission happens inside the launcher, at child start."""

    run_id: str  # the fan-out's reserved run id
    sizing_rel: str  # the slice sizing, POSIX, repo-relative
    emit_params: Mapping[str, Any]  # dispatch.emit params the emit leg sends (ask, sizing_path, reserved_run_id, ...)


LaunchFn = Callable[[Path, str, Sequence[LaunchChild], Optional[str]], Path]


def launcher_rel(parent_id: str) -> str:
    """Repo-relative POSIX path of the launcher script for ``parent_id``."""
    return f"scratch/warp/{parent_id}.launcher.workflow.mjs"


_META_JS = (
    "export const meta = { name: 'engine-ask-launcher', "
    "description: 'Emits and starts every ready run of one multi-ask submission' }\n"
)

# Trap: the run token rides the emit leg's reply into args at runtime; no token literal is ever rendered here.
_BODY_JS = r"""
const REPO_ROOT = __REPO_ROOT__;
const PARENT_ID = __PARENT_ID__;
const CHILDREN = __CHILDREN__;
const MAX_STAGE_ROUNDS = __MAX_ROUNDS__;
const WARP_PREFIX = REPO_ROOT + '/scratch/warp/';
const INVOKE = __INVOKE__;
const EMIT_OP = __EMIT_OP__;
const EMIT_SCHEMA = {
  type: 'object',
  required: ['path', 'run_id', 'fire_args'],
  properties: {
    path: { type: 'string' },
    run_id: { type: 'string' },
    fire_args: { type: 'object' },
    error: { type: 'string' },
  },
};
__VERIFY_REPLY_JS__
function _makeStageReader(realWorkflow) {
  let captured = null;
  const workflow = async (o) => { captured = await realWorkflow(o); return captured; };
__READ_STAGE_SIDECAR_JS__
  return async (h) => {
    const r = await _readStageSidecar(h, REPO_ROOT);
    return { fail: r.fail, text: captured };
  };
}

function _emitRefusal(child, e) {
  if (!e || typeof e !== 'object') return 'emit reply is not an object';
  if (typeof e.error === 'string' && e.error !== '') return 'emit failed: ' + e.error;
  if (e.run_id !== child.run_id) return 'emit reply run_id ' + String(e.run_id) + ' is not ' + child.run_id;
  if (typeof e.path !== 'string' || e.path === '') return 'emit reply path is not a non-empty string';
  const p = e.path.replace(/\\/g, '/');
  if (p.split('/').indexOf('..') !== -1) return 'emit reply path contains ..';
  if (p.slice(0, WARP_PREFIX.length).toLowerCase() !== WARP_PREFIX.toLowerCase()) {
    return 'emit reply path ' + p + ' is outside ' + WARP_PREFIX;
  }
  if (e.fire_args === null || typeof e.fire_args !== 'object') return 'emit reply carries no fire_args';
  const t = e.fire_args.runToken;
  if (t !== undefined && !(typeof t === 'string' && /^[0-9a-f]{32}$/.test(t))) return 'emit reply runToken is malformed';
  return null;
}

async function _emitLeg(child) {
  const params = JSON.stringify(child.emit_params).replace(/'/g, "'\\''");
  const reply = await agent(
    'Run `' + INVOKE + ' ' + EMIT_OP + " '" + params + "'` once, with the command word exactly as written, and "
    + 'return path, run_id and fire_args from its JSON reply verbatim. When the command is denied or fails, '
    + "return the command's error text verbatim (its stderr or the denial message) in the error field, "
    + 'with path as an empty string and fire_args as {}; on success omit error.',
    { label: 'emit-' + child.run_id, phase: 'emit', agentType: 'general-purpose', model: 'haiku', effort: 'low', schema: EMIT_SCHEMA },
  );
  return reply;
}

async function _runChild(child) {
  let emitted;
  try {
    emitted = await _emitLeg(child);
  } catch (e) {
    return { run_id: child.run_id, status: 'emit_refused', error: String(e && e.message ? e.message : e) };
  }
  const refusal = _emitRefusal(child, emitted);
  if (refusal !== null) return { run_id: child.run_id, status: 'emit_refused', error: refusal };
  const scriptPath = emitted.path.replace(/\\/g, '/');
  const extra = {};
  for (let round = 0; round <= MAX_STAGE_ROUNDS; round++) {
    let result;
    try {
      result = await workflow({ scriptPath: scriptPath, args: Object.assign({}, emitted.fire_args, { __CHILD_ARG__: true }, extra) });
    } catch (e) {
      return { run_id: child.run_id, status: 'failed', error: String(e && e.message ? e.message : e) };
    }
    const pending = (result !== null && typeof result === 'object') ? result.__PENDING_KEY__ : null;
    if (!pending) return { run_id: child.run_id, status: 'ok', result: result };
    const handle = pending.handle;
    if (round >= MAX_STAGE_ROUNDS) {
      return { run_id: child.run_id, status: 'failed', error: 'stage_pending after ' + MAX_STAGE_ROUNDS + ' stage rounds' };
    }
    const supplied = extra.__STAGED_ARG__;
    if (supplied && handle && supplied.handle && supplied.handle.sha256 === handle.sha256) {
      return { run_id: child.run_id, status: 'failed', error: 'stage_pending repeated for a handle already supplied' };
    }
    const read = await _makeStageReader(workflow)(handle);
    if (read.fail) return { run_id: child.run_id, status: 'failed', error: 'stage read: ' + read.fail };
    extra.__STAGED_ARG__ = { handle: handle, text: read.text };
    if (pending.__LEG_MEMO_ARG__ !== undefined) extra.__LEG_MEMO_ARG__ = pending.__LEG_MEMO_ARG__;
  }
  return { run_id: child.run_id, status: 'failed', error: 'stage rounds exhausted' };
}

const _settled = await parallel(CHILDREN.map((child) => () => _runChild(child)));
const runs = CHILDREN.map((child, i) => (
  _settled[i] === null || _settled[i] === undefined
    ? { run_id: child.run_id, status: 'failed', error: 'child threw' }
    : _settled[i]
));
return { parent_id: PARENT_ID, runs: runs };
"""


def _render(root: Path, parent_id: str, children: Sequence[LaunchChild]) -> str:
    lit = lambda value: json.dumps(value, ensure_ascii=True)  # noqa: E731
    literals = [
        {"run_id": c.run_id, "sizing_rel": c.sizing_rel, "emit_params": dict(c.emit_params)} for c in children
    ]
    values = {
        "__REPO_ROOT__": lit(Path(root).as_posix()),
        "__PARENT_ID__": lit(parent_id),
        "__CHILDREN__": lit(literals),
        "__MAX_ROUNDS__": str(MAX_STAGE_ROUNDS),
        "__INVOKE__": lit(_INVOKE),
        "__EMIT_OP__": lit(_EMIT_OP),
        "__CHILD_ARG__": LAUNCHER_CHILD_ARG,
        "__PENDING_KEY__": STAGE_PENDING_KEY,
        "__STAGED_ARG__": STAGED_MANIFEST_ARG,
        "__LEG_MEMO_ARG__": LEG_MEMO_ARG,
        "__VERIFY_REPLY_JS__": VERIFY_REPLY_JS,
        "__READ_STAGE_SIDECAR_JS__": READ_STAGE_SIDECAR_JS,
    }
    # One pass: a placeholder-shaped string inside a child's emit_params must stay literal.
    body = re.sub("|".join(values), lambda m: values[m.group(0)], _BODY_JS)
    return _META_JS + body


def write_launcher(
    root: Path,
    parent_id: str,
    children: Sequence[LaunchChild],
    session_id: Optional[str],
) -> Path:
    """Write the launcher for ``children`` under ``root`` and return its absolute path.

    The script opens with ``export const meta`` and runs each child under ``parallel()``, so one
    child's failure resolves to null and never cancels a sibling. Per child: (1) emit leg, a haiku
    ``agent()`` running ``dispatch.emit`` with ``child.emit_params``, refusing a path outside
    ``scratch/warp/`` or a foreign run_id; (2) start, ``workflow()`` on the emitted script with
    ``LAUNCHER_CHILD_ARG`` true; (3) stage hand-back, on a ``STAGE_PENDING_KEY`` return the
    launcher reads the handle and re-starts the child with ``STAGED_MANIFEST_ARG`` set. Returns
    ``{parent_id, runs: [{run_id, status, result|error}]}``. An emission receipt is recorded.

    Raises ``ValueError`` for no children, a duplicate run_id or an unsafe ``parent_id``, and
    ``FileExistsError`` when a launcher for ``parent_id`` already exists (never overwritten).
    """
    if not children:
        raise ValueError("write_launcher: no children")
    if not _PARENT_ID_RE.match(parent_id):
        raise ValueError(f"write_launcher: parent_id {parent_id!r} is not a plain file-name stem")
    run_ids = [c.run_id for c in children]
    duplicates = sorted({r for r in run_ids if run_ids.count(r) > 1})
    if duplicates:
        raise ValueError(f"write_launcher: duplicate run_id {duplicates}")
    script = _render(Path(root), parent_id, children)
    target = Path(root) / launcher_rel(parent_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = claimed_write.create_exclusive(target, script)
    emission_receipt._write_emission_receipt(written, None, {"session_id": session_id or ""})
    return written
