---
title: "Authoring constraints handed to plan-author before drafting (lever b1)"
created: 2026-10-25
author: claude-klabauter-7a (5b137c1a-f32c-4c7b-a9a6-d233bcea62b4)
status: executing
branch: "work/machine-b/2026-10-06"
plan_id: "pln-authoring-constraints-handed-t-874ab8"
deliverable_id: "dlv-the-engine-hands-plan-author-the-prep-d03039"
initiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative
sizing_object: "state/sizings/2026-10-25-07e966-3-29-b1-authoring-constraints.yaml"
pm_brief: {source: baton-originating-ask, ref: "state/handoffs/2026-10-25-07e966-3-29-b1-authoring-constraints.md"}
register_claims:
  sizing: "state/sizings/2026-10-25-07e966-3-29-b1-authoring-constraints.yaml"
  rows: ["R1", "R2"]
  pins: {"R1": "sha256:de395c2f755835208309bd6a419f48a34df079cbd7dca7354ba53418f3f9f197", "R2": "sha256:570d6d82e7b13ee6d2086b706966f0d22676f24d092c36ffa2404d2a7e1b6ad6"}
prime_exit_criterion:
  statement: >-
    The engine emits id/gate/rule/source/satisfy constraints to plan-author before drafting, honours plan frontmatter constraint_waived, and a cf544a-style plan is not pulled for register-rows-unclaimed or anchor-premises-undeclared; tests covering touched files pass.
  derived_from: "state/sizings/2026-10-25-07e966-3-29-b1-authoring-constraints.yaml"
census:
  - question: "How many engine python files spell anchor-premises-undeclared?"
    command: "grep -rl anchor-premises-undeclared coordinator_core --include=*.py | wc -l"
    result: "4 (ops/_anchor_premise.py plus three tests)"
    count: 4
  - question: "How many engine python files spell register-rows-unclaimed?"
    command: "grep -rl register-rows-unclaimed coordinator_core --include=*.py | wc -l"
    result: "3 (roadmap/prep_gate.py, ops/dispatch_emit/requirements_ledger.py, one test)"
    count: 3
  - question: "Does any constraint_waived reader exist yet?"
    command: "grep -rl constraint_waived coordinator_core | wc -l"
    result: "0 -- no emitter or waiver reader exists"
    count: 0
  - question: "How many report classes does prep_gate enumerate in CLASS_ORDER?"
    command: "grep -A4 '^CLASS_ORDER' coordinator_core/roadmap/prep_gate.py | grep -o '\"[A-Z_]*\"' | wc -l"
    result: "12 (the sizing premise says 13; the tree says 12)"
    count: 12
anchor_premises:
  - id: P1
    claim: "plan.schema.json's top level is not additionalProperties false, so constraint_waived is readable before DoE declares it."
    check:
      command: "grep -c '^  \"additionalProperties\": false' coordinator_core/frontmatter/schemas/plan.schema.json"
      expect: "0"
  - id: P2
    claim: "ask_compose composes the plan-author prompt under _PLAN_AUTHOR_AGENT_TYPE."
    check:
      command: "grep -c _PLAN_AUTHOR_AGENT_TYPE coordinator_core/ops/dispatch_emit/ask_compose.py"
      expect: ">=1"
capabilities: []
external_gate: []
review_signals: []
gated_exit_criteria:
  - brightline: work-proportionate-to-question
    statement: >-
      Five rows and one new module, nine files (four source, five tests), within the L sizing's scope floor (scope_files=7).
    met: false
  - brightline: multi-os-first-class
    statement: >-
      Pure Python with no shell-out and no path separator assumed. Tests write only under tmp_path.
    met: false
  - brightline: no-single-machine-assumptions
    statement: >-
      Constraint `source` values are repo-relative strings. No absolute path or user name is introduced.
    met: false
  - brightline: work-vs-question-ratio
    statement: >-
      The constraints list answers "which rules will the gate apply to this plan". The waiver
      reader answers "which of those did the author waive, and why". Nothing is corpus-scale.
    met: false
  - brightline: right-not-merely-working
    statement: >-
      The delivered code is efficient and elegant — right, not merely working.
      Rejected: a hand-written prose list of rules in the prompt, which drifts from the gate.
      Delivered: one registry that both the gate and the prompt read, so the emitted rule cannot
      diverge from the enforced one.
    met: false
