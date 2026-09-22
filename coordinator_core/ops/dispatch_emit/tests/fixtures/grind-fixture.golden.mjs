// This script runs inside the Workflow runner, which executes this
// file's body directly and permits a top-level `return`. `node --check`
// therefore reports `SyntaxError: Illegal return statement` for this
// file -- that is not a defect.

export const meta = {
  name: 'queue-grind:fixture',
  description: 'Queue grind over profile \'fixture\'.',
  phases: ['Grind'],
};


const QUEUE_GRIND_MANIFEST = {"declined": [], "digest": "deadbeef", "entries": [{"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000000", "path": "state/bug-backlog/row0.yaml", "row_id": "row0"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000001", "path": "state/bug-backlog/row1.yaml", "row_id": "row1"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000002", "path": "state/bug-backlog/row2.yaml", "row_id": "row2"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000003", "path": "state/bug-backlog/row3.yaml", "row_id": "row3"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000004", "path": "state/bug-backlog/row4.yaml", "row_id": "row4"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000005", "path": "state/bug-backlog/row5.yaml", "row_id": "row5"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000006", "path": "state/bug-backlog/row6.yaml", "row_id": "row6"}]};

const BATCHES = [{"batch_key": "P0", "id": "P0:b0", "rows": ["row0", "row1", "row2", "row3"]}, {"batch_key": "P0", "id": "P0:b1", "rows": ["row4", "row5", "row6"]}];

const ROUTING = {"commit": {"edges": {}, "kind": "commit", "on_fail": null}, "fix": {"edges": {"NEEDS_WIDER_SCOPE": "widen-exhausted", "NOT_REPRODUCED": "verify", "PEER_DIRTY": "peer-dirty", "baton": "baton", "done": "verify", "needs-judgment": "needs-judgment"}, "kind": "fix", "on_fail": null}, "refute_close": {"edges": {"confirmed": "commit", "refuted": "commit"}, "kind": "refute-close", "on_fail": null}, "triage": {"edges": {"confirmed-bug": "fix", "not-reproduced": "refute_close"}, "kind": "triage", "on_fail": null}, "verify": {"edges": {"fail": "verify-failed", "pass": "commit"}, "kind": "verify", "on_fail": null}};

const TRIAGE_NODE_ID = 'triage';

const PLAN_WEIGHT_FLOOR_RANK = 2;

const _locked = new Set();
const _waiters = [];
function _tryAcquire(keys) {
  if (keys.some((k) => _locked.has(k))) return false;
  keys.forEach((k) => _locked.add(k));
  return true;
}
function _acquire(keys) {
  return new Promise((resolve) => {
    if (_tryAcquire(keys)) { resolve(); return; }
    _waiters.push({ keys, resolve });
  });
}
function _release(keys) {
  keys.forEach((k) => _locked.delete(k));
  for (let i = 0; i < _waiters.length; ) {
    if (_tryAcquire(_waiters[i].keys)) { const w = _waiters.splice(i, 1)[0]; w.resolve(); }
    else i += 1;
  }
}
async function withLock(keys, fn) {
  const _keys = Array.from(new Set(keys)).sort();
  await _acquire(_keys);
  try {
    return await fn();
  } finally {
    _release(_keys);
  }
}

