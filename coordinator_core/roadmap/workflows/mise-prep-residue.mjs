// Parameterized mise-prep residue workflow. Runs inside the Workflow runner, which
// executes this body directly and permits a top-level `return`; `node --check`
// reports `Illegal return statement` for that, which is not a defect.
// Residue stage agent: coordinator:residue-author.
// Bound by `workflow.bind_args` with the `mise-prep-residue-args/4` object: it
// reads only the `args` global and never declares it. Agent types and models
// come from args; no model literal belongs in this file.

export const meta = {
  name: 'mise-prep-residue',
  description: 'Residue authoring for mise-prep: one coordinator:residue-author agent per batch (mise-prep-residue-args/4) of plans, then one APM agent over leftover census rows and seam-fix cycles.',
  phases: ['Residue', 'APM', 'Persist', 'Land'],
};

const RESIDUE_CHARTER = [
  'You are the mise-prep residue author. For each plan below, make it pass the gate command and nothing more.',
  'Allowed edits: declare `capabilities:` in the plan frontmatter; add `appends:` on rows a writes-collision seam line (inline or read from a findings file) names append-only; rewrite each census entry listed as needing a rewrite into the read-only form `coordinator_core/roadmap/census.py` screen accepts (a plain read-only command; a script that is a tracked file).',
  'A census entry no read-only command can carry (it needs node, npx, gh, claude, or python -c with no tracked script) is not yours to decide: do not rewrite or delete it; return it in `leftoverCensus` with `index`, `command` and `reason`.',
  'Never add `ui_carve_out`, change `status`, write any `mise_prepped_*` key, or edit the body beyond the named classes and spine repairs above. Scope decisions are APM\'s, not yours; put anything that needs one in `escalate`.',
  'A member with `spine-repair:` lines: apply exactly the named repair on the cited row and nothing beyond it; a census entry marked step0 moves into the owning row\'s step 0 with the census renumbered, never deleted.',
  'After editing a plan, run the gate command on it and report only what it said.',
  'You do not stage or commit anything.',
].join('\n');

const APM_CHARTER = [
  'You are the APM. Rule on each item below: an unscreenable census entry no read-only rewrite can carry, or a seam-fix cycle that could not be ordered.',
  'Set `item` to the given item `id` verbatim. Set `pmOnly` true only for an item that is important, urgent and has no clear right answer, which only the PM can decide.',
  'You rule only; a later stage records each ruling on its plan. You do not stage or commit anything.',
].join('\n');

function planBlock(p) {
  const lines = ['PLAN ' + p.plan];
  for (const l of (p.bar || [])) lines.push('  bar: ' + l);
  for (const l of (p.spine || [])) lines.push('  ' + l);
  for (const l of (p.seam || [])) lines.push('  seam: ' + l);
  if (p.seamRef) {
    lines.push('  seam: ' + p.seamRefLines + ' writes-collision line(s) in ' + p.seamRef + ' -- read that file; each line is one seam finding');
  }
  for (const c of (p.census || [])) {
    if (c.class === 'step0') {
      lines.push('  census[' + c.index + '] moves into its row\'s step 0: ' + c.command + '  (refused: ' + c.refusal + ')');
    } else {
      lines.push('  census[' + c.index + '] needs a rewrite: ' + c.command + '  (refused: ' + c.refusal + ')');
    }
  }
  return lines.join('\n');
}

await phase('Residue');

const batchResults = await Promise.all((args.batches || []).map((batch) => agent(
  'Repo root: ' + args.repoRoot + ' -- `cd` there first and resolve every relative path against it.\n'
  + 'Gate command (run per plan after editing): ' + args.gateCommand + '\n\n'
  + RESIDUE_CHARTER + '\n\n'
  + 'Batch ' + batch.id + ':\n\n'
  + batch.plans.map(planBlock).join('\n\n'),
  {
    label: 'residue-' + batch.id,
    phase: 'Residue',
    agentType: args.residue.agentType,
    model: args.residue.model,
    schema: {
      type: 'object',
      properties: {
        plans: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              plan: { type: 'string' },
              declared: { type: 'array', items: { type: 'string' } },
              rewrote: { type: 'array', items: { type: 'integer' } },
              leftoverCensus: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    index: { type: 'integer' },
                    command: { type: 'string' },
                    reason: { type: 'string' },
                  },
                  required: ['index', 'command', 'reason'],
                },
              },
              escalate: { type: 'array', items: { type: 'string' } },
            },
            required: ['plan', 'declared', 'rewrote', 'leftoverCensus', 'escalate'],
          },
        },
      },
      required: ['plans'],
    },
  },
)));

const returnedLeftovers = [];
for (const res of batchResults) {
  for (const p of ((res && res.plans) || [])) {
    for (const c of (p.leftoverCensus || [])) {
      returnedLeftovers.push({
        kind: 'census-leftover',
        id: 'census[' + c.index + ']',
        plan: p.plan,
        index: c.index,
        question: c.reason,
        command: c.command,
        refusal: c.reason,
      });
    }
  }
}

const apmItems = (args.apmItems || []).concat(returnedLeftovers);

