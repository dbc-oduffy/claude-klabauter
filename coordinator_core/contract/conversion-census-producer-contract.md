# coordinator → conversion-census row-shape contract (DRAFT)

> **What this is.** The row-shape contract for example-doctrine-repo's **conversion census** — the
> per-step classification record produced when converting a skill into a computed skill. It
> defines the fields a conforming census row carries (step identity, classification, and —
> when the step is `MIXED` — the forced mechanical/judgment split) so that claude-klabauter's
> compute-layer scaffolder (`coordinator_core/ops/compute_layer_scaffold/`) can read a
> conforming census and know what to generate versus stub, **without further co-design on the
> claude-klabauter side**. Example-doctrine-repo *classifies and produces rows*; claude-klabauter *reads rows and scaffolds*.
>
> **Who consumes this.** A context-less example-doctrine-repo EM building the census procedure/tooling,
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
> **Status: DRAFT — pending example-doctrine-repo's read of the fields.** This has NOT gone through the
> co-design round-trip the FROZEN exemplars in this directory have. It ships now so
> example-doctrine-repo can build against a concrete shape rather than waiting on a second round-trip;
> the counter-proposal is that the scaffolder's own output contract is already pinned by the
> decision-object shape, and this row shape is the one genuinely open surface. **This moves to
> FROZEN once example-doctrine-repo confirms the field set and `MIXED`-split rule against their actual
> classification procedure** (a cross-repo memo reply is the expected confirmation mechanism,
> mirroring `commit-trailer-producer-contract.md`'s freeze-gate pattern). Until then, treat
> every field here as negotiable on example-doctrine-repo's say-so, not claude-klabauter's.
>
> **Changelog:**
> - **2026-08-13 (initial authoring, DRAFT):** authored as the deliverable discharging
>   claude-klabauter's counter-proposal reply to
>   `cross-repo/inbox/2026-08-13-example-doctrine-repo-em-computed-conversion-vehicle.md`. Source:
>   `docs/plans/2026-08-13-compute-layer-scaffolder.md`, chunk C3.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-08-13-compute-layer-scaffolder.md`
> - Source memo (example-doctrine-repo's proposal): `cross-repo/inbox/2026-08-13-example-doctrine-repo-em-computed-conversion-vehicle.md`
> - Machine-checkable shape: `coordinator_core/contract/conversion-census.schema.json`

---

## 0. Contract summary (read this first)

Example-doctrine-repo's conversion procedure classifies each step of converting a skill into a computed
skill. A conforming census is a list of rows; each row carries, per step: the step's identity
(`step_id`), its `classification` (`MECHANICAL` | `JUDGMENT` | `MIXED`), and — only when
`MIXED` — a forced split into `mechanical_part` and `judgment_part`.

A scaffolder reading a conforming census can decide, per step: generate it in full
(`MECHANICAL`), stub it for the author (`JUDGMENT`), or generate the mechanical fraction and
stub the judgment fraction (`MIXED`). This contract fixes that read contract; it says nothing
about how example-doctrine-repo arrives at a classification.

**Two roles:**
- **example-doctrine-repo** — runs the conversion procedure, classifies each step, produces census rows.
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
| `mechanical_part` | string | conditional | The generatable portion of a `MIXED` step. Required, and only meaningful, when `classification` is `MIXED` — see § 2.2. |
| `judgment_part` | string | conditional | The author-authored portion of a `MIXED` step. Required, and only meaningful, when `classification` is `MIXED` — see § 2.2. |
| `notes` | string | no | Optional free-text rationale. Not consumed structurally by the scaffolder. |

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

---

## 2. Grain and split semantics

### 2.1 `step_id` is scoped to one skill's conversion, not a global registry

`step_id` identifies a step *within one skill's conversion procedure*. It is not asserted to
be unique across different skills' censuses, and this contract does not define a cross-skill
join key. A scaffolder reading a census reads it as the ordered/keyed step set for the one
skill under conversion.

### 2.2 `MIXED` is a forced split, not an optional detail

When `classification` is `MIXED`, the row MUST carry both `mechanical_part` and
`judgment_part` — this is not an elaboration example-doctrine-repo may choose to omit. The split exists
so the scaffolder has an unambiguous generate-vs-stub boundary within one step: it generates
`mechanical_part`'s content and stubs `judgment_part`'s content, rather than treating the
whole step as one opaque unit.

A row with `classification: MECHANICAL` or `classification: JUDGMENT` MUST NOT carry
`mechanical_part` or `judgment_part` — those fields are meaningless outside the `MIXED` case
and their presence would signal a split that isn't there.

---

## 3. Producer precision guarantee

**A `MIXED` row without both split fields is not a valid row.** This contract has no
"omit-when-unsure" posture for the split fields the way the commit-trailer contract does for
optional keys (§ 3 of that contract) — `mechanical_part`/`judgment_part` are conditionally
**required**, not conditionally omitted. If example-doctrine-repo's procedure cannot yet name both halves
of a `MIXED` step, the step is not yet ready to be classified `MIXED`.

---

## 4. Worked example

```json
[
  {
    "step_id": "extract-verb-set",
    "classification": "MECHANICAL",
    "notes": "Verb set is read directly off the skill's frontmatter; no author input needed."
  },
  {
    "step_id": "author-judgment-boundary",
    "classification": "JUDGMENT",
    "notes": "Deciding which decisions stay human-owned in the computed form requires domain judgment."
  },
  {
    "step_id": "compose-dispatch-table",
    "classification": "MIXED",
    "mechanical_part": "Emit the closed dict literal mapping each declared verb to its handler stub.",
    "judgment_part": "Author fills in each handler's business logic; the scaffolder cannot infer it.",
    "notes": "Dispatch shape is generatable; handler bodies are not."
  }
]
```

Row 1 is `MECHANICAL`: the scaffolder generates it wholesale. Row 2 is `JUDGMENT`: the
scaffolder stubs it entirely. Row 3 is `MIXED`: the scaffolder generates the dispatch table
shape (`mechanical_part`) and stubs the handler bodies (`judgment_part`).

---

## 5. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part
of this contract** and are **example-doctrine-repo's own decisions**:

- **The conversion procedure itself** — what counts as a "step," how a step is judged
  `MECHANICAL` vs `JUDGMENT` vs `MIXED`, and any rubric or heuristic behind that judgment.
  example-doctrine-repo's own design surface.
- **The tooling that produces census rows** — how a census file is assembled, generated,
  reviewed, or revised. Example-doctrine-repo's own build.
- **Census file delivery/storage** — the format the census is packaged in, its filename, its
  location, or how it reaches claude-klabauter. This contract fixes the shape of one row; it does not
  fix a file format or a transport.
- **The compute-layer scaffolder's emit/check implementation** — how the scaffolder actually
  composes generated module text or scores conformance. See
  `docs/plans/2026-08-13-compute-layer-scaffolder.md` C1/C2; not this contract.
- **Cross-skill or cross-census identity** — whether two censuses' `step_id` values relate to
  each other. Out of scope per § 2.1.

---

## 6. Path to FROZEN

This contract ships as **DRAFT**, not FROZEN, unlike the other producer contracts in this
directory. It moves to **FROZEN** once example-doctrine-repo confirms, via cross-repo memo reply, that:

1. The field set (`step_id`, `classification`, `mechanical_part`, `judgment_part`, `notes`)
   matches what their classification procedure can actually produce.
2. The `MIXED`-forced-split rule (§ 2.2, § 3) is workable against their procedure — i.e. their
   tooling can always name both halves of a step it classifies `MIXED`.

Until that confirmation lands, this file and its schema are subject to change on example-doctrine-repo's
say-so without triggering the reader-widen-before-writer-flips bump protocol the FROZEN
exemplars in this directory use — that protocol applies only after freeze.

---

<!-- producer-contract: example-doctrine-repo conversion-census row shape. DRAFT (2026-08-13), pending
     example-doctrine-repo confirmation of the field set and MIXED-split rule. Discharges
     docs/plans/2026-08-13-compute-layer-scaffolder.md C3 / AC11. -->