const _TSHIRT_ORDER = ['XS', 'S', 'M', 'L', 'XL', 'XXL'];
function _tshirtRank(size) { const i = _TSHIRT_ORDER.indexOf(size || 'XS'); return i < 0 ? 0 : i; }
function routeAfterTriage(verdict, size, tradeoff) {
  if (_tshirtRank(size) >= PLAN_WEIGHT_FLOOR_RANK) return { kind: 'handback', value: 'baton' };
  if (tradeoff) return { kind: 'handback', value: 'needs-judgment' };
  const target = ROUTING[TRIAGE_NODE_ID].edges[verdict];
  if (!target || !ROUTING[target]) return { kind: 'handback', value: target || 'stage-dead' };
  return { kind: 'node', value: target };
}
function followEdge(nodeId, outcome, row) {
  const node = ROUTING[nodeId];
  const target = node.edges[outcome];
  if (target === undefined) {
    if (node.on_fail && !ROUTING[node.on_fail]) return { kind: 'handback', value: node.on_fail };
    if (node.on_fail && !row.onFailUsed) { row.onFailUsed = true; return { kind: 'node', value: node.on_fail }; }
    return { kind: 'handback', value: 'stage-dead' };
  }
  if (!ROUTING[target]) return { kind: 'handback', value: target };
  return { kind: 'node', value: target };
}
function applyRoute(row, rowId, route, reason) {
  if (route.kind === 'handback') { row.done = true; _handedBack.push({ row: rowId, type: route.value, reason }); }
  else { row.node = route.value; }
}
function _lockKeysFor(row, rowId) { return row.declaredFiles.concat([`ledger:${rowId}`]); }

const WINDOW = 2;

const BATCH_SIZE = 4;

const RESERVE = 23144;

const MAX_AGENT_CALLS = 40;

const BUDGET_TOKENS = null;

const RUN_ID = args.run_stamp;

const SCRIPT_PATH = args.script_path;

const PROFILE_NAME = 'fixture';

const PROFILE_DIR = args.profile_dir;

const APPETITE_NAME = 'standard';

const RESOLVED_KNOBS = {"batch_size": {"@unkeyed": 4, "default": 4}, "concurrency": 4, "extra_verification": false, "limit": null, "max_agent_calls": 40, "triage_depth": {"default": "standard"}, "where": [], "window": 2};

const MANIFEST_DIGEST = 'deadbeef';

const TRIAGE_DEPTH_BY_KEY = {"P0": "standard"};

const VERIFY_SPEC = {"verify": {"P0": {"mode": "op", "op": "lessons.verify_extraction"}}};

function _ledgerPathFor(rowId) { return `state/queue-grind/${PROFILE_NAME}/${rowId}.jsonl`; }

let _callCount = 0;

const _agentCallsByStageKind = {};

function _recordCall(kind) { _callCount += 1; _agentCallsByStageKind[kind] = (_agentCallsByStageKind[kind] || 0) + 1; }

const _startSpent = budget.spent();

let _exhausted = false;

const _handedBack = [];

const _settled = [];

async function _triageCall(batchId, batchKey, rowIds) {
  const rowsData = rowIds.map((r) => {
    const e = QUEUE_GRIND_MANIFEST.entries.find((x) => x.row_id === r);
    return { row_id: r, path: e.path, digest: e.digest };
  });
    const _result = await agent('You are the triage stage. Your rows (row_id/path/digest) are: ' + (JSON.stringify(rowsData)) + '. Run `grind-row check --manifest ' + (SCRIPT_PATH) + ' --batch ' + (batchId) + ' --repo-root .' + '` first, and skip any row it reports as `stale` or `vanished`. For every remaining row, decide a verdict, cite the evidence for it, size the row XS through XXL with the evidence for that size, and write a fix plan. Only fill in a tradeoff statement when the fix genuinely carries one -- leave it empty otherwise. Name every file your triage declares the row touches. As you finish each row, run `grind-row append --profile fixture --row-id <its row_id> --digest <its digest> --stage triage --verdict <its verdict> --outcome <its verdict> --evidence-file <a file with your evidence> --run-stamp ' + (RUN_ID) + ' --repo-root .' + '` immediately (idempotent under a retried agent -- an identical line already appended is not re-appended) and write the per-batch triage record to state/queue-grind/fixture/run-1/records/' + (batchId) + '.json. Triage depth for this batch is \'' + (TRIAGE_DEPTH_BY_KEY[batchKey]) + '\'. You do not stage or commit anything. Only the committer stage does that.', { label: 'triage', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'medium', schema: {"properties": {"rows": {"items": {"properties": {"declared_files": {"items": {"type": "string"}, "type": "array"}, "evidence": {"type": "string"}, "fix_plan": {"type": "string"}, "row": {"type": "string"}, "sizing_evidence": {"type": "string"}, "tradeoff": {"type": "string"}, "tshirt_size": {"enum": ["L", "M", "S", "XL", "XS", "XXL"], "type": "string"}, "verdict": {"type": "string"}}, "required": ["row", "verdict", "evidence", "tshirt_size", "sizing_evidence", "tradeoff", "declared_files", "fix_plan"], "type": "object"}, "type": "array"}}, "required": ["rows"], "type": "object"} });
    _recordCall('triage');
    return (_result && _result.rows) || [];
}