grouping_approvals:
  do:
    status: pending
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# Authoring constraints handed to plan-author before drafting (lever b1)

## PM brief

> The engine hands plan-author the prep-gate and readiness rules as authoring constraints before it drafts (lever b1): emit {id, gate, rule, source, satisfy, declared_empty_form?} for each rule; plan frontmatter constraint_waived: [{id, reason}] waives one; an absent field means plan-author proceeds. Test: a cf544a-style plan is no longer pulled for register-rows-unclaimed or anchor-premises-undeclared.
>
> - Estimate: L
> - Interaction mode: ceo
> - Exit criterion: The engine emits id/gate/rule/source/satisfy constraints to plan-author before drafting, honours plan frontmatter constraint_waived, and a cf544a-style plan is not pulled for register-rows-unclaimed or anchor-premises-undeclared; tests covering touched files pass.
> - Sizing: state/sizings/2026-10-25-07e966-3-29-b1-authoring-constraints.yaml

## Problem

plan-author learns the prep-gate and readiness rules only after it drafts, when a gate pulls the
plan. Two rules are named in the brief:

- `register-rows-unclaimed`, emitted by `_register` inside `coordinator_core/roadmap/prep_gate.py :: evaluate_plan`.
- `anchor-premises-undeclared`, emitted by `coordinator_core/ops/_anchor_premise.py :: parse_anchors` and read by `coordinator_core/ops/plan_readiness_verdict.py`.

The cf544a plan (`docs/plans/2026-10-25-durable-objective-wave-checkin-reacher-gate.md`) is the
reference case. Nothing gives the author these rules before it drafts:
`grep -rl constraint_waived coordinator_core` finds 0 files.

This plan adds:

- One registry of authoring constraints, each of the form `{id, gate, rule, source, satisfy, declared_empty_form?}`.
- The registry rendered into the plan-author prompt that `ask_compose.py` composes before drafting.
- Support in both gates for a plan's `constraint_waived: [{id, reason}]`.

A plan without the field gets the gates exactly as they behave today.

File scope:

- `coordinator_core/roadmap/authoring_constraints.py` (new)
- `coordinator_core/roadmap/prep_gate.py`
- `coordinator_core/ops/plan_readiness_verdict.py`
- `coordinator_core/ops/dispatch_emit/ask_compose.py`

Tests touched (one new file per row):

- `coordinator_core/roadmap/tests/test_authoring_constraints.py`
- `coordinator_core/roadmap/tests/test_prep_gate_constraint_waived.py`
- `coordinator_core/ops/tests/test_plan_readiness_constraint_waived.py`
- `coordinator_core/ops/dispatch_emit/tests/test_ask_compose_authoring_constraints.py`
- `coordinator_core/ops/tests/test_replay_cf544a_authoring_constraints.py`

## Anti-scope

- **The schema and the doctrine belong to DoE.** Declaring `constraint_waived` in `plan.schema.json` and changing the plan-author agent or skill doctrine are DoE's half. R2 says "DoE owns the plan-author doctrine half", and the seam is pinned on DoE run ask-20261025T230248-e26211 Q2. This plan does not edit the vendored schema. The engine can still read the field because the schema's top level is open (anchor P1).
- **A waiver never passes silently.** A waived constraint is reported as `waived: <id> (<reason>)` in the class detail. A waiver with no reason is a defect.
- **`register-rows-unclaimed` cannot be waived through `constraint_waived`.** Its `satisfy` text points to the register's own deferred/waived row ruling, and `ask.stage` still refuses unclaimed rows (docs/wiki/requirement-register.md). A waiver that names it is reported as `constraint-not-waivable`.
- **The engine-composed prompt is the only carrier.** The constraints reach plan-author through that prompt and through no other document.
- **The constraints stay engine code**, never a prose copy that drifts from the gate kinds.
- **No subprocess from the prompt composer.** Re-running the gate as a subprocess would cost a spawn, and the brightline is 500ms.

