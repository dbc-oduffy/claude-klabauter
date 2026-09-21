// This script runs inside the Workflow runner, which executes this
// file's body directly and permits a top-level `return`. `node --check`
// therefore reports `SyntaxError: Illegal return statement` for this
// file -- that is not a defect.

export const meta = {
  name: 'queue-grind:fixture',
  description: 'Queue grind over profile \'fixture\'.',
  phases: ['Grind'],
};


const MANIFEST = {"digest": "deadbeef", "entries": [{"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000000", "path": "state/bug-backlog/row0.yaml", "row_id": "row0"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000001", "path": "state/bug-backlog/row1.yaml", "row_id": "row1"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000002", "path": "state/bug-backlog/row2.yaml", "row_id": "row2"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000003", "path": "state/bug-backlog/row3.yaml", "row_id": "row3"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000004", "path": "state/bug-backlog/row4.yaml", "row_id": "row4"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000005", "path": "state/bug-backlog/row5.yaml", "row_id": "row5"}, {"batch_key": "P0", "digest": "0000000000000000000000000000000000000000000000000000000000000006", "path": "state/bug-backlog/row6.yaml", "row_id": "row6"}]};

// Runtime mutex: a per-key lock (file paths plus `ledger:<row-id>`),
// acquired atomically over the union of keys a stage needs. A single
// '@commit' key serialises every commit call, including the
// ledger-only commit (eng-director F3).
async function withLock(keys, fn) {
  const _keys = Array.from(new Set(keys)).sort();
  await lock.acquire(_keys);
  try {
    return await fn();
  } finally {
    await lock.release(_keys);
  }
}

const DOWNSTREAM_SEQUENCE = {"close": ["close", "commit"], "fix": ["fix", "verify", "commit"], "handback": []};

const WINDOW = 2;

const BATCH_SIZE = 4;

const RESERVE = 23144;

const MAX_AGENT_CALLS = 40;

const BUDGET_TOKENS = null;

let _callCount = 0;

let _exhausted = false;

const _startSpent = budget.spent();

const _agentCallsByStageKind = {};

function _recordCall(kind) { _callCount += 1; _agentCallsByStageKind[kind] = (_agentCallsByStageKind[kind] || 0) + 1; }

async function _runDownstream(b) {
  const stage = b.pending[0];
  const _lockKeys = [b.id, `ledger:${b.id}`];
  const result = await withLock(_lockKeys, async () => {
  if (b.id === 'P0:b0') {
  await agent('You are the fix stage for row row0. You hold the lock on [state/bug-backlog/row0.yaml, state/bug-backlog/row1.yaml, state/bug-backlog/row2.yaml, state/bug-backlog/row3.yaml] plus `ledger:row0`. Before doing any work, pre-check every locked file for peer dirt -- if a locked file has changed under you since the lock was acquired, stop and report PEER_DIRTY rather than fixing over it. If the fix needs files beyond your locked set, stop and report NEEDS_WIDER_SCOPE with the extra files, and take no other action. If the fix needs a plan before it can proceed, report NEEDS_PLAN. If your fix genuinely carries a tradeoff triage did not catch, report that tradeoff instead of proceeding. Otherwise, fix the row, run its tests, and run `grind-row close` when they pass. You do not stage or commit anything. Only the committer stage does that.', { label: 'fix:P0:b0', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"extra_files": {"items": {"type": "string"}, "type": "array"}, "outcome": {"enum": ["done", "NEEDS_WIDER_SCOPE", "PEER_DIRTY", "NOT_REPRODUCED", "NEEDS_PLAN"], "type": "string"}, "tradeoff": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
  const _fixOutcome = 'done';
  if (_fixOutcome === 'NEEDS_WIDER_SCOPE') { return { outcome: 'NEEDS_WIDER_SCOPE' }; }
  await agent('You are the verify stage. Try to reject the fix you are handed -- look for a way it fails, not a reason to wave it through. You are read-only apart from running the named tests: do not edit any file. Report pass only if your attempt to reject it failed. You do not stage or commit anything. Only the committer stage does that.', { label: 'verify:P0:b0', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"outcome": {"enum": ["pass", "fail"], "type": "string"}, "reason": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
  const _verifyOutcome = 'pass';
  if (_verifyOutcome === 'fail') { return { outcome: 'fail' }; }
  return await withLock(['@commit'], async () => {
  await agent('You are the committer for row row0. You are the only stage that stages or commits anything. Stage exactly this touched list: [state/bug-backlog/row0.yaml, state/bug-backlog/row1.yaml, state/bug-backlog/row2.yaml, state/bug-backlog/row3.yaml], plus this row\'s ledger deletion via `grind-row settle`. Then commit. If the outcome is indeterminate, reconcile it against `git log` and `git status` before doing anything else -- never retry blind.', { label: 'commit:P0:b0', phase: 'Grind', agentType: 'coordinator:git-commit-agent', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["committed", "commit-failed"], "type": "string"}, "sha": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
    return { outcome: 'committed' };
  });
  }
  if (b.id === 'P0:b1') {
  await agent('You are the fix stage for row row4. You hold the lock on [state/bug-backlog/row4.yaml, state/bug-backlog/row5.yaml, state/bug-backlog/row6.yaml] plus `ledger:row4`. Before doing any work, pre-check every locked file for peer dirt -- if a locked file has changed under you since the lock was acquired, stop and report PEER_DIRTY rather than fixing over it. If the fix needs files beyond your locked set, stop and report NEEDS_WIDER_SCOPE with the extra files, and take no other action. If the fix needs a plan before it can proceed, report NEEDS_PLAN. If your fix genuinely carries a tradeoff triage did not catch, report that tradeoff instead of proceeding. Otherwise, fix the row, run its tests, and run `grind-row close` when they pass. You do not stage or commit anything. Only the committer stage does that.', { label: 'fix:P0:b1', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"extra_files": {"items": {"type": "string"}, "type": "array"}, "outcome": {"enum": ["done", "NEEDS_WIDER_SCOPE", "PEER_DIRTY", "NOT_REPRODUCED", "NEEDS_PLAN"], "type": "string"}, "tradeoff": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
  const _fixOutcome = 'done';
  if (_fixOutcome === 'NEEDS_WIDER_SCOPE') { return { outcome: 'NEEDS_WIDER_SCOPE' }; }
  await agent('You are the verify stage. Try to reject the fix you are handed -- look for a way it fails, not a reason to wave it through. You are read-only apart from running the named tests: do not edit any file. Report pass only if your attempt to reject it failed. You do not stage or commit anything. Only the committer stage does that.', { label: 'verify:P0:b1', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'high', schema: {"properties": {"outcome": {"enum": ["pass", "fail"], "type": "string"}, "reason": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
  const _verifyOutcome = 'pass';
  if (_verifyOutcome === 'fail') { return { outcome: 'fail' }; }
  return await withLock(['@commit'], async () => {
  await agent('You are the committer for row row4. You are the only stage that stages or commits anything. Stage exactly this touched list: [state/bug-backlog/row4.yaml, state/bug-backlog/row5.yaml, state/bug-backlog/row6.yaml], plus this row\'s ledger deletion via `grind-row settle`. Then commit. If the outcome is indeterminate, reconcile it against `git log` and `git status` before doing anything else -- never retry blind.', { label: 'commit:P0:b1', phase: 'Grind', agentType: 'coordinator:git-commit-agent', model: 'sonnet', effort: 'low', schema: {"properties": {"outcome": {"enum": ["committed", "commit-failed"], "type": "string"}, "sha": {"type": "string"}}, "required": ["outcome"], "type": "object"} });
    return { outcome: 'committed' };
  });
  }
    return { outcome: 'done' };
  });
  if (stage === 'fix' && result.outcome === 'NEEDS_WIDER_SCOPE' && !b.widened) { b.widened = true; return; }
  if (stage === 'fix' && result.outcome === 'NEEDS_WIDER_SCOPE' && b.widened) {
    b.done = true; _handedBack.push({ row: b.id, type: 'widen-exhausted', reason: 'second NEEDS_WIDER_SCOPE' }); return;
  }
  if (stage === 'verify' && result.outcome === 'fail' && !b.verifyRetried) { b.verifyRetried = true; return; }
  if (stage === 'verify' && result.outcome === 'fail' && b.verifyRetried) {
    await withLock([b.id, `ledger:${b.id}`], async () => { _recordCall('undo'); });
    b.done = true; _handedBack.push({ row: b.id, type: 'rejected-after-retry', reason: 'verify failed twice' }); return;
  }
  if (stage === 'commit' && result.outcome === 'commit-failed') {
    b.done = true; _handedBack.push({ row: b.id, type: 'commit-failed', reason: 'commit did not land' }); return;
  }
  b.pending = b.pending.slice(1);
  if (b.pending.length === 0) { b.done = true; _settled.push({ row: b.id, outcome: result.outcome, sha: result.sha || '' }); }
}

async function _runTriage(batchId) {
  if (batchId === 'P0:b0') {
  await agent('You are the triage stage. Run `grind-row check --manifest <script> --batch P0:b0` first, and skip any row it reports as `stale` or `vanished`. For every remaining row, decide a verdict, cite the evidence for it, size the row XS through XXL with the evidence for that size, and write a fix plan. Only fill in a tradeoff statement when the fix genuinely carries one -- leave it empty otherwise. Name every file your triage declares the row touches. As you finish each row, append one ledger line for it immediately (a retried agent skips rows that already have one) and write the per-batch triage record to state/queue-grind/fixture/run-1/records/P0:b0.json. Triage depth for this batch is \'standard\'. You do not stage or commit anything. Only the committer stage does that.', { label: 'triage:P0:b0', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'medium', schema: {"properties": {"rows": {"items": {"properties": {"declared_files": {"items": {"type": "string"}, "type": "array"}, "evidence": {"type": "string"}, "fix_plan": {"type": "string"}, "row": {"type": "string"}, "sizing_evidence": {"type": "string"}, "tradeoff": {"type": "string"}, "tshirt_size": {"enum": ["L", "M", "S", "XL", "XS", "XXL"], "type": "string"}, "verdict": {"type": "string"}}, "required": ["row", "verdict", "evidence", "tshirt_size", "sizing_evidence", "tradeoff", "declared_files", "fix_plan"], "type": "object"}, "type": "array"}}, "required": ["rows"], "type": "object"} });
    _recordCall('triage');
    const bucket = 'fix';
    if (bucket === 'handback') {
      _handedBack.push({ row: batchId, type: 'needs-judgment', reason: 'triage verdict UNCLEAR' });
      return;
    }
    _admitted.push({ id: batchId, pending: DOWNSTREAM_SEQUENCE[bucket].slice(), widened: false, verifyRetried: false, done: false });
    return;
  }
  if (batchId === 'P0:b1') {
  await agent('You are the triage stage. Run `grind-row check --manifest <script> --batch P0:b1` first, and skip any row it reports as `stale` or `vanished`. For every remaining row, decide a verdict, cite the evidence for it, size the row XS through XXL with the evidence for that size, and write a fix plan. Only fill in a tradeoff statement when the fix genuinely carries one -- leave it empty otherwise. Name every file your triage declares the row touches. As you finish each row, append one ledger line for it immediately (a retried agent skips rows that already have one) and write the per-batch triage record to state/queue-grind/fixture/run-1/records/P0:b1.json. Triage depth for this batch is \'standard\'. You do not stage or commit anything. Only the committer stage does that.', { label: 'triage:P0:b1', phase: 'Grind', agentType: 'general-purpose', model: 'sonnet', effort: 'medium', schema: {"properties": {"rows": {"items": {"properties": {"declared_files": {"items": {"type": "string"}, "type": "array"}, "evidence": {"type": "string"}, "fix_plan": {"type": "string"}, "row": {"type": "string"}, "sizing_evidence": {"type": "string"}, "tradeoff": {"type": "string"}, "tshirt_size": {"enum": ["L", "M", "S", "XL", "XS", "XXL"], "type": "string"}, "verdict": {"type": "string"}}, "required": ["row", "verdict", "evidence", "tshirt_size", "sizing_evidence", "tradeoff", "declared_files", "fix_plan"], "type": "object"}, "type": "array"}}, "required": ["rows"], "type": "object"} });
    _recordCall('triage');
    const bucket = 'fix';
    if (bucket === 'handback') {
      _handedBack.push({ row: batchId, type: 'needs-judgment', reason: 'triage verdict UNCLEAR' });
      return;
    }
    _admitted.push({ id: batchId, pending: DOWNSTREAM_SEQUENCE[bucket].slice(), widened: false, verifyRetried: false, done: false });
    return;
  }
}

const _queue = [['P0:b0', 'fix'], ['P0:b1', 'fix']];

const _admitted = [];

const _handedBack = [];

const _settled = [];

while (true) {

  // dispatch_ready_downstream: downstream work always wins over a
  // new triage admission (§ Design § Composer).
  const _ready = _admitted.find((b) => !b.done && b.pending.length);
  if (_ready) { await _runDownstream(_ready); continue; }

  // check_ceilings: budget and max_agent_calls, before any new admit.
  if (_queue.length === 0) break;
  if (MAX_AGENT_CALLS !== null && _callCount >= MAX_AGENT_CALLS) { _exhausted = true; break; }
  const _spentDelta = budget.spent() - _startSpent;
  if (BUDGET_TOKENS !== null && _spentDelta + RESERVE > BUDGET_TOKENS) { _exhausted = true; break; }
  if (budget.total !== undefined && budget.total !== null && (budget.remaining() ?? 0) <= RESERVE) { _exhausted = true; break; }

  // admit_next_triage: only reached once no admitted batch has a
  // ready downstream stage, and both ceilings above passed.
  const _nextId = _queue.shift();
  await _runTriage(_nextId);

  if (_admitted.every((b) => b.done) && _queue.length === 0) break;

}

if (_exhausted) {
  for (const _qEntry of _queue) _handedBack.push({ row: _qEntry[0], type: 'budget-exhausted', reason: 'admission ceiling reached' });
  for (const _b of _admitted) if (!_b.done) _handedBack.push({ row: _b.id, type: 'budget-exhausted', reason: 'admission ceiling reached' });
}

const HANDBACK = {
  schema: 'queue-grind-handback/1',
  profile: 'fixture',
  appetite: 'standard',
  handed_back: _handedBack,
  settled: _settled,
  counts: {},
  spend: {
    output_tokens: budget.spent() - _startSpent,
    agent_calls_total: _callCount,
    agent_calls_by_stage_kind: _agentCallsByStageKind,
  },
};

return HANDBACK;