async function _closeCall(proposals) {
    const _result = await agent('You are the refute-close stage. Your close proposals (row_id/path/digest/evidence) are: ' + (JSON.stringify(proposals)) + '. For each one, actively try to refute it -- look for evidence the row is not actually resolved. For every proposal that survives that attempt, run `grind-row close --profile-dir ' + (PROFILE_DIR) + ' --profile fixture --row <its path> --digest <its digest> --verdict refute-close --evidence-file <a file with your evidence> --closed-by refute-close --run-stamp ' + (RUN_ID) + ' --repo-root .' + '`, and report the `{old,new}` path pair it prints as that row\'s `new_path`. If `grind-row close` exits 3 (digest mismatch -- the row changed since the manifest was emitted), put that row\'s id in `stale` instead. Report every proposal you refuted along with why, and never run `grind-row close` for one of those. You do not stage or commit anything. Only the committer stage does that.', { label: 'close', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'medium', schema: {"properties": {"confirmed": {"items": {"properties": {"new_path": {"type": "string"}, "row": {"type": "string"}}, "required": ["row", "new_path"], "type": "object"}, "type": "array"}, "refuted": {"items": {"properties": {"reason": {"type": "string"}, "row": {"type": "string"}}, "required": ["row", "reason"], "type": "object"}, "type": "array"}, "stale": {"items": {"type": "string"}, "type": "array"}}, "required": ["confirmed", "refuted"], "type": "object"} });
    _recordCall('refute-close');
    return _result;
}