## Out of scope

- Making gate kinds outside the registry honour waivers. Every registry entry is emitted, but only the waivable ones are honoured.
- Re-gating plans already on disk.

## Tasks

```yaml plan-tasks
- id: C1
  title: "Interface: authoring-constraints registry and constraint_waived reader"
  change_kind: script-edit
  surface: coordinator_core/roadmap/authoring_constraints.py
  writes: [coordinator_core/roadmap/authoring_constraints.py, coordinator_core/roadmap/tests/test_authoring_constraints.py]
  traces_to_brief: "emit {id, gate, rule, source, satisfy, declared_empty_form?} for each rule"
  register_rows: [R1]
  queue_scope: project
  disposition: open
  body: |
    Wave 0. Every other row builds on this one.
    When done, the module exposes three things:
    1. The constraint list. Each entry carries:
       - id: the gate's defect kind
       - gate: the prep_gate class, or READINESS
       - rule
       - source: enclosing function plus repo-relative path
       - satisfy
       - declared_empty_form, wherever the gate accepts an empty
         declaration (for example `anchor_premises: []`, `census: []`,
         `capabilities: []`)
       - whether the constraint is waivable
    2. A reader that maps a plan's `constraint_waived` to {id: reason}.
       It reports malformed, reasonless, unknown and non-waivable entries.
    3. A prompt renderer that turns the list into prompt text.
    The list covers at least register-rows-unclaimed,
    register-claims-missing, anchor-premises-undeclared,
    capabilities-undeclared, the census form and the prime-exit
    requirement.
    The test pins that every id is a kind the gate code actually emits,
    by checking the source literals.
    No subprocess. No I/O at import.
- id: C2
  title: "prep_gate honours constraint_waived"
  change_kind: script-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: [coordinator_core/roadmap/prep_gate.py, coordinator_core/roadmap/tests/test_prep_gate_constraint_waived.py]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: imports the C1 registry and reader
  traces_to_brief: "plan frontmatter constraint_waived: [{id, reason}] waives one"
  register_rows: [R1]
  queue_scope: project
  disposition: open
  body: |
    When done, in evaluate_plan:
    - A defect becomes a pass when its kind is a waivable constraint id
      and the plan waives it with a reason. The pass detail names the
      waiver.
    - A reasonless, unknown or non-waivable waiver is a defect.
    - REGISTER's register-rows-unclaimed stays unwaivable.
    - A plan with no `constraint_waived` gets byte-identical reports to
      today. The test pins this on an existing fixture.
- id: C3
  title: "Readiness verdict honours constraint_waived for anchor premises"
  change_kind: script-edit
  surface: coordinator_core/ops/plan_readiness_verdict.py
  writes: [coordinator_core/ops/plan_readiness_verdict.py, coordinator_core/ops/tests/test_plan_readiness_constraint_waived.py]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: imports the C1 reader
  traces_to_brief: "an absent field means plan-author proceeds"
  register_rows: [R1]
  queue_scope: project
  disposition: open
  body: |
    When done:
    - When the plan waives anchor-premises-undeclared with a reason, the
      finding from _anchor_premise.parse_anchors is dropped and a waiver
      note is recorded. parse_anchors returns that defect as a prose
      string beginning "anchor-premises-undeclared (", not a bare kind,
      so match the id as the string's leading token.
    - An absent constraint_waived leaves the verdict unchanged.
    - _anchor_premise.py is not edited. The waiver is applied at the
      verdict's call site.
- id: C4
  title: "Plan-author prompt carries the constraints before drafting"
  change_kind: script-edit
  surface: coordinator_core/ops/dispatch_emit/ask_compose.py
  writes: [coordinator_core/ops/dispatch_emit/ask_compose.py, coordinator_core/ops/dispatch_emit/tests/test_ask_compose_authoring_constraints.py]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: renders the C1 registry into the prompt
  traces_to_brief: "The engine hands plan-author the prep-gate and readiness rules as authoring constraints before it drafts"
  register_rows: [R1, R2]
  queue_scope: project
  disposition: open
  body: |
    When done:
    - The plan_author, ledger-replan and plan-revise prompts dispatched
      under _PLAN_AUTHOR_AGENT_TYPE carry the rendered constraint block,
      including the one-line constraint_waived form.
    - The block is rendered in process at compose time.
    - The test asserts that every registry id appears in the composed
      plan-author prompt.
    - If a golden or snapshot test pins the composed workflow text, the
      executor updates that golden as well and names it.
- id: C5
  title: "Replay: a cf544a-style plan is not pulled"
  change_kind: script-edit
  surface: coordinator_core/ops/tests/test_replay_cf544a_authoring_constraints.py
  writes: [coordinator_core/ops/tests/test_replay_cf544a_authoring_constraints.py]
  depends_on:
    - chunk: C2
      gate_kind: output-consumption-runtime
      note: exercises the waiver-aware prep gate
    - chunk: C3
      gate_kind: output-consumption-runtime
      note: exercises the waiver-aware readiness verdict
  traces_to_brief: "Test: a cf544a-style plan is no longer pulled for register-rows-unclaimed or anchor-premises-undeclared"
  register_rows: [R1]
  acceptance: "python -m pytest coordinator_core/ops/tests/test_replay_cf544a_authoring_constraints.py -q  # a cf544a-style plan is no longer pulled for register-rows-unclaimed or anchor-premises-undeclared"
  queue_scope: project
  disposition: open
  body: |
    Build a cf544a-shaped fixture under tmp_path: sizing, register and
    plan frontmatter modelled on
    docs/plans/2026-10-25-durable-objective-wave-checkin-reacher-gate.md.
    When done, the test shows:
    - Authored to each constraint's satisfy or declared_empty_form, the
      fixture passes REGISTER and is not pulled for
      anchor-premises-undeclared.
    - With those declarations removed, the same fixture is pulled. This
      proves the test bites.
    - With anchor-premises-undeclared waived with a reason, the fixture
      is not pulled.
    - A waiver of register-rows-unclaimed is refused.
```

