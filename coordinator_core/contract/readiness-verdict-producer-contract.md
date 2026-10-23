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
- **Row `anchor` (optional string).** Names a plan `anchor_premises[].id`. A non-string value is
  refused with a ValueError naming `premise.rows[i].anchor`. A row carrying `anchor` is judged only
  through the anchor findings below, never as a citation row.
- `premise` together with a `reviews[]` entry whose `reviewer` is `premise-check` is refused: the
  rows are passed once.
- An empty `rows` list adds nothing to the verdict (the `premise-check` exclusion still applies). With `premise` absent, the reply is unchanged.

### Anchor premises

**Plan frontmatter `anchor_premises:` (input, read by the op from the plan file).** A list of
`{id, claim, check: {command, expect}}`: the premises the route rests on (where data lives, which
caller runs, what a path resolves to). `id` and `claim` are non-blank strings, `id` unique.
`check.command` is a census-form command that passes `roadmap.census.screen`
(`census.ALLOWED_FORM`); `check.expect` is non-blank. Absent key: undeclared. `[]`: the plan rests
on no anchor premise, and adds no finding. The engine never runs `command`; the premise-checker
does.

**Findings.** Each is one `judgment` entry and one ruling (`source: premise`), and counts as one
blocking finding:

- `anchor-premises-undeclared`: the key is absent.
- `anchor-premise-unchecked: <id>`: malformed entry, missing or blank check, or a `command` that
  `census.screen` refuses.
- `anchor-premise-no-verdict: <id>`: no row resolves the anchor.
- `anchor-premise-unknown: <id>`: a passed row names an `anchor` no declared entry carries.
- `anchor-premise-<VERDICT>: <id>`: the verdict is `UNRESOLVED`, `CONTRADICTED` or `UNCHECKABLE`.

**Verdict source.** A passed `premise.rows` entry with `anchor: <id>` wins. Otherwise a row in the
plan's premise-carry file (`premise-carry-contract.md` § 2) with `anchor: <id>` counts only while
its `line_hash` is still among the plan's line hashes. A null or stale hash (the anchor line was
edited) is no verdict. Matching is exact id equality, never prose similarity.

**No exemption.** `corrects:` and `declared_absent:` never cover an anchor finding.

**Two rules, one reason.** "Only `UNRESOLVED` and `CONTRADICTED` rows block" holds for citation rows
(approved ruling: `docs/plans/2026-10-15-premise-uncheckable-never-blocks.md`). An anchor row blocks
on any verdict but `RESOLVES`: the route rests on it, so unverifiable means halt or re-plan.
Non-anchor `UNCHECKABLE` rows stay notes.

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
| `blocking_findings` | integer >= 0; uncleared findings that forbid buying a resolver |
| `advisory_findings` | integer >= 0; uncleared findings an agent may resolve |
| `rulings` | `[{id, source, subject, ask}]`; required, `[]` unless `verdict` is `pulled` or `judgment` |
| `split_recommended` | boolean; required; true when the verdict is `pulled` (substantive) or `judgment` and `blocking_findings >= SPLIT_BLOCKING_THRESHOLD` |
| `blocking` | optional; sorted, de-duplicated keys of everything that put the verdict into `judgment` or `pulled` |

**`blocking` key spelling.** Premise: `premise-check: <VERDICT> <citation[:200]>`. Reviewer:
`<reviewer>: '<verdict>'`. Any other entry that puts the verdict into `judgment` or `pulled` (a
substantive prep-gate entry, the coverage judgment-finding count) appears verbatim as the string the
op appends to its `substantive` or `judgment` list. Covered and stale premise rows never appear:
they are notes. `blocking` is built before the `+N more` truncation of `reason` and is not
truncated by it. A caller compares two pulls by comparing their `blocking` sets. Absent on replies
from a producer that predates it; the schema does not require it.

**`blocking_findings` / `advisory_findings`.** Both are required on every reply and are 0 and 0 on
`ready`. A review that is neither clear nor ledger-cleared adds its uncleared ledger rows (rows not
`verified: true`, or whose `status` is present and not `applied`), never fewer than 1 (also 1 when
the ledger is absent, unreadable or has no rows): to `blocking_findings` when its verdict is
`BLOCKED` or unrecognised, to `advisory_findings` when `WARN`. Coverage judgment findings add their
count to `advisory_findings`, or `max(count, 1)` to `blocking_findings` when the coverage verdict
is `BLOCKED-SURFACE-TO-PM`. Each surviving premise row (not covered, declared absent or stale) adds
1 to `blocking_findings`. `len(blocking)` is never the count: `blocking` keys include WARN reviewers
and prep-gate entries, and its meaning is unchanged. A `judgment` `reason` begins
`needs an agent: <b> blocking, <a> advisory: `.

**`rulings` / `split_recommended`.** Each ruling is one decision the EM owes: `id` is `J<n>`
(distinct from the requirements ledger's `R<n>`), numbered substantive, then judgment, then
surviving premise rows. `source` is `prep-gate`, `coverage`, `review` or `premise`; `subject` is the
blocking entry text capped at 300 characters; `ask` is one sentence naming the decision and where
it is discharged (fix the class in the plan; revise or accept a review; a `corrects:` entry for a
CONTRADICTED premise; a `declared_absent:` entry for an UNRESOLVED one). A mechanical pull carries no
rulings and never recommends a split, whatever its count. `SPLIT_BLOCKING_THRESHOLD` (12, module
constant in `plan_readiness_verdict.py`) is the blocking count at which a pull is too large for one
baton.

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