async function _fixCall(row) {
    const _result = await agent('You are the fix stage for row ' + (row.rowId) + '. You hold the lock on [' + ((row.declaredFiles).join(', ')) + '] plus `ledger:' + (row.rowId) + '`. Before doing any work, pre-check every locked file for peer dirt -- if a locked file has changed under you since the lock was acquired, stop and report PEER_DIRTY rather than fixing over it. If the fix needs files beyond your locked set, stop and report NEEDS_WIDER_SCOPE with the extra files, and take no other action. If the fix needs a plan before it can proceed, report NEEDS_PLAN. If your fix genuinely carries a tradeoff triage did not catch, report that tradeoff instead of proceeding. Otherwise, fix the row, run its tests, report every file you touched and every file you created, and when they pass run `grind-row close --profile-dir ' + (PROFILE_DIR) + ' --profile fixture --row ' + (row.path) + ' --digest ' + (row.digest) + ' --verdict fix --evidence-file <a file with your evidence> --closed-by fix --run-stamp ' + (RUN_ID) + ' --repo-root .`, reporting the `{old,new}` path pair it prints as `close_result`. If `grind-row close` exits 3 (digest mismatch -- the row changed since the manifest was emitted), report MANIFEST_STALE and stop.' + ((row.verifyFeedback ? (' Verifier feedback from your last attempt: ' + row.verifyFeedback) : '')) + ' You do not stage or commit anything. Only the committer stage does that.', { label: 'fix', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"close_result": {"properties": {"new": {"type": "string"}, "old": {"type": "string"}}, "type": "object"}, "created_files": {"items": {"type": "string"}, "type": "array"}, "extra_files": {"items": {"type": "string"}, "type": "array"}, "outcome": {"enum": ["done", "NEEDS_WIDER_SCOPE", "PEER_DIRTY", "NOT_REPRODUCED", "NEEDS_PLAN", "MANIFEST_STALE"], "type": "string"}, "touched_files": {"items": {"type": "string"}, "type": "array"}, "tradeoff": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
    _recordCall('fix');
    return _result;
}

async function _verifyCall(row) {
  const spec = ((VERIFY_SPEC[row.node] || {})[row.batchKey]) || { mode: 'agent', op: null };
  if (spec.mode === 'op') {
      const _result = await agent('Run `coordinator-invoke ' + (spec.op) + ' --params-file state/queue-grind/fixture/run-1/records/' + (row.batchId) + '.json` and return its JSON output verbatim. Exit 0 means the batch passes. A non-zero exit means the JSON output names the failing row ids -- return it unchanged either way; do not summarize or reinterpret it. You do not stage or commit anything. Only the committer stage does that.', { label: 'verify-op', phase: 'Grind', agentType: 'coordinator:queue-grind-op-runner', model: 'sonnet', effort: 'low', schema: {"properties": {"exit_code": {"type": "integer"}, "output": {"type": "object"}}, "required": ["exit_code", "output"], "type": "object"} });
      _recordCall('verify');
      const _failing = (_result.output && _result.output.failing_ids) || [];
      const _pass = _result.exit_code === 0 || !_failing.includes(row.rowId);
      return { outcome: _pass ? 'pass' : 'fail', reason: JSON.stringify(_result.output) };
  }
    const _result = await agent('You are the verify stage. Try to reject the fix you are handed -- look for a way it fails, not a reason to wave it through. You are read-only apart from running the named tests: do not edit any file. Report pass only if your attempt to reject it failed. You do not stage or commit anything. Only the committer stage does that.', { label: 'verify-agent', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"outcome": {"enum": ["pass", "fail"], "type": "string"}, "reason": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
    _recordCall('verify');
    return _result;
}

async function _commitCall(row) {
    const _result = await agent('You are the committer for row ' + (row.rowId) + '. You are the only stage that stages or commits anything. Stage exactly this touched list: [' + ((row.touchedFiles).join(', ')) + '], plus this row\'s ledger deletion via `grind-row settle`.' + ' Pass --declared-revert for every one of these removed paths: [' + ((row.removedFiles.concat([_ledgerPathFor(row.rowId)])).join(', ')) + '].' + ' Then commit. If the outcome is indeterminate, reconcile it against `git log` and `git status` before doing anything else -- never retry blind.', { label: 'commit', phase: 'Grind', agentType: 'coordinator:git-commit-agent', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["committed", "commit-failed"], "type": "string"}, "sha": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
    _recordCall('commit');
    return _result;
}

async function _undoCall(row) {
    const _result = await agent('Restore these files from HEAD: [' + ((row.touchedFiles).join(', ')) + '], and remove these files the fix created: [' + ((row.createdFiles).join(', ')) + ']. You do not stage or commit anything. Only the committer stage does that.', { label: 'undo', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["undone"], "type": "string"}}, "required": ["outcome"], "type": "object"} });
    _recordCall('undo');
    return _result;
}

function _counts() {
  const by_type = {};
  for (const h of _handedBack) { by_type[h.type] = (by_type[h.type] || 0) + 1; }
  const by_outcome = {};
  for (const s of _settled) { by_outcome[s.outcome] = (by_outcome[s.outcome] || 0) + 1; }
  return { by_type, by_outcome };
}
function _spend() {
  return { output_tokens: budget.spent() - _startSpent, agent_calls_total: _callCount, agent_calls_by_stage_kind: _agentCallsByStageKind };
}
function _runCostRecord() {
  return { profile: PROFILE_NAME, appetite: APPETITE_NAME, run_id: RUN_ID, resolved_knobs: RESOLVED_KNOBS, manifest_digest: MANIFEST_DIGEST, counts: _counts(), spend: _spend() };
}

const _rows = {};
for (const b of BATCHES) {
  for (const r of b.rows) {
    const _entry = QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r);
    const _path = _entry.path;
    _rows[r] = { rowId: r, batchId: b.id, batchKey: b.batch_key, path: _path, digest: _entry.digest, node: null, declaredFiles: [_path], touchedFiles: [_path], removedFiles: [], createdFiles: [], evidence: '', verifyFeedback: '', fixNode: null, widened: false, verifyRetried: false, onFailUsed: false, done: false, sha: '' };
  }
}

function _pendingRow(batch) {
  for (const r of batch.rows) { const row = _rows[r]; if (!row.done && row.node) return r; }
  return null;
}
function _batchDone(batch) { return batch.rows.every((r) => _rows[r].done); }
function _batchUnsettledRows(batch) { return batch.rows.filter((r) => _rows[r].done && !_settled.some((s) => s.row === r)); }

async function _triageBatch(batchState) {
  const batch = BATCHES.find((b) => b.id === batchState.id);
  const rowsOut = await _triageCall(batchState.id, batchState.batchKey, batch.rows);
  batchState.triaged = true;
  const _seen = new Set();
  for (const rec of rowsOut) {
    const row = _rows[rec.row];
    if (!row) continue;
    _seen.add(rec.row);
    row.evidence = rec.evidence || '';
    if (rec.declared_files && rec.declared_files.length) { row.declaredFiles = rec.declared_files; row.touchedFiles = rec.declared_files; }
    const route = routeAfterTriage(rec.verdict, rec.tshirt_size, rec.tradeoff || '');
    applyRoute(row, rec.row, route, `triage verdict ${rec.verdict}`);
  }
  for (const r of batch.rows) {
    if (!_seen.has(r) && !_rows[r].done && !_rows[r].node) {
      _rows[r].done = true;
      _handedBack.push({ row: r, type: 'stage-dead', reason: 'triage never returned a record for this row' });
    }
  }
}

async function _closeBatch(batchState) {
  const closeNodeIds = Object.keys(ROUTING).filter((n) => ROUTING[n].kind === 'refute-close');
  const batch = BATCHES.find((b) => b.id === batchState.id);
  const proposals = batch.rows.filter((r) => closeNodeIds.includes(_rows[r].node)).map((r) => {
    const row = _rows[r];
    return { row_id: r, path: row.path, digest: (QUEUE_GRIND_MANIFEST.entries.find((e) => e.row_id === r) || {}).digest, evidence: row.evidence };
  });
  const result = await _closeCall(proposals);
  batchState.closeCalled = true;
  for (const item of (result.confirmed || [])) {
    const row = _rows[item.row];
    if (!row || row.done || !row.node) continue;
    row.removedFiles = row.removedFiles.concat([row.path]);
    row.touchedFiles = row.touchedFiles.concat([item.new_path]);
    applyRoute(row, item.row, followEdge(row.node, 'confirmed', row), 'refute-close confirmed');
  }
  for (const entry of (result.refuted || [])) {
    const row = _rows[entry.row];
    if (!row || row.done || !row.node) continue;
    applyRoute(row, entry.row, followEdge(row.node, 'refuted', row), 'refute-close refuted');
  }
  for (const staleId of (result.stale || [])) {
    const row = _rows[staleId];
    if (!row || row.done || !row.node) continue;
    row.done = true; _handedBack.push({ row: staleId, type: 'manifest-stale', reason: 'refute-close close exited 3 (digest mismatch)' });
  }
}

async function _fixStage(rowId) {
  const row = _rows[rowId];
  const lockKeys = _lockKeysFor(row, rowId);
  const _priorNode = row.node;
  const result = await withLock(lockKeys, async () => _fixCall(row));
  const tradeoff = result.tradeoff || '';
  const outcome = result.outcome;
  if (result.touched_files && result.touched_files.length) { row.touchedFiles = result.touched_files; }
  row.createdFiles = result.created_files || [];
  if (outcome === 'done' && result.close_result) {
    row.removedFiles = row.removedFiles.concat([result.close_result.old || row.path]);
    row.touchedFiles = row.touchedFiles.concat([result.close_result.new]);
  }
  if (tradeoff) { row.done = true; _handedBack.push({ row: rowId, type: 'needs-judgment', reason: 'fix reported a tradeoff' }); return; }
  if (outcome === 'NEEDS_PLAN') { row.done = true; _handedBack.push({ row: rowId, type: 'baton', reason: 'fix reported NEEDS_PLAN' }); return; }
  if (outcome === 'PEER_DIRTY') { row.done = true; _handedBack.push({ row: rowId, type: 'peer-dirty', reason: 'fix reported PEER_DIRTY' }); return; }
  if (outcome === 'MANIFEST_STALE') { row.done = true; _handedBack.push({ row: rowId, type: 'manifest-stale', reason: 'fix reported MANIFEST_STALE' }); return; }
  if (outcome === 'NEEDS_WIDER_SCOPE') {
    if (!row.widened) { row.widened = true; row.declaredFiles = row.declaredFiles.concat(result.extra_files || []); return; }
    row.done = true; _handedBack.push({ row: rowId, type: 'widen-exhausted', reason: 'second NEEDS_WIDER_SCOPE' }); return;
  }
  row.fixNode = _priorNode;
  applyRoute(row, rowId, followEdge(row.node, outcome, row), `fix outcome ${outcome}`);
}

async function _verifyStage(rowId) {
  const row = _rows[rowId];
  const lockKeys = _lockKeysFor(row, rowId);
  const result = await withLock(lockKeys, async () => _verifyCall(row));
  if (result.outcome === 'fail') {
    if (!row.verifyRetried) {
      row.verifyRetried = true;
      row.verifyFeedback = result.reason || '';
      row.node = row.fixNode || row.node;
      return;
    }
    await withLock(lockKeys, async () => _undoCall(row));
    row.done = true; _handedBack.push({ row: rowId, type: 'rejected-after-retry', reason: 'verify failed twice' }); return;
  }
  applyRoute(row, rowId, followEdge(row.node, result.outcome, row), `verify outcome ${result.outcome}`);
}

async function _commitStage(rowId) {
  const row = _rows[rowId];
  const result = await withLock(['@commit'], async () => _commitCall(row));
  if (result.outcome === 'commit-failed') { row.done = true; _handedBack.push({ row: rowId, type: 'commit-failed', reason: 'commit did not land' }); return; }
  row.done = true; row.sha = result.sha || '';
  _settled.push({ row: rowId, outcome: 'committed', sha: row.sha });
}

async function _dispatchRow(rowId, batchState) {
  const row = _rows[rowId];
  const kind = ROUTING[row.node].kind;
  if (kind === 'fix') { await _fixStage(rowId); }
  else if (kind === 'verify') { await _verifyStage(rowId); }
  else if (kind === 'commit') { await _commitStage(rowId); }
  else if (kind === 'refute-close') { if (!batchState.closeCalled) { await _closeBatch(batchState); } }
}

async function _finishBatch(batchState) {
  const unsettled = _batchUnsettledRows(batchState);
  const unsettledPaths = unsettled.map((r) => _ledgerPathFor(r));
  const lockKeys = ['@commit'].concat(unsettled.map((r) => `ledger:${r}`));
  if (unsettled.length) {
    await withLock(lockKeys, async () => {
        const _result = await agent('You are the committer for a ledger-only commit. You are the only stage that stages or commits anything. Stage exactly these unsettled rows\' ledger files: [' + ((unsettledPaths).join(', ')) + '], and nothing else.' + ' Then commit. If the outcome is indeterminate, reconcile it against `git log` and `git status` before doing anything else -- never retry blind.', { label: 'commit-ledger:batch', phase: 'Grind', agentType: 'coordinator:git-commit-agent', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["committed", "commit-failed"], "type": "string"}, "sha": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
        _recordCall('commit');
        return _result;
    });
  }
}

async function _drainCommit() {
  const unsettled = Object.values(_rows).filter((r) => r.done && !_settled.some((s) => s.row === r.rowId)).map((r) => r.rowId);
  const unsettledPaths = unsettled.map((r) => _ledgerPathFor(r));
  const lockKeys = ['@commit'].concat(unsettled.map((r) => `ledger:${r}`));
  await withLock(lockKeys, async () => {
      const _result = await agent('You are the committer for a ledger-only commit. You are the only stage that stages or commits anything. Stage exactly these unsettled rows\' ledger files: [' + ((unsettledPaths).join(', ')) + '], and nothing else.' + ' This is the drain commit: also write and stage state/queue-grind/fixture/runs/' + (RUN_ID) + '.json in this same commit.' + ' Its content is exactly this JSON, byte for byte: ' + (JSON.stringify(_runCostRecord())) + '.' + ' Then commit. If the outcome is indeterminate, reconcile it against `git log` and `git status` before doing anything else -- never retry blind.', { label: 'commit-ledger:drain', phase: 'Grind', agentType: 'coordinator:git-commit-agent', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["committed", "commit-failed"], "type": "string"}, "sha": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
      _recordCall('commit');
      return _result;
  });
}

const _admitted = {};
const _queue = BATCHES.map((b) => b.id);
function _anyAdmittedHasPendingDownstream() {
  return Object.values(_admitted).some((bs) => !_batchDone(BATCHES.find((b) => b.id === bs.id)) && _pendingRow(BATCHES.find((b) => b.id === bs.id)) !== null);
}
async function _runBatchWorker(batchState) {
  const batch = BATCHES.find((b) => b.id === batchState.id);
  if (!batchState.triaged) { await _triageBatch(batchState); }
  while (!_batchDone(batch)) {
    const pending = _pendingRow(batch);
    if (pending === null) break;
    await _dispatchRow(pending, batchState);
  }
  await _finishBatch(batchState);
  delete _admitted[batchState.id];
}
async function runGrind() {
  const workers = [];
  while (_queue.length || workers.length) {
    if (_queue.length && workers.length < WINDOW && !_anyAdmittedHasPendingDownstream()) {
      if (MAX_AGENT_CALLS !== null && _callCount >= MAX_AGENT_CALLS) { _exhausted = true; }
      else {
        const _spentDelta = budget.spent() - _startSpent;
        if (BUDGET_TOKENS !== null && _spentDelta + RESERVE > BUDGET_TOKENS) { _exhausted = true; }
        else if (budget.total !== undefined && budget.total !== null && (budget.remaining() ?? 0) <= RESERVE) { _exhausted = true; }
      }
      if (!_exhausted) {
        const bid = _queue.shift();
        const batchState = { id: bid, batchKey: BATCHES.find((b) => b.id === bid).batch_key, triaged: false, closeCalled: false };
        _admitted[bid] = batchState;
        workers.push(_runBatchWorker(batchState));
        continue;
      }
    }
    if (!workers.length) break;
    const idx = await Promise.race(workers.map((w, i) => w.then(() => i)));
    workers.splice(idx, 1);
  }
  if (_exhausted) {
    for (const bid of _queue) { for (const r of BATCHES.find((b) => b.id === bid).rows) _handedBack.push({ row: r, type: 'budget-exhausted', reason: 'admission ceiling reached' }); }
    for (const bid of Object.keys(_admitted)) { for (const r of BATCHES.find((b) => b.id === bid).rows) if (!_rows[r].done) _handedBack.push({ row: r, type: 'budget-exhausted', reason: 'admission ceiling reached' }); }
  }
  await _drainCommit();
}
await runGrind();

const HANDBACK = {
  schema: 'queue-grind-handback/1',
  profile: PROFILE_NAME,
  appetite: APPETITE_NAME,
  handed_back: _handedBack,
  settled: _settled,
  counts: _counts(),
  spend: _spend(),
};

return HANDBACK;
