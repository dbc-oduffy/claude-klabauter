// Parameterized mise-prep residue workflow. Runs inside the Workflow runner, which
// executes this body directly and permits a top-level `return`; `node --check`
// reports `Illegal return statement` for that, which is not a defect.
// Bound by `workflow.bind_args` with the `mise-prep-residue-args/2` object: it
// reads only the `args` global and never declares it. Agent types and models
// come from args; no model literal belongs in this file.

export const meta = {
  name: 'mise-prep-residue',
  description: 'Residue authoring for mise-prep: one coordinator:plan-author-residue agent per batch of plans, then one APM agent over leftover census rows and seam-fix cycles.',
  phases: ['Residue', 'APM', 'Persist'],
};

const RESIDUE_CHARTER = [
  'You are the mise-prep residue author. For each plan below, make it pass the gate command and nothing more.',
  'Allowed edits: declare `capabilities:` in the plan frontmatter; add `appends:` on rows a writes-collision seam line (inline or read from a findings file) names append-only; rewrite each census entry listed as needing a rewrite into the read-only form `coordinator_core/roadmap/census.py` screen accepts (a plain read-only command; a script that is a tracked file).',
  'A census entry no read-only command can carry (it needs node, npx, gh, claude, or python -c with no tracked script) is not yours to decide: do not rewrite or delete it; return it in `leftoverCensus` with `index`, `command` and `reason`.',
  'Never add `ui_carve_out`, change `status`, write any `mise_prepped_*` key, or edit the body beyond the named classes above. Scope decisions are APM\'s, not yours; put anything that needs one in `escalate`.',
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
  for (const l of (p.seam || [])) lines.push('  seam: ' + l);
  if (p.seamRef) {
    lines.push('  seam: ' + p.seamRefLines + ' writes-collision line(s) in ' + p.seamRef + ' -- read that file; each line is one seam finding');
  }
  for (const c of (p.census || [])) {
    lines.push('  census[' + c.index + '] needs a rewrite: ' + c.command + '  (refused: ' + c.refusal + ')');
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
if (rulings.length > 0) {
  await phase('Persist');
  // The op reads `id`/`item_kind`/`subjectSha`/`cycle_plans`; the APM returns only `item`.
  const byId = new Map(apmItems.map((i) => [i.id, i]));
  const opRulings = rulings.map((r) => {
    const it = byId.get(r.item);
    return {
      id: r.item,
      item_kind: it.kind,
      plan: r.plan,
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
  persisted = ((receipt && receipt.applied) || []).map((a) => (typeof a === 'string' ? a : a && a.id));
  const ruled = rulings.map((r) => r.item);
  const missing = ruled.filter((id) => !persisted.includes(id));
  const extra = persisted.filter((id) => !ruled.includes(id));
  if (missing.length > 0 || extra.length > 0) {
    throw new Error('mise-prep-residue: rulings not persisted; missing ids: [' + missing.join(', ') + ']; unexpected ids: [' + extra.join(', ') + ']');
  }
}

return {
  batches: batchResults,
  rulings,
  persisted,
  unmatched,
  pmOnly: rulings.filter((r) => r.pmOnly),
};
