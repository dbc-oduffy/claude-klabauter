# coordinator → strategic-self-description-draft producer contract (PROPOSED)

> **What this is.** The producer-side contract of the draft artifact that the coordinator
> control-plane engine (**claude-klabauter**) derives and writes via the `strategic.generate` op. It defines
> the inputs-to-fields mapping per derivation track, the draft-artifact consumption seam (path,
> shape, consumer), and the non-clobber invariant that makes generation safe to run against a repo
> that already carries a human-ratified canonical instance.
> claude-klabauter *derives and writes the draft*; the human ceremony (example-doctrine-repo's refresh skill) *reconciles and
> ratifies* the canonical `self-description.yaml`.
>
> **Who consumes this.** example-doctrine-repo's C4 refresh skill (`coordinator:strategic-self-description-refresh`),
> which reads the draft artifact this op writes and reconciles it against the repo's canonical
> `self-description.yaml` under human review — it never auto-commits over curated content. A
> secondary reader is any operator inspecting `state/strategic/self-description.draft.yaml`
> directly.
>
> **What this is NOT.** This is the *producer contract* for the draft artifact's shape and
> provenance — not example-doctrine-repo's refresh-skill reconciliation logic, not the frozen
> `strategic-self-description.schema.json` itself (that schema is the joint authority both sides
> build against; this doc only describes the generatable-subset the op emits), and not the
> human-curated fields (`relationship`, prose framing, anything marked `provenance: curated` or
> `provenance: asserted`) that only a human ceremony may author. See § 6 "Out of scope" for the
> full exclusion list.
>
> **Status: PROPOSED.** The strategic-self-description schema froze 2026-07-12 (example-doctrine-repo 017da24b);
> `strategic.generate` ships this session against that frozen schema. This contract moves to
> FROZEN once the op has shipped, AC5 (draft validates against the frozen generatable-subset) is
> green, and AC7 (dogfood — claude-klabauter's own draft generated end-to-end) passes.
>
> **Changelog:**
> - **2026-07-12 (PROPOSED):** initial authoring — op shipping this session against the
>   2026-07-12-frozen schema (example-doctrine-repo 017da24b); Track A / Track B input mapping, draft seam, and
>   non-clobber invariant documented ahead of/alongside the op landing.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-07-11-claude-klabauter-strategic-self-description-generation-leg.md`
> - Frozen schema: `example-doctrine-repo:coordinator/schemas/strategic-self-description.schema.json`
>   (x-schema-version 1.0.0, frozen example-doctrine-repo 017da24b)
> - Upstream standard (example-doctrine-repo-owned): `example-doctrine-repo:coordinator/docs/plans/2026-07-11-strategic-self-description-standard.md`
> - Consumer refresh skill: `coordinator:strategic-self-description-refresh`
> - Sibling contract (model): `coordinator_core/contract/deliverable-rollup-producer-contract.md`

---

## 0. Contract summary (read this first)

Claude-klabauter's `strategic.generate` op (**MUTATING**, scope `"none"`, fleet-generic, explicit
`target_root` wire param — mirrors `percolate.run`) derives strategic-self-description fields from
two independent evidence tracks and writes them, stamped `provenance: "generated"`, to a **draft**
artifact at `<target_root>/state/strategic/self-description.draft.yaml`. It never opens, reads for
merge, or writes the canonical `self-description.yaml`. Generation *seeds* human curation; it does
not perform it.

**Three planes, three roles:**
- **claude-klabauter** — derives `version_highlights[]` (Track A) and `competitors[]` (Track B) from
  evidence already on disk or provided by an upstream owner; owns this contract; writes only the
  draft path.
- **example-doctrine-repo (coordinator-claude)** — owns the frozen schema (joint authority), the refresh-ceremony
  skill that reconciles the draft against the canonical instance under human review, and the
  `workweek-complete` nudge that surfaces the refresh ceremony to the PM.
- **example-market-data-repo-team / repo-setup** — upstream *providers* of Track B raw signal (market-
  intel snapshot diffs; peer-repo identity marking) that `strategic.generate` consumes as input.
  Neither owns this op or this contract; both are named because their signal shape gates what
  Track B can emit.

**The non-clobber invariant, restated for producers.** A present draft file is safe to overwrite
on every run (idempotent replace, never append). The canonical file is **never** touched by this
op under any code path — not read for merge, not opened for write, not created on a clean tree.
Human ceremony is the sole canonical writer.

---

## 1. The op

`strategic.generate` — **MUTATING** (writes the draft artifact), scope `"none"` (fleet-generic,
explicit `target_root` required — no `common_dir` self-only default), classified per the DR-208
five-question model in `coordinator_core/authz/classification.py`. Closest working sibling: the
cartography ops (`coordinator_core/ops/cartography_tree.py`) — same scope-`"none"` +
`target_root` + `path_guard`-validated shape.

| Wire param | Type | Required | Notes |
|---|---|---|---|
| `target_root` | `str` | **yes** | Repo root to generate a draft for. Canonicalized and validated via `coordinator_core.cartography._guard.path_guard(target_root, ".")` before any read/write — fail-loud on a missing/invalid root, never silently falls back to the claude-klabauter repo itself. |

The op writes **exactly one file**: `<target_root>/state/strategic/self-description.draft.yaml`
(created, including the `state/strategic/` directory, if absent). No other path is read for
mutation purposes; no other path is written.

---

## 2. Inputs → fields mapping, per track

Two independent derivation tracks, split by signal maturity (upstream DEC-2). Each track is
independently degradable — a track with no usable signal emits an empty/absent field rather than
guessing, and does not block the other track from populating.

### 2.1 Track A — version history → `version_highlights[]`

**Inputs (checked in this fallback order — first available source wins per claude-klabauter repo; claude-klabauter is
tag-less today, so Track A's *primary* path in practice is the changelog, not tags):**

1. `git tags` — if the target repo carries annotated/lightweight release tags, each tag is a
   candidate `version_highlights[]` entry.
2. `state/week-changelog/*.md` — the **primary path today**. Each dated changelog entry
   (`state/week-changelog/YYYY-MM-DD-*.md`) is a candidate entry; claude-klabauter's own repo is verified
   tag-less (0 git tags), so this is the path that actually populates claude-klabauter's own draft.
3. `commit-log window` — a bounded recent commit-log window, used only when neither tags nor a
   week-changelog directory exist, as the sparsest fallback.

**Output shape (frozen schema, generatable-subset):**

```json
{
  "label": "<str>",
  "date": "<YYYY-MM-DD>",
  "bullets": ["<str>", ...],
  "provenance": "generated"
}
```

| Field | Source | Notes |
|---|---|---|
| `label` | tag name, or changelog entry title/filename-derived label, or commit-log window label | Display string |
| `date` | tag date, or changelog entry's date (from filename or frontmatter), or commit-log window end-date | Must conform to schema `format: date` (`YYYY-MM-DD`) |
| `bullets` | tag annotation body lines, or changelog entry bullet points, or summarized commit subjects | `str[]`; empty array is a safe null, not an error |
| `provenance` | literal constant | **always** `"generated"` — see § 4 |

### 2.2 Track B — market/peer signal → `competitors[]`

**Inputs (both PROVIDED — sourced by an upstream owner, not derived by claude-klabauter from repo state):**

1. **Market-intel snapshot diff** — owned and produced by the example-market-data-repo-team; this op
   consumes a diff/snapshot artifact as input, it does not itself scrape or research the market.
2. **Peer identity marking** — owned and produced by repo-setup; identifies which peer repos in
   the fleet are candidate competitor/complement entries.

**Output shape (frozen schema, generatable-subset — narrower than the full schema):**

```json
{
  "name": "<str>",
  "note": "<str|null>",
  "provenance": "generated"
}
```

| Field | Source | Notes |
|---|---|---|
| `name` | peer identity marking (repo-setup) | Display name of the peer/competitor |
| `note` | market-intel snapshot diff (example-market-data-repo-team), when available | Nullable — absent snapshot signal for a given peer emits `note: null`, never a guess |
| `provenance` | literal constant | **always** `"generated"` — see § 4 |

**`relationship` is NEVER emitted.** The full schema's `competitors[]` entry additionally requires
`relationship` (enum: `competitor \| complement \| prior-art \| superseded-by \| supersedes`). This
field is **human-curated only** — classifying a peer's relationship to this project is a judgment
call, not a derivation. `strategic.generate` MUST NOT emit `relationship` under any code path; the
draft's `competitors[]` entries are the narrower 3-field shape above, not the full schema shape.
Example-doctrine-repo's refresh ceremony is where a human adds `relationship` when promoting a draft entry into the
canonical file.

**When Track B has no usable signal** (no market-intel snapshot diff and/or no peer identity
marking available for the target repo), `competitors[]` is emitted as an empty array — an honest
"nothing to report," not an omitted field and not a fabricated entry.

---

## 3. The draft consumption seam (DEC-4)

| Property | Value |
|---|---|
| Path | `<target_root>/state/strategic/self-description.draft.yaml` |
| Shape | The frozen `strategic-self-description.schema.json`'s **generatable-subset only** — `version_highlights[]` and `competitors[]` (narrowed, no `relationship`) as specified in § 2. The draft is NOT a full canonical instance; it does not carry the fields only a human curates. |
| Trigger | **Present-triggers-consume.** The draft's existence is the signal; there is no separate "ready" flag or lock file. Example-doctrine-repo's refresh skill checks for the draft's presence and, when found, offers reconciliation. |
| Consumer | example-doctrine-repo's C4 refresh skill (`coordinator:strategic-self-description-refresh`) — the only reader of this path in the fleet today. |
| Write discipline | **Idempotent overwrite.** Every `strategic.generate` run replaces the draft wholesale; it never appends to or merges with a prior draft. A stale draft from a previous run is simply superseded, not accumulated. |

---

## 4. The `provenance` three-value model

The frozen schema's `provenance` enum is **three-valued**: `curated \| generated \| asserted`.
`strategic.generate` is a single-role producer within that model:

- **`generated`** — the value this op stamps on every field it emits. Machine-derived, from the
  input tracks in § 2.
- **`curated`** — human-authored/ratified fields in the *canonical* file. This op never reads,
  writes, or passes through `curated` content — there is none in the draft to preserve, because
  the draft never touches the canonical file (§ 5).
- **`asserted`** — a third category (human claim not yet ratified as curated) that exists in the
  full schema's vocabulary but has no producer role in this op. `strategic.generate` neither reads
  nor emits `asserted` fields.

**This op emits ONLY `"generated"`.** It never authors `"curated"` or `"asserted"` on any field,
under any code path, for any track.

<!-- Review: code-reviewer (F6) — undocumented writer-side defense-in-depth, now noted. -->
**Defense-in-depth note.** `draft_writer.write_draft` additionally re-stamps any missing/falsy
`provenance` on every dict it walks inside each field-list entry (Track A/B already stamp
`"generated"` upstream; this is a safety net against a future upstream regression). This walk is
scoped to the field-list entries only (`version_highlights[]` / `competitors[]` items) — it never
touches the top-level draft document, which carries no `provenance` property of its own.

---

## 5. The non-clobber invariant (DEC-3)

**The op writes ONLY the draft path.** It never opens, reads-for-merge, or writes
`<target_root>/self-description.yaml` (or wherever the canonical instance resides) under any code
path — success, partial-track-degradation, or error. This is a structural invariant, not a
best-effort guard: the writer has no code path that references the canonical file at all.

- **Existing canonical present:** untouched, byte-identical, before and after any
  `strategic.generate` run.
- **No canonical present (clean tree):** the op creates `state/strategic/` and the draft file; it
  does **not** create a canonical `self-description.yaml`. A clean tree stays clean of a canonical
  file until a human runs the refresh ceremony and ratifies one.

**Generation seeds curation; it does not perform it.** The draft is raw material for a human
decision, never a substitute for one. Promotion from draft → canonical is exclusively example-doctrine-repo's
refresh-skill ceremony, which is the sole path by which `curated`/`asserted` fields, the
`relationship` enum, and any other human-only content enter the canonical file.

---

## 6. Out of scope — not our surface

- **example-doctrine-repo's refresh-skill reconciliation logic** — how the draft is diffed against the canonical
  file, what UI/prompt the human sees, and the ratification/commit flow. Example-doctrine-repo's own design surface.
- **The frozen schema's full shape** — fields outside the generatable-subset (anything requiring
  `curated`/`asserted` provenance, `relationship`, and any canonical-only field) are not authored
  or validated by this op; they are example-doctrine-repo-schema-owned and human-ceremony-populated.
- **Sourcing Track B's raw signal** — producing the market-intel snapshot diff is
  example-market-data-repo-team-owned; producing the peer identity marking is repo-setup-owned. This op
  consumes both as inputs; it does not generate them.
- **Cadence/scheduling** — `strategic.generate` is invokable on demand and nudged (never
  cron-scheduled) ahead of the example-doctrine-repo-owned refresh ceremony (DEC-5). The nudge surface itself
  (doctor probe / workday-start marker) is documented in
  `docs/plans/2026-07-11-claude-klabauter-strategic-self-description-generation-leg.md` § C5(b); this
  contract covers only the artifact this op produces, not the nudge machinery.
- **A JSON-Schema sibling of this contract** — the frozen `strategic-self-description.schema.json`
  already is the joint-authority schema; this contract adds the derivation/seam/invariant
  narrative on top of it, not a competing schema.

---

<!-- producer-contract: claude-klabauter strategic.generate op — draft artifact for example-doctrine-repo's refresh ceremony.
     PROPOSED (2026-07-12) — schema frozen (example-doctrine-repo 017da24b); op shipping this session. -->
