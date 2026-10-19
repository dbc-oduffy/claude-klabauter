// Parameterized mise-prep residue workflow. Runs inside the Workflow runner, which
// executes this body directly and permits a top-level `return`; `node --check`
// reports `Illegal return statement` for that, which is not a defect.
// Bound by `workflow.bind_args` with the `mise-prep-residue-args/1` object: it
// reads only the `args` global and never declares it. Agent types and models
// come from args; no model literal belongs in this file.

export const meta = {
  name: 'mise-prep-residue',
  description: 'Residue authoring for mise-prep: one residue-author agent per batch of plans, then one APM agent over leftover census rows and seam-fix cycles.',
  phases: ['Residue', 'APM'],
};

const RESIDUE_CHARTER = [
  'You are the mise-prep residue author. For each plan below, make it pass the gate command and nothing more.',
  'Allowed edits: declare `capabilities:` in the plan frontmatter; add `appends:` on rows a writes-collision seam line names append-only; rewrite each census entry listed as needing a rewrite into the read-only form `coordinator_core/roadmap/census.py` screen accepts (a plain read-only command; a script that is a tracked file).',
  'A census entry no read-only command can carry (it needs node, npx, gh, claude, or python -c with no tracked script) is not yours to decide: do not rewrite or delete it; return it in `leftoverCensus` with `index`, `command` and `reason`.',
  'Never add `ui_carve_out`, change `status`, write any `mise_prepped_*` key, or edit the body beyond the named classes above. Scope decisions are APM\'s, not yours; put anything that needs one in `escalate`.',
  'After editing a plan, run the gate command on it and report only what it said.',
  'You do not stage or commit anything.',
].join('\n');

const APM_CHARTER = [
  'You are the APM. Rule on each item below: an unscreenable census entry no read-only rewrite can carry, or a seam-fix cycle that could not be ordered.',
  'Write the ruling to the plan as `pm_ruling:` in its frontmatter, as you do today. Set `pmOnly` true only for an item that is important, urgent and has no clear right answer, which only the PM can decide.',
  'You do not stage or commit anything.',
].join('\n');

function planBlock(p) {
  const lines = ['PLAN ' + p.plan];
  for (const l of (p.bar || [])) lines.push('  bar: ' + l);
  for (const l of (p.seam || [])) lines.push('  seam: ' + l);
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

return {
  batches: batchResults,
  rulings,
  pmOnly: rulings.filter((r) => r.pmOnly),
};