## Width rationale

- C1 is the interface.
- C2, C3 and C4 then run in parallel on disjoint files.
- C5 is the replay.

The critical path C1 -> C2 -> C5 is 3 of 5 rows, which is over the 1/3 guide. The interface and the
end-to-end replay cannot be folded into the parallel middle without making the middle rows depend
on each other.

## Exit criteria — verification

The plan is finished when, and only when:

1. `python -m pytest coordinator_core/roadmap/tests/test_authoring_constraints.py coordinator_core/roadmap/tests/test_prep_gate_constraint_waived.py coordinator_core/ops/tests/test_plan_readiness_constraint_waived.py coordinator_core/ops/dispatch_emit/tests/test_ask_compose_authoring_constraints.py coordinator_core/ops/tests/test_replay_cf544a_authoring_constraints.py coordinator_core/roadmap/tests/test_prep_gate_register.py coordinator_core/ops/tests/test_plan_readiness_verdict.py` is green.
2. Acceptance oracle: C5's replay test. The fixture is pulled without the declarations and passes with them.
3. The gated_exit_criteria rows carry close-out evidence and are flipped to met: true through the op plan.gated_criteria_met.

The repo's fast tier and full suite are **never** a criterion for this plan — not the prime exit
criterion, not a gated exit criterion, not a chunk's test surface, and not a plan deliverable.
They are EM-owned, run only at the wave boundary or cadence gate, and running either proves
nothing about this plan.