let rulings = [];
if (apmItems.length > 0) {
  await phase('APM');
  const apmResult = await agent(
    'Repo root: ' + args.repoRoot + ' -- `cd` there first and resolve every relative path against it.\n\n'
    + APM_CHARTER + '\n\n'
    + 'Items (JSON):\n' + JSON.stringify(apmItems, null, 2),
    {
      label: 'apm-rulings',
      phase: 'APM',
      agentType: args.apm.agentType,
      model: args.apm.model,
      schema: {
        type: 'object',
        properties: {
          rulings: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                item: { type: 'string' },
                ruling: { type: 'string' },
                plan: { type: 'string' },
                pmOnly: { type: 'boolean' },
              },
              required: ['item', 'ruling', 'plan', 'pmOnly'],
            },
          },
        },
        required: ['rulings'],
      },
    },
  );
  rulings = (apmResult && apmResult.rulings) || [];
}

const itemIds = new Set(apmItems.map((i) => i.id));
const unmatched = rulings.filter((r) => !itemIds.has(r.item));
rulings = rulings.filter((r) => itemIds.has(r.item));

let persisted = [];
let receiptApplied = [];
if (rulings.length > 0) {
  await phase('Persist');
  // The op reads `id`/`item_kind`/`subjectSha`/`cycle_plans`; the APM returns only `item`.
  const byId = new Map(apmItems.map((i) => [i.id, i]));
  const opRulings = rulings.map((r) => {
    const it = byId.get(r.item);
    return {
      id: r.item,
      item_kind: it.kind,
      plan: it.kind === 'census-leftover' ? it.plan : r.plan,
      ruling: r.ruling,
      pmOnly: r.pmOnly,
      subjectSha: it.subjectSha || '',
      cycle_plans: it.plans || [],
    };
  });
  const payloadPath = args.repoRoot + '/scratch/mise-rulings-' + args.runId + '.json';
  const receipt = await agent(
    'Repo root: ' + args.repoRoot + ' -- `cd` there first.\n'
    + 'Step 1: with your Write tool (no shell heredoc), write this exact JSON to `' + payloadPath + '`:\n'
    + JSON.stringify({ run_id: args.runId, rulings: opRulings }, null, 2) + '\n'
    + 'Step 2: run this command, appending the path from step 1 as its last argument, and nothing else:\n'
    + args.rulingsCommand + ' ' + payloadPath + '\n'
    + 'Return the command\'s JSON reply verbatim as `applied` and `refused`. You run nothing else and do not stage or commit anything.',
    {
      label: 'persist-rulings',
      phase: 'Persist',
      agentType: args.residue.agentType,
      model: args.residue.model,
      schema: {
        type: 'object',
        properties: {
          applied: { type: 'array' },
          refused: { type: 'array' },
        },
        required: ['applied', 'refused'],
      },
    },
  );
  // The op's `applied` rows are `{plan, id}`.
  receiptApplied = (receipt && receipt.applied) || [];
  persisted = receiptApplied.map((a) => (typeof a === 'string' ? a : a && a.id));
  const ruled = rulings.map((r) => r.item);
  const missing = ruled.filter((id) => !persisted.includes(id));
  const extra = persisted.filter((id) => !ruled.includes(id));
  if (missing.length > 0 || extra.length > 0) {
    throw new Error('mise-prep-residue: rulings not persisted; missing ids: [' + missing.join(', ') + ']; unexpected ids: [' + extra.join(', ') + ']');
  }
}

// Landing union: plans the residue stage edited, plans the rulings op rewrote, and the seam notes
// the launcher wrote. Nothing else.
const landPaths = [];
const addLandPath = (p) => { if (typeof p === 'string' && p.length > 0 && !landPaths.includes(p)) landPaths.push(p); };
for (const res of batchResults) {
  for (const p of ((res && res.plans) || [])) {
    if ((p.declared || []).length > 0 || (p.rewrote || []).length > 0) addLandPath(p.plan);
  }
}
for (const a of receiptApplied) addLandPath(a && a.plan);
for (const batch of (args.batches || [])) {
  for (const p of (batch.plans || [])) addLandPath(p.seamRef);
}

let landed = null;
if (landPaths.length > 0) {
  await phase('Land');
  const landParams = JSON.stringify({ paths: landPaths, message: 'mise-prep residue: land plans and seam notes (run ' + args.runId + ')' }).replace(/'/g, "'\\''");
  landed = await agent(
    'Repo root: ' + args.repoRoot + ' -- `cd` there first.\n'
    + 'Run this one command as its own Bash call, verbatim, and nothing else (the inline JSON is what the commit guard reads; no params file):\n'
    + '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" ceremony.commit_v2 \'' + landParams + '\' --repo ' + args.repoRoot + '\n'
    + 'Return `committed`, `sha`, `error` and `refused` from its JSON reply (null where absent). You stage and run nothing else.',
    {
      label: 'land-residue',
      phase: 'Land',
      agentType: 'coordinator:git-commit-agent',
      model: args.residue.model,
      schema: {
        type: 'object',
        properties: {
          committed: { type: 'boolean' },
          sha: { type: ['string', 'null'] },
          error: { type: ['string', 'null'] },
          refused: {},
        },
        required: ['committed'],
      },
    },
  );
  if (!landed || landed.committed !== true) {
    throw new Error('land-residue did not commit: ' + JSON.stringify(landed));
  }
}

return {
  landPaths,
  landed,
  batches: batchResults,
  rulings,
  persisted,
  unmatched,
  pmOnly: rulings.filter((r) => r.pmOnly),
};
