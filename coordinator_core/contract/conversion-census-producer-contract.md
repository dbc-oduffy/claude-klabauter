# coordinator → conversion-census row-shape contract (DRAFT)

> **What this is.** The row-shape contract for DoE-claude's **conversion census** — the
> per-step classification record produced when converting a skill into a computed skill. It
> defines the fields a conforming census row carries (step identity, classification, and —
> when the step is `MIXED` — the forced mechanical/judgment split) so that claude-klabauter's
> compute-layer scaffolder (`coordinator_core/ops/compute_layer_scaffold/`) can read a
> conforming census and know what to generate versus stub, **without further co-design on the
> claude-klabauter side**. DoE-claude *classifies and produces rows*; claude-klabauter *reads rows and scaffolds*.
>
> **Who consumes this.** A context-less DoE-claude EM building the census procedure/tooling,
> and claude-klabauter's own compute-layer scaffolder implementation. Everything needed to emit a
> conforming row is in this file: the field table, the `classification` enum, the `MIXED`
> split rule, and the worked example. The machine-checkable shape is
> `coordinator_core/contract/conversion-census.schema.json`.
>
> **What this is NOT.** This is the *row shape* — not the census procedure, not the tooling
> that produces rows, and not the compute-layer scaffolder's own emitter/check-mode
> implementation. It does not specify how a step is judged `MECHANICAL` vs `JUDGMENT` vs
> `MIXED`, nor how a census file is assembled, stored, or delivered to claude-klabauter. See § "Out of
> scope — not our surface".
>
> **Status: DRAFT — one confirmation short of FROZEN.** The co-design round-trip the FROZEN
> exemplars in this directory have has now *happened*: DoE-claude read the fields and replied
> (twice, the second correcting the first). The `MIXED`-split rule is confirmed workable, and the
> corrected field set is implemented here. What is still owed is DoE-claude's read of the
> **implemented shape** rather than of the memo that requested it — two shaping calls inside it
> were delegated to claude-klabauter (§ 2.3, § 2.4) and only they can say the result is what they meant.
> **§ 6 states the exact one-line confirmation that closes the gate.** Until it lands, treat every
> field here as negotiable on DoE-claude's say-so, not claude-klabauter's — but note that the DRAFT
> exemption is claude-klabauter-local and does not reach the artifact-shape-contract bundle, which vendors
> this row shape and takes a MAJOR from this revision regardless (§ 7).
>
> **Changelog:**
> - **2026-08-13 (initial authoring, DRAFT):** authored as the deliverable discharging
>   claude-klabauter's counter-proposal reply to
>   `cross-repo/inbox/2026-08-13-doe-claude-em-computed-conversion-vehicle.md`. Source:
>   `docs/plans/2026-08-13-compute-layer-scaffolder.md`, chunk C3.
> - **2026-08-14 (field-set revision, still DRAFT):** DoE-claude's § 6 confirmation arrived as
>   two memos — `cross-repo/inbox/2026-08-13-doe-claude-em-census-field-set.md`, superseded
>   within the hour by `...-census-field-set-corrected.md`. Both § 6 conditions are met: the
>   `MIXED`-split rule is confirmed workable (their doctrine changes, not this schema), and the
>   field set is confirmed *with corrections*, applied here. Five changes: `mechanical_part` and
>   `judgment_part` promoted from strings to objects so per-half fields key to the half they
>   describe (§ 2.3); `judgment_kind` added as a named enum rather than the originally-requested
>   ordinal `judgment_tier`; `round_trip` and `revalidate_at_dispatch` added as two orthogonal
>   fields rather than one lossy enum; `candidate_op` added, optional; and the
>   `additionalProperties: false` question answered with a single `x_producer` extension object
>   (§ 2.4). The freeze gate stays open pending DoE-claude's read of the *implemented* shape —
>   see § 6.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-08-13-compute-layer-scaffolder.md`
> - Source memo (DoE-claude's proposal): `cross-repo/inbox/2026-08-13-doe-claude-em-computed-conversion-vehicle.md`
> - Machine-checkable shape: `coordinator_core/contract/conversion-census.schema.json`

---

## 0. Contract summary (read this first)

DoE-claude's conversion procedure classifies each step of converting a skill into a computed
skill. A conforming census is a list of rows; each row carries, per step: the step's identity
(`step_id`), its `classification` (`MECHANICAL` | `JUDGMENT` | `MIXED`), the fields that decide
*what* gets generated (`candidate_op` on the mechanical side; `judgment_kind`, `round_trip`,
`revalidate_at_dispatch` on the judgment side), and — only when `MIXED` — a forced split into
`mechanical_part` and `judgment_part` objects that carry those same fields per half.

A scaffolder reading a conforming census can decide, per step: generate it in full
(`MECHANICAL`), stub it for the author (`JUDGMENT`), or generate the mechanical fraction and
stub the judgment fraction (`MIXED`) — and in every case knows *which* construct to emit rather
than guessing. This contract fixes that read contract; it says nothing about how DoE-claude
arrives at a classification.

**Two roles:**
- **DoE-claude** — runs the conversion procedure, classifies each step, produces census rows.
  Owns the procedure and its tooling.
- **claude-klabauter** — reads a conforming census; the compute-layer scaffolder consumes rows
  to decide generate-vs-stub per step. Owns this row-shape contract and the scaffolder that
  reads it.

---

## 1. Row-shape schema

### 1.1 Field table

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `step_id` | string | **yes** | Stable identity of the conversion step this row classifies, scoped to one skill's conversion procedure. Not a cross-skill join key — see § 2. |
| `classification` | enum | **yes** | `MECHANICAL` \| `JUDGMENT` \| `MIXED` — see § 1.2. |
| `candidate_op` | string | no | Name of the existing `coordinator_core` capability this step becomes. Only on a `MECHANICAL` row, forbidden elsewhere. Optional because it is sometimes genuinely unknown at census time; when present it is the difference between generating a `directives[]` entry and stubbing one. |
| `judgment_kind` | enum | conditional | `advisory` \| `untrusted_gate` — which judgment-point constructor to emit (§ 1.3). **Required** on a `JUDGMENT` row, forbidden elsewhere. |
| `round_trip` | enum | conditional | `terminal` \| `round_trip` — whether this judgment point gates downstream mechanical recomputation (§ 1.4). **Required** on a `JUDGMENT` row, forbidden elsewhere. |
| `revalidate_at_dispatch` | boolean | no | Default `false`. Whether the entry's evidence is freshness-sensitive. Orthogonal to `round_trip`, not a value of it (§ 1.4). `JUDGMENT` rows only. |
| `mechanical_part` | object | conditional | The generatable half of a `MIXED` step: `{ summary, candidate_op? }`. Required, and only meaningful, when `classification` is `MIXED` — see § 2.2, § 2.3. |
| `judgment_part` | object | conditional | The author-authored half of a `MIXED` step: `{ summary, judgment_kind, round_trip, revalidate_at_dispatch? }`. Required, and only meaningful, when `classification` is `MIXED` — see § 2.2, § 2.3. |
| `notes` | string | no | Optional free-text rationale. Not consumed structurally by the scaffolder. |
| `x_producer` | object | no | Producer-owned extension namespace, open inside. Never read by the scaffolder — see § 2.4. |

The three per-half fields (`candidate_op`, `judgment_kind`, `round_trip`, plus
`revalidate_at_dispatch`) appear **either** flat on a single-classification row **or** inside a
part object on a `MIXED` row — never both. § 2.3 says why.

### 1.2 `classification` enum

```
MECHANICAL | JUDGMENT | MIXED
```

- **`MECHANICAL`** — the step is fully generatable by the scaffolder from declared inputs; no
  author judgment required.
- **`JUDGMENT`** — the step requires author judgment in full; the scaffolder emits a stub for
  the author to fill in, generating nothing structural for this step.
- **`MIXED`** — the step forcibly splits: some fraction is generatable, some fraction requires
  author judgment. A `MIXED` row MUST also carry `mechanical_part` and `judgment_part` (§ 2.2).

### 1.3 `judgment_kind` — which constructor, not which tier

```
advisory | untrusted_gate
```

- **`advisory`** — built through `contract/decision_object/judgment.build_judgment_point`, whose
  `recommendation` parameter is **required with no default**.
- **`untrusted_gate`** — built through `build_untrusted_gate_judgment_point`, which **forbids** a
  recommendation.

Without this field the scaffolder must guess the constructor, and guessing wrong produces a
`TypeError` at runtime for the first case or a doctrine violation for the second. It is named
rather than numbered on purpose: the discriminator is *"may the engine offer a recommendation at
all?"* — a security-class boolean, not an ordinal. A `2|3` encoding invites `>=` comparisons with
no defined meaning, and collides with DoE-claude's own three-tier sort in which tier 1 is
`MECHANICAL`. The tier numbers remain useful documentation; they are not wire values.

### 1.4 `round_trip` and `revalidate_at_dispatch` are two fields, not one

```
round_trip:             terminal | round_trip
revalidate_at_dispatch: boolean (default false)
```

`round_trip` decides whether the emitted CLI is single-shot or genuinely two-phase — a
whole-module shape decision. `revalidate_at_dispatch` decides whether the entry's evidence is
freshness-sensitive. They are orthogonal, and collapsing them loses a real state:
`round_trip: round_trip` with `revalidate_at_dispatch: false` is an entry that gates downstream
recomputation but whose evidence does not go stale. That state is representable and meaningful in
the decision-object schema the scaffolder generates against, so a census that cannot express it
does not merely lose detail — it silently mis-generates. Both are spelled as DoE-claude's own wiki
spells them (`coordinator/docs/wiki/computed-skills.md` § round-trip classification).

---

## 2. Grain and split semantics

### 2.1 `step_id` is scoped to one skill's conversion, not a global registry

`step_id` identifies a step *within one skill's conversion procedure*. It is not asserted to
be unique across different skills' censuses, and this contract does not define a cross-skill
join key. A scaffolder reading a census reads it as the ordered/keyed step set for the one
skill under conversion.

### 2.2 `MIXED` is a forced split, not an optional detail

When `classification` is `MIXED`, the row MUST carry both `mechanical_part` and
`judgment_part` — this is not an elaboration DoE-claude may choose to omit. The split exists
so the scaffolder has an unambiguous generate-vs-stub boundary within one step: it generates
`mechanical_part`'s content and stubs `judgment_part`'s content, rather than treating the
whole step as one opaque unit.

A row with `classification: MECHANICAL` or `classification: JUDGMENT` MUST NOT carry
`mechanical_part` or `judgment_part` — those fields are meaningless outside the `MIXED` case
and their presence would signal a split that isn't there.

### 2.3 The per-half fields key to the half, and `classification` stays an explicit discriminator

Two decisions here, and they pull in opposite directions on purpose.

**The parts are objects, not strings.** A `MIXED` row is two half-steps. A `candidate_op` or a
`judgment_kind` keyed to the row's single `classification` cannot say *which half* it names — and
since `MIXED` is none of `MECHANICAL`/`JUDGMENT`, a field gated on those values never fires on a
`MIXED` row at all. The generation-relevant fields would therefore be absent from exactly the rows
whose ambiguity they exist to resolve, leaving the scaffolder guessing the judgment-point
constructor for every `MIXED` row: the defect the fields were requested to close, reintroduced by
where they were attached. Promoting the halves to objects and moving the fields inside them is the
fix.

**`classification` stays, rather than being derived from which halves are present.** The more
uniform alternative — every row carries an optional `mechanical` object and an optional `judgment`
object, require at least one, derive the classification — makes `MIXED` structural instead of a
magic enum value and collapses the conditionals in § 3 into a single `minProperties`-style rule.
It is genuinely the cleaner shape, and it is rejected for one reason: it converts a **loud
contradiction into a silent reclassification**. Under the derived form, a producer that classifies
a step `JUDGMENT` and attaches a stray `mechanical` object has simply emitted a `MIXED` row, and
nothing anywhere says otherwise. Under the explicit form the same bug fails validation with the
mismatch named. The redundancy between `classification` and the part objects is not duplication —
it is a checksum, and the checksum is the whole reason to keep it.

### 2.4 `additionalProperties` — the row stays closed, `x_producer` is the one open room

The producer needs to carry its own per-row fields: the locus in the `SKILL.md`, structured
classification rationale, evidence citation. Those are the producer's and this schema should not
model them. Three ways to allow it were on the table; this contract takes the first.

- **A single `x_producer` object, open inside — adopted.** The row itself stays
  `additionalProperties: false`, so a misspelling of a field this schema *does* model
  (`judgement_part`) still fails loudly. Inside `x_producer`, anything goes, and the producer
  needs no claude-klabauter change to add a field.
- **Dropping the whole row to `additionalProperties: true` — rejected.** It buys the same
  flexibility by disabling the typo detection that is most of what a closed row is *for*. A
  misspelled required field would be silently absorbed as an extra property rather than reported.
- **A parallel file keyed by `step_id` — rejected.** The drift concern raised against it is
  correct, not overweighted: a second artifact that can disagree with the first is precisely the
  defect this contract exists to end, and forcing the producer into one makes the wrong path the
  cheap path.

The name is `x_producer`, not a vendor name: it is scoped to the **producer role** this contract
defines in § 0, so the field does not carry the current occupant's identity into a contract that
outlives it.

---

## 3. Producer precision guarantee

**A `MIXED` row without both split fields is not a valid row.** This contract has no
"omit-when-unsure" posture for the split fields the way the commit-trailer contract does for
optional keys (§ 3 of that contract) — `mechanical_part`/`judgment_part` are conditionally
**required**, not conditionally omitted. If DoE-claude's procedure cannot yet name both halves
of a `MIXED` step, the step is not yet ready to be classified `MIXED`.

---

## 4. Worked example

```json
[
  {
    "step_id": "extract-verb-set",
    "classification": "MECHANICAL",
    "candidate_op": "contract.verb_set.extract",
    "notes": "Verb set is read directly off the skill's frontmatter; no author input needed."
  },
  {
    "step_id": "author-judgment-boundary",
    "classification": "JUDGMENT",
    "judgment_kind": "advisory",
    "round_trip": "terminal",
    "notes": "Deciding which decisions stay human-owned in the computed form requires domain judgment.",
    "x_producer": {
      "locus": "SKILL.md § How You Decide",
      "evidence": ["computed-skills.md § Generalizing this pattern, step 4"]
    }
  },
  {
    "step_id": "confirm-untrusted-probe",
    "classification": "JUDGMENT",
    "judgment_kind": "untrusted_gate",
    "round_trip": "round_trip",
    "revalidate_at_dispatch": false,
    "notes": "Operator confirmation of an arbitrary probe command — the engine may not recommend. Gates downstream recomputation, but the confirmation itself does not go stale."
  },
  {
    "step_id": "compose-dispatch-table",
    "classification": "MIXED",
    "mechanical_part": {
      "summary": "Emit the closed dict literal mapping each declared verb to its handler stub.",
      "candidate_op": "contract.dispatch_table.emit"
    },
    "judgment_part": {
      "summary": "Author fills in each handler's business logic; the scaffolder cannot infer it.",
      "judgment_kind": "advisory",
      "round_trip": "terminal"
    },
    "notes": "Dispatch shape is generatable; handler bodies are not."
  }
]
```

Row 1 is `MECHANICAL`: the scaffolder generates it wholesale, targeting `candidate_op`. Row 2 is
`JUDGMENT`: the scaffolder stubs it entirely, emitting a `build_judgment_point` call because
`judgment_kind` is `advisory`. Row 3 is also `JUDGMENT` but emits
`build_untrusted_gate_judgment_point` instead, and shows the state a single collapsed enum could
not express — `round_trip: round_trip` alongside `revalidate_at_dispatch: false` (§ 1.4). Row 4 is
`MIXED`: the scaffolder generates the dispatch table shape from `mechanical_part` and stubs the
handler bodies from `judgment_part`, reading the constructor off the *half* rather than the row
(§ 2.3).

---

## 5. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part
of this contract** and are **DoE-claude's own decisions**:

- **The conversion procedure itself** — what counts as a "step," how a step is judged
  `MECHANICAL` vs `JUDGMENT` vs `MIXED`, and any rubric or heuristic behind that judgment.
  DoE-claude's own design surface.
- **The tooling that produces census rows** — how a census file is assembled, generated,
  reviewed, or revised. DoE-claude's own build.
- **Census file delivery/storage** — the format the census is packaged in, its filename, its
  location, or how it reaches claude-klabauter. This contract fixes the shape of one row; it does not
  fix a file format or a transport. **DoE-claude has taken this half up**: the document envelope
  `{ schema_version, skill, source_path, source_sha, unit, taken_at, round_trip_shape, rows }`
  is theirs, authored as `coordinator/schemas/census-document.schema.json` in their tree and
  registered into the artifact-shape-contract bundle at `7.0.0`. It `$ref`s this row shape rather
  than redefining it. That is where the module-level facts belong — `skill`, the round-trip shape,
  and the `unit` question — instead of being replicated onto every row where nothing could enforce
  their consistency.
- **The `unit` a census counts** — step, fenced block, or block-id. Out of scope here and settled
  on the envelope above. `step` is the unit this row shape is written for.
- **The compute-layer scaffolder's emit/check implementation** — how the scaffolder actually
  composes generated module text or scores conformance. See
  `docs/plans/2026-08-13-compute-layer-scaffolder.md` C1/C2; not this contract.
- **Cross-skill or cross-census identity** — whether two censuses' `step_id` values relate to
  each other. Out of scope per § 2.1.

---

## 6. Path to FROZEN

This contract ships as **DRAFT**, not FROZEN, unlike the other producer contracts in this
directory. It moves to **FROZEN** once DoE-claude confirms, via cross-repo memo reply, that:

1. The field set matches what their classification procedure can actually produce.
2. The `MIXED`-forced-split rule (§ 2.2, § 3) is workable against their procedure — i.e. their
   tooling can always name both halves of a step it classifies `MIXED`.

**Status as of 2026-08-14: condition 2 is confirmed; condition 1 is confirmed-with-corrections
and the corrections are applied, but the gate stays open for one more round-trip.**

Condition 2 is closed outright — DoE-claude confirmed § 2.2 and § 3 workable and is restating
their own "zero MIXED may remain" doctrine as "no MIXED row may remain *unsplit*", with an
invariant checker enforcing exactly this contract's conditional.

Condition 1 is deliberately **not** self-certified. DoE-claude's corrected memo specified a field
set; this revision implements it, with two shaping decisions that were theirs to delegate and are
therefore theirs to check — the explicit-`classification` choice over the derived alternative they
leaned toward (§ 2.3), and `x_producer` over the other two escape hatches (§ 2.4). Reading their
memo as a confirmation of the shape *this file now contains* would be confirming a paraphrase:
they described fields, claude-klabauter built a schema, and only they can say the schema is what they meant.
**The freeze needs one line back — "the implemented shape matches" — against this revision, not
against the memo that requested it.**

Until that confirmation lands, this file and its schema are subject to change on DoE-claude's
say-so without triggering the reader-widen-before-writer-flips bump protocol the FROZEN
exemplars in this directory use — that protocol applies only after freeze.

**That DRAFT exemption is claude-klabauter-local and does not reach the bundle.** Because DoE-claude's
census-document envelope vendors this row shape into the artifact-shape-contract bundle, a change
here changes their bundle body whether or not this contract has frozen. The bundle's own bump rule
has no DRAFT carve-out — see § 7.

---

## 7. Bump consequence of this revision — the vendored row shape makes it MAJOR

`conversion-census-row` is a top-level `$def` in the artifact-shape-contract bundle at `7.0.0`,
hoisted out of census-document's own `$defs`. This revision therefore changes the bundle body, and
the class of that change is **not** the additive MINOR both repos were predicting:

- **Additive**, and MINOR on their own: `judgment_kind`, `round_trip`, `revalidate_at_dispatch`,
  `candidate_op`, and `x_producer` are all new optional properties; the `x_producer` door is a
  widening.
- **Non-additive**, and the piece that sets the class: `mechanical_part` and `judgment_part` change
  **type**, string → object. A consumer holding `7.x` semantics accepts `"mechanical_part": "some
  string"`, which this shape rejects. That is the same harm class as an enum narrow.

By the emitter's bump rule — *any* non-additive change bumps MAJOR regardless of the additive
majority around it — the next bundle stamp carrying this shape is **`8.0.0`**, not `7.1.0`.

The softening that was available and was rejected: an `anyOf` accepting either a string or an
object would keep the change additive. It would also make the per-half fields optional in
practice, which is exactly the ambiguity § 2.3 exists to remove — the scaffolder would be back to
guessing on any row that took the string arm. Paying a MAJOR is the cheaper of the two.

---

<!-- producer-contract: DoE-claude conversion-census row shape. DRAFT (2026-08-13, field-set
     revision 2026-08-14), pending DoE-claude confirmation of the IMPLEMENTED shape — the
     MIXED-split rule is already confirmed and the corrected field set is applied; § 6 names the
     one line that closes the gate. Discharges
     docs/plans/2026-08-13-compute-layer-scaffolder.md C3 / AC11. -->
