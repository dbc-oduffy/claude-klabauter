# plan.readiness_verdict — consumer-facing contract

> **What this is.** The statement of the reply `plan.readiness_verdict` returns, the roster `tiers`
> field the execute-review compose reads, and the folded judge fields. **Source of truth for the
> reply:** `coordinator_core/contract/readiness-verdict.schema.json`. This note restates; the
> schema wins.

## 1. Producer and consumers

- **Producer:** the engine op `plan.readiness_verdict` (`coordinator_core/ops/plan_readiness_verdict.py`).
  COMPUTE_ONLY: no spawn, no git. It computes the prep gate in-process through
  `roadmap.prep_gate.gate_plan`; there is no skip or force parameter.
- **Consumers:** the `ask_compose` revise block (applies `mechanical_repairs` in one targeted
  revise, no second `planBlitz` round); `plan-blitz.mjs` once the DoE run wires it.

## 2. Params

`{plan, coverage?, reviews?}`. `coverage` is `{verdict, mechanicalFindings, judgmentFindings}`;
`reviews` is `[{reviewer, verdict}]`. Both are the structured values plan-blitz already holds.

## 3. Reply

| Field | Meaning |
|---|---|
| `plan` | repo-relative plan path |
| `verdict` | `ready`, `pulled` or `judgment` |
| `pull_class` | `none`, `mechanical` or `substantive` |
| `mechanical_repairs` | `[{kind, target, instruction}]`; empty unless `pull_class` is `mechanical` |
| `prep_gate` | the `plan.prep_gate` reply verbatim |
| `reason` | one line |

- `ready` with empty `mechanical_repairs`: no prep-gate or readiness agent is owed.
- `pulled` + `mechanical`: apply the repairs in one revise; re-run the op, not the review round.
- `pulled` + `substantive`, or a post-revise verdict that is not `ready`: the full `_reblitz` path.
- `judgment`: a reviewer verdict or a judgment finding needs an agent.

## 4. Roster `tiers`

Optional per-agent array in `execute_review` stages; values `lightweight | standard | full`.
**Absent means every tier.** claude-klabauter reads the field and mints no size-to-reviewer table; the
roster is DoE-owned. `coordinator:code-reviewer` runs at every tier, so a wave is never empty.

## 5. Folded judge fields

The exit-criterion judge's schema, widened by `execute_review._widen_judge_schema`, also carries
the ledger-check `items`, `all_of_pm_ask` and `conflicts`. The script's `_ledger` variable keeps
its shape, so `dispatch.terminal_commit`'s `ledger_verdict` param is unchanged.
