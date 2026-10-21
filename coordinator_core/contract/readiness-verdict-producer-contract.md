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
- **Producer of `premise`:** DoE `premiseAsReview`'s caller in `plan-blitz.mjs`, once the DoE
  change lands. It passes the premise checker's rows as `premise` instead of as a `premise-check`
  reviews entry.

## 2. Params

`{plan, coverage?, reviews?, premise?}`. `coverage` is `{verdict, mechanicalFindings, judgmentFindings}`;
`reviews` is `[{reviewer, verdict, ledger?}]`. These are the structured values plan-blitz already
holds. No other top-level param exists.

**`premise` (optional).** `{rows: [{verdict, citation, questionClass?, evidence?}]}`: the rows of
DoE's `PREMISE_SCHEMA`, passed through unchanged.

- `verdict` is one of `RESOLVES`, `UNRESOLVED`, `CONTRADICTED`, `UNCHECKABLE`. Any other value is
  refused with a ValueError naming the row; the op invents no blocking class.
- Only `UNRESOLVED` and `CONTRADICTED` rows block. Each adds one judgment entry,
  `premise-check: <VERDICT> <citation>` (citation capped at 200 characters), and the verdict is
  `judgment`, as for a non-clear reviewer. When several rows block, `reason` names as many
  citations as fit and ends `+N more premise row(s)`.
- `UNCHECKABLE` and `RESOLVES` rows never add a judgment entry. `UNCHECKABLE` is reviewer judgment,
  not a premise failure: it never makes the verdict `judgment` or pulls the plan. When the verdict
  is `ready`, `reason` appends `; N UNCHECKABLE premise row(s) left to reviewer judgment`, a
  non-blocking note only.
- **Row `baseline_ref` (optional string).** Meaningful on a class-4 (falsifier arming) row only: the
  falsifier `baseline_ref` the checker read against. When present and different from the plan's
  current `prime_exit_criterion.falsifier.baseline_ref`, the row is **stale**: a note, never
  blocking. A row without `baseline_ref` behaves as before; a non-string value is refused.
- **Plan frontmatter `corrects:` (optional input, read by the op from the plan file).** A list of
  `{citation: str, fixed_by: str}`. `citation` is non-empty text that the row's citation contains,
  compared after whitespace normalisation, case-sensitively. `fixed_by` is a spine row id. An entry
  **covers** a `CONTRADICTED` row only when its citation matches and `fixed_by` names a row in the
  plan's spine whose disposition is `open` or `coded`. A malformed entry, an unknown row id, or a
  deferred, spun-off or won't-do row covers nothing. `corrects:` never covers an `UNRESOLVED` row (see `declared_absent:`). A covered
  row is a note (`<n> CONTRADICTED premise row(s) the plan corrects: <citation> -> <row id>`), never
  blocking.
- **Plan frontmatter `declared_absent:` (optional input, read by the op from the plan file).** A
  list of `{citation: str, tracked_by: str}`, both non-empty. An entry **covers** an `UNRESOLVED`
  row when its whitespace-normalised `citation` occurs in the row's citation (case-sensitive, the
  same match as `corrects:`) and `tracked_by` is one of: a repo-relative path that exists under the
  repo root (a filed ask, memo or bug record); an `http(s)://` URL (a filed issue); or text that
  occurs verbatim in the body of the plan's `## Open questions` section. A covered row is a note
  (`<n> UNRESOLVED premise row(s) the plan declares absent: <citation> -> <tracked_by>`), **never in
  `blocking`**. `CONTRADICTED` rows are never covered by `declared_absent` (that is `corrects:`'s
  job), and an entry with an unresolvable `tracked_by`, or a malformed entry, covers nothing. The
  carry that lets a refire skip unchanged citations is in `premise-carry-contract.md`.
- `premise` together with a `reviews[]` entry whose `reviewer` is `premise-check` is refused: the
  rows are passed once.
