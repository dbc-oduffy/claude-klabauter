export const meta = {
  name: "structured-research-2026-10-03-fixture",
  description: "Structured research on 3 subject(s), run sequentially: scout, verifiers, synthesizer",
  phases: ['Scout', 'Verify', 'Rebuttal', 'Synthesize'],
};

const MAX_CONCURRENT = 5;
const subjects = [
 {
  "subject": "Subject One",
  "scratch": "<scratch_root>/2026-10-03-fixture-subject-one-workdir",
  "outputPath": "out/Subject One.json",
  "scoutPrompt": "Scout Subject One. Context: Subject key: Subject One. Write findings under <scratch_root>/2026-10-03-fixture-subject-one-workdir.\n",
  "verifiers": [
   {
    "role": "verifier-alpha",
    "topic": "alpha",
    "prompt": "Verify topic Alpha topic (id alpha) for Subject One. Scratch: <scratch_root>/2026-10-03-fixture-subject-one-workdir.\n"
   },
   {
    "role": "verifier-beta",
    "topic": "beta",
    "prompt": "Verify topic Beta topic (id beta) for Subject One. Scratch: <scratch_root>/2026-10-03-fixture-subject-one-workdir.\n"
   }
  ],
  "synthPrompt": "Synthesize Subject One into out/Subject One.json for run 2026-10-03-fixture on 2026-10-03. Scratch: <scratch_root>/2026-10-03-fixture-subject-one-workdir.\n"
 },
 {
  "subject": "Subject Two",
  "scratch": "<scratch_root>/2026-10-03-fixture-subject-two-workdir",
  "outputPath": "out/Subject Two.json",
  "scoutPrompt": "Scout Subject Two. Context: Subject key: Subject Two. Write findings under <scratch_root>/2026-10-03-fixture-subject-two-workdir.\n",
  "verifiers": [
   {
    "role": "verifier-alpha",
    "topic": "alpha",
    "prompt": "Verify topic Alpha topic (id alpha) for Subject Two. Scratch: <scratch_root>/2026-10-03-fixture-subject-two-workdir.\n"
   },
   {
    "role": "verifier-beta",
    "topic": "beta",
    "prompt": "Verify topic Beta topic (id beta) for Subject Two. Scratch: <scratch_root>/2026-10-03-fixture-subject-two-workdir.\n"
   }
  ],
  "synthPrompt": "Synthesize Subject Two into out/Subject Two.json for run 2026-10-03-fixture on 2026-10-03. Scratch: <scratch_root>/2026-10-03-fixture-subject-two-workdir.\n"
 },
 {
  "subject": "Subject Three",
  "scratch": "<scratch_root>/2026-10-03-fixture-subject-three-workdir",
  "outputPath": "out/Subject Three.json",
  "scoutPrompt": "Scout Subject Three. Context: Subject key: Subject Three. Write findings under <scratch_root>/2026-10-03-fixture-subject-three-workdir.\n",
  "verifiers": [
   {
    "role": "verifier-alpha",
    "topic": "alpha",
    "prompt": "Verify topic Alpha topic (id alpha) for Subject Three. Scratch: <scratch_root>/2026-10-03-fixture-subject-three-workdir.\n"
   },
   {
    "role": "verifier-beta",
    "topic": "beta",
    "prompt": "Verify topic Beta topic (id beta) for Subject Three. Scratch: <scratch_root>/2026-10-03-fixture-subject-three-workdir.\n"
   }
  ],
  "synthPrompt": "Synthesize Subject Three into out/Subject Three.json for run 2026-10-03-fixture on 2026-10-03. Scratch: <scratch_root>/2026-10-03-fixture-subject-three-workdir.\n"
 }
];

const verifierReturn = { type: 'object', required: ['topic', 'challenged'],
  properties: { topic: { type: 'string' }, challenged: { type: 'array', items: { type: 'string' } } } };

const rebuttalBrief = (s, v, prior) => `You are the continuation of ${v.role}. Your inbox is ${s.scratch}/mail/${v.role}.jsonl; your prior output is ${s.scratch}/${v.topic}-findings.md; your predecessor returned: ${JSON.stringify(prior)}. Answer every challenge with evidence or concede, revise ${v.topic}-findings.md (unanswered challenges become CONTESTED with both sides' evidence), append {"read": true} to your inbox, and return the same JSON shape.`;

const synthPrompt = (s, returns) => `${s.synthPrompt}

Verifier return values:
${JSON.stringify(returns)}`;

async function inChunks(items, run) {
  const out = [];
  for (let i = 0; i < items.length; i += MAX_CONCURRENT) {
    out.push(...await parallel(items.slice(i, i + MAX_CONCURRENT).map((item, j) => () => run(item, i + j))));
  }
  return out;
}

const results = [];
for (const s of subjects) {
  try {
    phase('Scout');
    await agent(s.scoutPrompt, { label: `${s.subject}:scout`, agentType: 'coordinator:research-scout', model: 'haiku' });

    phase('Verify');
    const round1 = await inChunks(s.verifiers, (v) =>
      agent(v.prompt, { label: `${s.subject}:${v.role}`, agentType: 'coordinator:research-specialist', model: 'sonnet', schema: verifierReturn }));

    phase('Rebuttal');
    const challenged = new Set(round1.flatMap((r) => (r && r.challenged) || []));
    const round2 = await inChunks(s.verifiers, (v, i) => challenged.has(v.topic)
      ? agent(rebuttalBrief(s, v, round1[i]), { label: `${s.subject}:${v.role}-rebuttal`, agentType: 'coordinator:research-specialist', model: 'sonnet', schema: verifierReturn })
      : Promise.resolve(round1[i]));

    phase('Synthesize');
    const result = await agent(synthPrompt(s, round2), { label: `${s.subject}:synthesizer`, agentType: 'coordinator:structured-synthesizer', model: 'opus' });
    results.push({ subject: s.subject, outputPath: s.outputPath, result });
  } catch (err) {
    results.push({ subject: s.subject, outputPath: s.outputPath, error: String(err && err.message || err) });
  }
}
return results;
