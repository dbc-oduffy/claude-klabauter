"""
coordinator_core.ops._workflow_patterns — the four house-pattern scaffold templates.

Purpose: pure-data module holding the four named fleet Workflow orchestration
shapes (pipeline-default, disk-poll-fanout, adversarial-verify, loop-until-dry)
as commented fill-in skeleton strings, sourced from the harness Workflow tool
contract. `workflow.scaffold` (C3) composes an emitted script by embedding the
chosen entry's template text as a commented block inside a conformant
meta/phase/agent scaffold — this module supplies ONLY the templates, never the
composition (that's `workflow_scaffold.py`) and never the correctness-contract
checks (that's `_workflow_contract.py`, C1's SSOT). Imported by
`workflow_scaffold.py` ONLY — `_workflow_contract.py` does not need the
templates to validate a script.

Spec backlink: pln-workflow-skeleton-stamper-maki-adab0d § C3

Negative-spec:
  - Does NOT implement or register any op — pure data (a dict literal), no
    I/O, no register_op call.
  - Does NOT validate anything — the templates are illustrative fill-in
    skeletons, not executable scripts; `workflow.validate` checks the SCAFFOLD
    OUTPUT (meta/phase/agent scaffold + this template embedded as a comment),
    not this module's raw strings.
  - Does NOT duplicate the correctness contract — every agent() call shown
    here carries active `model: 'sonnet'` and a `phase:` value purely as
    illustration of the house shape; the actual conformance guarantee comes
    from `workflow_scaffold.py`'s composed meta/phase() scaffold, not from
    this module.
"""

from __future__ import annotations

HOUSE_PATTERNS = {
    "pipeline-default": (
        "// use when: multi-stage per-item work; the DEFAULT — pipeline by "
        "default, no barrier between stages\n"
        "// const DIMENSIONS = [ /* { key, prompt } */ ]\n"
        "// const results = await pipeline(\n"
        "//   DIMENSIONS,\n"
        "//   d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', model: 'sonnet', schema: FINDINGS_SCHEMA }),\n"
        "//   review => parallel(review.findings.map(f => () =>\n"
        "//     agent(`Verify: ${f.title}`, { label: `verify:${f.file}`, phase: 'Verify', model: 'sonnet', schema: VERDICT_SCHEMA })\n"
        "//       .then(v => ({ ...f, verdict: v })))),\n"
        "// )\n"
        "// return { confirmed: results.flat().filter(Boolean) }\n"
    ),
    "disk-poll-fanout": (
        "// use when: N independent work items, each one small agent; "
        "parallel() barrier collects them\n"
        "// const ITEMS = [ /* work items */ ]\n"
        "// const out = await parallel(ITEMS.map((item, i) => () =>\n"
        "//   agent(`Process ${item}`, { label: `work:${i}`, phase: 'Process', model: 'sonnet', schema: RESULT_SCHEMA })))\n"
        "// return out.filter(Boolean)\n"
    ),
    "adversarial-verify": (
        "// use when: a finding/claim must survive independent skeptics "
        "before you trust it\n"
        "// const votes = await parallel(Array.from({ length: 3 }, (_, i) =>\n"
        "//   () => agent(`Try to refute this claim: ${claim}. Default to refuted=true if uncertain.`,\n"
        "//     { label: `refute:${i}`, phase: 'Verify', model: 'sonnet', schema: VERDICT_SCHEMA })))\n"
        "// const survives = votes.filter(Boolean).filter(v => !v.refuted).length >= 2\n"
    ),
    "loop-until-dry": (
        "// use when: unknown-size discovery — keep finding until K "
        "consecutive rounds surface nothing new\n"
        "// const seen = new Set(); const confirmed = []; let dry = 0\n"
        "// while (dry < 2) {\n"
        "//   const found = (await parallel(FINDERS.map(f => () =>\n"
        "//     agent(f.prompt, { phase: 'Find', model: 'sonnet', schema: FINDINGS_SCHEMA }))))\n"
        "//     .filter(Boolean).flatMap(r => r.findings)\n"
        "//   const fresh = found.filter(x => !seen.has(keyOf(x)))\n"
        "//   if (!fresh.length) { dry++; continue }\n"
        "//   dry = 0; fresh.forEach(x => seen.add(keyOf(x)))\n"
        "//   confirmed.push(...fresh)\n"
        "// }\n"
        "// return confirmed\n"
    ),
}