- An empty `rows` list adds nothing to the verdict (the `premise-check` exclusion still applies). With `premise` absent, the reply is unchanged.

**Clear vocabulary.** A coverage or review verdict reads as clear (case-insensitive) when it is one
of `approve`, `approved`, `pass`, `passed`, `clear`, `clean`, `ready`, `ok`, `accept`, `accepted`,
or `complete`. `COMPLETE` is the coverage check's own clear verdict; A non-clear coverage verdict
(`INCOMPLETE`, `SCOPE-MISMATCH`) with zero findings gives the plan nothing to fix: it adds a note to
a `ready` reply and never pulls. PIVOT, REJECTED, FAILED and any unknown verdict stay `judgment` (fail-closed).

**`reviews[].ledger` (optional).** A repo-relative or absolute path to that reviewer's
findings-ledger sidecar. The op reads it in-process and never stamps or writes it.

- A `WARN` or `BLOCKED` review reads as clear only when its `ledger` names a sidecar that carries a
  `findings_ledger:` frontmatter stamp, every row is `verified: true`, and every row's `status` is
  absent or `applied`. `verify` stamps em-rejected and suspended rows `verified: true` too, so an
  em-rejected or suspended row leaves the review at `judgment`.
- An absent, unreadable, unstamped or partially verified ledger leaves the review at `judgment`.
- `OK`-class verdicts need no ledger. An unknown key inside a review entry other than `ledger`
  changes nothing.
- **Producer of `ledger`:** DoE `readinessStep` in `plan-blitz.mjs`, once the X1 memo lands. Until
  then no caller passes it, and a `WARN`/`BLOCKED` review stays `judgment`.

## 3. Reply

| Field | Meaning |
|---|---|
| `plan` | repo-relative plan path |
| `verdict` | `ready`, `pulled` or `judgment` |
| `pull_class` | `none`, `mechanical` or `substantive` |
| `mechanical_repairs` | `[{kind, target, instruction}]`; empty unless `pull_class` is `mechanical` |
| `prep_gate` | the `plan.prep_gate` reply verbatim |
| `reason` | one line |
| `blocking` | optional; sorted, de-duplicated keys of everything that put the verdict into `judgment` or `pulled` |

**`blocking` key spelling.** Premise: `premise-check: <VERDICT> <citation[:200]>`. Reviewer:
`<reviewer>: '<verdict>'`. Any other entry that puts the verdict into `judgment` or `pulled` (a
substantive prep-gate entry, the coverage judgment-finding count) appears verbatim as the string the
op appends to its `substantive` or `judgment` list. Covered and stale premise rows never appear:
they are notes. `blocking` is built before the `+N more` truncation of `reason` and is not
truncated by it. A caller compares two pulls by comparing their `blocking` sets. Absent on replies
from a producer that predates it; the schema does not require it.

- `ready` with empty `mechanical_repairs`: no prep-gate or readiness agent is owed.
- `pulled` + `mechanical`: apply the repairs in one revise; re-run the op, not the review round.
- `pulled` + `substantive`, or a post-revise verdict that is not `ready`: the full `_reblitz` path.
- `judgment`: a reviewer verdict or a judgment finding needs an agent, including a `WARN`/`BLOCKED`
  review without a fully verified `ledger`.

## 4. Roster `tiers`

Optional per-agent array in `execute_review` stages; values `lightweight | standard | full`.
**Absent means every tier.** claude-klabauter reads the field and mints no size-to-reviewer table; the
roster is DoE-owned. `coordinator:code-reviewer` runs at every tier, so a wave is never empty.

## 5. Folded judge fields

The exit-criterion judge's schema, widened by `execute_review._widen_judge_schema`, also carries
the ledger-check `items`, `all_of_pm_ask` and `conflicts`. The script's `_ledger` variable keeps
its shape, so `dispatch.terminal_commit`'s `ledger_verdict` param is unchanged.
