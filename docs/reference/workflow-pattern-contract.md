# Workflow pattern contract — what a conformant fleet Workflow looks like

> **This document is the readable face, not a second source.** The
> machine-readable correctness contract lives in
> [`coordinator_core/ops/_workflow_contract.py`](../../coordinator_core/ops/_workflow_contract.py)
> (the check registry — scrubber, forbidden globals, `meta` rules, phase
> set-diff, barrier/model-default heuristics) and the four scaffold templates
> live in [`coordinator_core/ops/_workflow_patterns.py`](../../coordinator_core/ops/_workflow_patterns.py).
> If this doc and those modules ever disagree, the modules win — file a fix
> against this doc, not the other way round.

This page describes the shape a fleet `.mjs` Workflow script needs in order to
pass `workflow.validate` cleanly (or, if you're starting from scratch, the
shape `workflow.scaffold` will hand you). It exists so an EM can reason about
"is this conformant?" without reading regex.

## The meta pure-literal rule (ERROR)

Every Workflow script must export a top-level `meta` object as a **pure
object literal**:

```js
export const meta = {
  name: 'my-workflow',
  description: 'what this workflow does',
  phases: ['Review', 'Verify'],
};
```

"Pure literal" means: no `${...}` interpolation, no backtick template
literals, no spread (`...`), no function/method calls, and no bare-identifier
values (`name: someVar` is impure — inline the literal instead). The harness
evaluates `meta` at **parse time**, before any of your script's runtime code
has executed — so anything that requires evaluation (a call, a variable
reference, string interpolation) throws before the workflow ever starts. This
is why it's ERROR-tier, not advisory: an impure `meta` block is not a style
problem, it's a script that cannot boot.

`meta` must also declare the two required fields, `name` and `description`;
missing either is also an ERROR.

## `phase()` vs `meta.phases` (WARN, both surfaces)

Two independent places in a script can name a phase:

1. **The `phase()` call site** — `phase('Review')` — a standalone statement
   that opens a named progress group.
2. **The agent-options `phase:` field** — `agent(prompt, { phase: 'Review', ... })`
   — an option carried on an individual `agent()` (or `parallel()` item)
   call, independent of any `phase()` call elsewhere in the script.

Both surfaces are checked **independently** against the phase titles declared
in `meta.phases`. A title used on either surface but absent from
`meta.phases` is flagged — but only as a **WARN**, never an ERROR: an
unmatched phase title doesn't crash anything, it just falls into its own
separate, ungrouped progress bucket in the harness UI. If you want tidy
grouped progress reporting, keep `meta.phases` in sync with every title you
use on either surface; if you don't, nothing breaks, you just get messier
progress output.

## Forbidden globals (ERROR) — and why

Three calls throw at runtime inside a Workflow script and are ERROR-tier:

| Forbidden | Why |
|---|---|
| `Date.now()` | Unavailable in the Workflow runtime — throws. |
| `Math.random()` | Unavailable in the Workflow runtime — throws. |
| `new Date()` (argless only) | Unavailable — throws. `new Date(someTimestamp)` is fine; only the no-argument form is forbidden. |

The common thread is **resume-safety**. A fleet Workflow can be paused and
resumed — re-entering a script after a crash, a compaction boundary, or a
deliberate suspend. Any call whose result depends on "what wall-clock instant
is it right now" or "what random value do I get this time" is fundamentally
incompatible with deterministic resume: replaying the script on resume would
produce a *different* timestamp or *different* random value than the first
run did, silently diverging state. The harness disallows these globals
outright rather than let that class of bug exist. If you need a timestamp or
a random-ish id, derive it from op output or a parameter passed in — never
from the ambient clock or RNG.

**These checks run against scrubbed text.** The contract module masks the
*contents* of every string, template literal, and comment before running the
forbidden-globals regex, so a prompt template that literally contains the
text `Date.now()` (e.g. as an instructional string shown to an agent) is
never mistaken for real code. Only actual call sites in code trip the check.
The same scrubbed view backs the barrier-vs-pipeline and model-default
heuristics below, for the identical reason — advisory checks about call
*shape* shouldn't fire on string/comment content either.

## Barrier vs. pipeline (WARN)

`pipeline()` runs each item through every stage with **no barrier** between
stages — an item that finishes stage 1 early moves straight into stage 2
while its siblings are still on stage 1. Two sequential `parallel()` calls,
by contrast, force a **full-barrier wait**: every item must finish the first
`parallel()` before any item starts the second.

If a script contains `await parallel(...)`, then some non-agent transform,
then another `await parallel(...)`, with no `agent()` work happening in
between, that's usually a sign the author reached for two barriers when they
meant `pipeline()` — wasting wall-clock waiting on the slowest item at each
stage for no reason. This is a single advisory WARN (not per-occurrence):
a genuine barrier — e.g. dedup or early-exit logic that needs to see *all*
results before deciding whether to continue — is a legitimate use case, so
the check never hard-fails.

## The four house patterns

`workflow.scaffold` composes its output around one of four named
orchestration shapes, defined as commented fill-in templates in
[`_workflow_patterns.py`](../../coordinator_core/ops/_workflow_patterns.py).
Passing no `pattern` to `workflow.scaffold` emits `pipeline-default`.

| pattern | use when |
|---|---|
| `pipeline-default` | Multi-stage per-item work; the DEFAULT — pipeline by default, no barrier between stages. |
| `disk-poll-fanout` | N independent work items, each one small agent; a single `parallel()` barrier collects them. |
| `adversarial-verify` | A finding/claim must survive independent skeptics before you trust it. |
| `loop-until-dry` | Unknown-size discovery — keep finding until K consecutive rounds surface nothing new. |

Every `agent()` call shown in these templates carries an explicit
`model: 'sonnet'` — mechanical fan-out work should default to the cheap
model, not silently inherit the session model (see the model-default
heuristic below). Treat that as the template convention to copy, not a
detail to strip out.

## Model-default heuristic (WARN)

An `agent()` call with no explicit `model:` key inherits the **session**
model — Opus, if you're running on an Opus session. For a mechanical fan-out
(the common case: N small identical-shaped agent calls processing a list),
that's a roughly 4x cost defect. The check can't textually tell a mechanical
fan-out apart from a genuine judgment call that should legitimately inherit
Opus, so it's advisory: pin `model: 'sonnet'` for fan-outs, and leave the
inherited default alone when the call really is a judgment agent.
`workflow.scaffold`'s emitted output is model-default-WARN-clean by
construction — every `agent()` call it emits already carries an active
`model: 'sonnet'`.
