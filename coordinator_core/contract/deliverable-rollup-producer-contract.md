# coordinator → deliverable-rollup producer contract (FROZEN)

> **What this is.** The FROZEN producer-side contract of the structured payload that the
> coordinator control-plane engine (**claude-klabauter**) derives and returns via the `deliverable.rollup` op.
> It defines the return-field schema, per-field grain/cardinality/precision, and the resolution
> semantics (DIRECT — slice-1 only), so that **DoE** can build its completion-entry fold render
> (C5 of `DoE-claude:docs/plans/2026-07-06-mechanize-execution-record-fold.md`) against a stable
> schema — without further co-design on the claude-klabauter side.
> claude-klabauter *derives and resolves*; DoE *composes the prose* and owns the completion entry.
>
> **Who consumes this.** A context-less DoE EM building the `/workstream-complete` completion-entry
> fold render (their C5). Everything needed to wire the call and render the field values is in this
> file: the return-field schema with grain and cardinality, the precision guarantee, the recall
> envelope, the `cc_invoke` / fail-open consumer notes, the vendored-pin discipline, and the bump
> protocol.
>
> **What this is NOT.** This is the *producer contract* — not DoE's prose fold implementation (C1–C4
> of their plan, a separate surface). It does not specify the completion-entry title, body, or the
> `## Execution Observations` fold that DoE composes from its own frontmatter. It does not specify
> DoE's `cc_invoke` call site or fail-open error handling — those are DoE's own design surface. It
> does not cover rag ingest or cockpit display of the rollup payload — those are out of scope for
> slice-1 (see § "Out of scope"). See § "Out of scope" for the full exclusion list.
>
> **Status: FROZEN — v1.0, frozen 2026-07-06.** Freeze gate cleared on the claude-klabauter side: the
> `deliverable.rollup` op shipped, all AC7 tests pass, and the command-type dispatch smoke is green
> (a JSON-RPC `dispatch_message` invocation resolves a known deliverable end-to-end — the DR-215
> command-type equivalent of the retired resident-daemon live-socket check; there is no resident
> socket to smoke under DR-215). Freeze **gates** DoE's C5 render wiring (DoE builds against this
> frozen schema; wiring is downstream of freeze, not a freeze precondition). Post-freeze changes
> follow the reader-widen-before-writer-flips bump protocol (§ 5); DoE is notified via cross-repo
> memo before any breaking change.
>
> **Changelog:**
> - **2026-08-13 (leg (a) widen LANDED — gate cleared, SUPERSEDES the PENDING entry below):**
>   `artifacts_matched`'s scan surface now covers five roots — `state/sizings/**` folded into
>   `RESOLVABLE_ARTIFACT_ROOTS` alongside the existing `docs/plans`, `state/handoffs`,
>   `archive/handoffs`, `archive/specs`. The gate below assumed the reader
>   (`coordinator_render_rollup.py`) was DoE-side and would need a reader-widen-before-writer-flips
>   round trip per § 5.2. That assumption was wrong: since the finish-strangler port,
>   `coordinator_render_rollup.py` is **claude-klabauter-resident**, and it is **count-agnostic** over
>   `artifacts_matched` (`grep -c artifacts_matched coordinator_core/ops/coordinator_render_rollup.py`
>   returns 0 — the field appears nowhere in that reader's production path). Ruled in
>   `cross-repo/inbox/2026-08-13-doe-claude-em-spec-backlink-id-form-ruled-and-rollup-cleared.md`:
>   widen freely, no DoE-side reader change or sign-off was required, the `be8b5d88` precedent
>   (which WAS a genuine DoE-side reader widen, for `scan_incomplete`) does not apply here. The
>   writer (`_scan_artifacts_by_deliverable_id` in `deliverable_rollup.py`) is flipped as of this
>   entry — `state/sizings/*.yaml` (whole-document YAML, no frontmatter fence) is now live-scanned.
> - **2026-08-13 (breaking-semantics widen, PENDING — SUPERSEDED same-day, see entry above):**
>   `artifacts_matched`'s scan surface is documented to widen from four roots to five, adding
>   `state/sizings/**` alongside the existing `docs/plans`, `state/handoffs`, `archive/handoffs`,
>   `archive/specs`. Classified BREAKING per § 5.2 (semantics changed, not merely additive) because
>   it changes what an existing count already means, not just what it can also include. Flagged to
>   claude-central-em via `coordinator/bin/cross-repo-memo.py` (delivered alongside C6's convention
>   memo as one delivery with two separated asks). Per § 5.2's breaking-change clause, this
>   MUST NOT ship until DoE has landed its widen AND explicitly acknowledged the breaking shape —
>   this entry documented the pending, gated change only; the writer (`_scan_artifacts_by_deliverable_id`
>   in `deliverable_rollup.py`) had NOT been flipped at the time of this entry. Also corrects a
>   pre-existing, unrelated staleness in the `scan_incomplete` row and § 1.3: `archive/specs` was
>   already a live scan root in the scanner but was missing from both tables before this edit.
>   **Superseded same-day**: the "claude-klabauter notifies DoE, DoE widens, DoE acks" framing this entry
>   assumed was itself mistaken — see the entry above for the correction and the reply that
>   cleared the gate.
> - **2026-07-26 (additive widen, post-freeze v1.0):** `scan_incomplete` (bool) added to the
>   emitted payload. Followed the § 5.2 reader-widen-before-writer-flips protocol: DoE widened
>   their `coordinator_render_rollup` reader first (`be8b5d88`, additive/absent-safe), replied
>   confirming readiness, and claude-klabauter flipped the writer to emit the field on every response
>   (including the safe-empty shapes). No breaking change; no version bump.
> - **2026-07-25 (citation re-point):** Tri-plane boundary spec-backlink re-pointed to
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`, the ratified successor
>   authority for `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md`; the 2026-07-03
>   citation is retained alongside as the historical source. Prose/citation-only; no field, grain,
>   cardinality, or resolution semantics changed, so this is outside the § 5 bump protocol and does
>   not reopen the FROZEN v1.0 gate.
> - **2026-07-06 (op-gap amend, post-freeze v1.0):** initiative-entity resolution root relocated from the scanned-worktree `state/initiatives/` to the claude-klabauter central-state root (worktree-local fail-open fallback); section-3 negative-spec amended for the env-miss-only non-git machine-local registry subprocess. Schema/wire shape UNCHANGED — op-gap, no version bump, no re-vendor required.
> - **2026-07-06 (FROZEN, v1.0):** freeze gate cleared — op shipped (`deliverable.rollup`,
>   COMPUTE_ONLY, common_dir-keyed), AC7 suite green, command-type dispatch smoke green. DoE builds
>   its C5 render against this frozen schema.
> - **2026-07-06 (PROPOSED):** initial authoring from C0 investigation findings; DIRECT semantics
>   pinned (transitive rejected — 0 recall, false-edge risk per `commit.anchors` §6.1 precedent);
>   field schema derived from C0 findings doc; recall envelope recorded verbatim.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-07-06-claude-klabauter-deliverable-spine-factsupply-op.md`
> - C0 findings: `docs/plans/2026-07-06-claude-klabauter-deliverable-spine-factsupply-op.c0-findings.md`
> - Demand memo: `cross-repo/inbox/2026-07-06-2026-07-06-completion-fold-factsupply-unlock.md`
> - Boundary adjudication: `cross-repo/inbox/2026-07-06-completion-fold-boundary-reply.md` (Option B)
> - Deliverable spine: `docs/decisions/DR-207-deliverable-spine-initiative-entity.md`
> - Tri-plane boundary: `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md` (superseded
>   on read-model ownership / dual-write ban by
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`)
> - Sibling contract (model): `coordinator_core/contract/commit-trailer-producer-contract.md`

---

## 0. Contract summary (read this first)

Claude-klabauter's `deliverable.rollup` op (`COMPUTE_ONLY` — DR-208 five-question classification in
`coordinator_core/authz/classification.py`) resolves initiative forward-edges from the live
deliverable-spine read-model given a `deliverable_id` wire parameter, and returns them as
structured fields. No prose is composed; no state is written.

DoE's `/workstream-complete` completion-entry fold (their C5 render) calls this op via `cc_invoke`
to retrieve the structured fields, then folds them into the entry prose it already owns. The
result is one factual sentence in the entry: *"advances initiative R"* (direct FK membership —
see § 3 for semantic-honesty note).

**Three planes, three roles:**
- **claude-klabauter** — derives initiative forward-edges from its deliverable-spine read-model; owns this
  contract; returns structured fields only.
- **DoE (coordinator-claude)** — consumes the structured fields via `cc_invoke`; composes the
  completion-entry prose and the `/workstream-complete` entry fold; owns the call site, the
  fail-open error handling, and the entry render. DoE's C1–C4 prose fold and this op are
  independent workstreams; only DoE's C5 render is gated on this contract freezing.
- **example-retrieval-repo / example-cockpit-repo** — no consumer role for this op in slice-1. The rollup payload
  goes to DoE's entry prose, not a typed durable store. If a durable-store need surfaces later,
  that is a separate contract (see § "Out of scope").

**Producer precision guarantee.** A *present* `advances_initiatives` entry is a high-confidence
edge; claude-klabauter includes only what it can resolve to a real `state/initiatives/<id>.yaml` file.
An *empty* list is a safe null, never a guess. A wrong initiative entry is worse than none —
DoE renders it as a confident false fact.

---

## 1. Return-field schema

### 1.1 Field table

Input parameter: `deliverable_id: str` — wire token; used ONLY as a frontmatter filter VALUE,
never as a filesystem path component (see § 3, security note).

```json
{
  "deliverable_id": "<the queried id, echoed>",
  "resolution_mode": "direct",
  "artifacts_matched": <int>,
  "advances_initiatives": [
    {"id": "<initiative-id>", "label": "<label>", "status": "<status|null>"}
  ],
  "scan_incomplete": <bool>
}
```

| Field | Source | Grain | Cardinality | Notes |
|-------|--------|-------|-------------|-------|
| `deliverable_id` | echoed from wire param | deliverable-grain | **1** (always present; echo) | Identity echo — the queried id; enables response correlation |
| `resolution_mode` | literal constant | op-shape | **1** (always `"direct"` in slice-1) | Documents which resolution semantics produced the payload; leaves room for an additive `"transitive"` mode later without a breaking schema change |
| `artifacts_matched` | count of artifacts (plan/handoff/sizing) carrying the queried `deliverable_id` | artifact-count | **1** (integer ≥ 0) | 0 → unknown deliverable → `advances_initiatives` is empty; N > 1 is the EXPECTED case (a deliverable spans multiple artifacts by design — see § 1.2). **LANDED 2026-08-13:** the sizing root (`state/sizings/**`) is now part of the five-root scan surface — see Changelog |
| `advances_initiatives` | UNION of non-null `initiative` FKs across all matching artifacts, deduped by `id`, each resolved to its `state/initiatives/<id>.yaml` entry | initiative-grain | **0..N** (empty list is the safe null and the COMMON case today — see § 2 recall envelope) | Each entry included ONLY when the FK is non-null AND resolves to a real `state/initiatives/<id>.yaml`; precision-over-recall at the edge level |
| `scan_incomplete` | True when any scan root (`docs/plans`, `state/handoffs`, `archive/handoffs`, `archive/specs`, `state/sizings/**`) could not be fully enumerated (e.g. permission-denied) | scan-shape | **1** (always present; bool) | Additive field, landed 2026-07-26 per the § 5.2 bump protocol — DoE's reader widened first (`be8b5d88`), appending `" (partial scan)"` per rendered line when set. `True` means this payload may be missing artifacts/initiatives; treat as "incomplete", never as "genuinely empty". `archive/specs` and `state/sizings/**` are both now live scan roots this field covers |

### 1.2 `advances_initiatives` entry shape

Each entry in the array is:

| Sub-field | Source | Notes |
|-----------|--------|-------|
| `id` | initiative id (FK from artifact frontmatter, dedup key) | durable minted id; join key |
| `label` | `label` field from `state/initiatives/<id>.yaml` | display string; may be null if the YAML has no `label` key |
| `status` | `status` field from `state/initiatives/<id>.yaml` | nullable; omit / null when the YAML has no `status` key |

**Cardinality note — N > 1 artifacts is expected, not ambiguous.** A deliverable is designed to
span multiple plans, stubs, and handoffs; the op **aggregates** (UNION of forward-edges across all
matching artifacts, deduped by `id`). This is distinct from `commit.anchors`' omit-on-multi rule,
which applies to a single-commit→single-plan grain. The multiplicity here is the expected grain,
not an ambiguity signal.

### 1.3 Resolution scan surface

The op scans:
- `docs/plans/*.md` frontmatter — the **primary** surface for the `deliverable_id` + `initiative`
  co-occurrence (plans mint `deliverable_id` and most often carry the `initiative` FK).
- `state/handoffs/*.md` frontmatter — secondary surface (stubs; may carry `deliverable_id`).
- `archive/handoffs/**/*.md` frontmatter — archived stubs; same scan.
- `archive/specs/**/*.md` frontmatter — archived plans (`fleet.archive_completed_plans` moves a
  plan from `docs/plans/` to `archive/specs/<YYYY-MM>/` on completion). This root was already
  live in the scanner; it was missing from this list, pre-existing drift corrected 2026-08-13.
- `state/sizings/*.yaml` — **LANDED 2026-08-13.** Whole-document YAML sizing objects (no
  frontmatter fence), read via a dedicated YAML reader rather than the markdown-frontmatter
  parser. Folded into `RESOLVABLE_ARTIFACT_ROOTS` alongside the four roots above once the reply
  in `cross-repo/inbox/2026-08-13-doe-claude-em-spec-backlink-id-form-ruled-and-rollup-cleared.md`
  established the reader is count-agnostic and claude-klabauter-resident (see Changelog).

A handoff-only scan under-counts direct recall because the `initiative` FK co-occurs with
`deliverable_id` predominantly in plan frontmatter, not handoff frontmatter.

---

## 2. Recall envelope (verbatim from C0 findings)

> As of 2026-07-06, of 46 fleet `deliverable_id`s, **2 resolve a non-empty rollup** (both →
> the single `claude-klabauter-strangler` initiative); transitive resolution is **0**. The op returns an
> empty `advances_initiatives` for ~96% of deliverables today. This is a substrate-population
> state, not an op defect: recall appreciates automatically as the `initiative` FK is populated
> across plans/stubs. **Leverage-unlock: populating the `initiative` FK across the deliverable
> spine** is what makes this rollup rich — DoE should weigh whether wiring the C5 render is worth
> it now, or defer the render until FK population climbs.

This envelope is surfaced in the AC8 freeze brief to DoE so they can make an informed wiring
decision. The freeze brief names the measured recall numbers; it is NOT sufficient to say
"sparse coverage" without the count.

---

## 3. Producer precision guarantee

**A present `advances_initiatives` entry is a high-confidence edge. An empty list is a safe null,
never a guess.**

Claude-klabauter's `deliverable.rollup` op resolves only what it can pin unambiguously:

- **`advances_initiatives`** — an entry is included only when: (i) the artifact's `initiative`
  frontmatter FK is non-null, AND (ii) a real `state/initiatives/<id>.yaml` file exists for
  that id. Both conditions must hold. An FK that is present but resolves to no file is OMITTED
  (precision-over-recall at the edge level). Deduplication is by `id` across all matching
  artifacts.
- **`artifacts_matched`** — a count of artifacts carrying the queried `deliverable_id` in their
  frontmatter; 0 when the id is unknown. Never inflated.

This guarantee is **asymmetric by design**: the precision cost of a false initiative entry
(DoE renders a confident wrong affiliation) exceeds the recall cost of an empty list (DoE
omits the rollup sentence, or renders it as absent — not an error).

**Semantic-honesty note.** The contract resolves **"advances initiative R"** (direct FK
membership — the artifact explicitly carries `initiative: R` in its frontmatter). It does NOT
resolve the **"unblocks initiatives R/S"** transitive reading that DoE's demand memo used as its
motivating framing. Transitive resolution (walking `blocks` DAG edges downstream to their
initiative FKs) resolves **0** deliverables today (no stubs carry `blocks:` frontmatter edges)
and was rejected for slice-1 per the `commit.anchors` §6.1 false-transitive-edge precedent.
DoE MUST render the direct fact only: *"advances initiative R"*, not *"unblocks R/S"*. The
`resolution_mode: "direct"` field is the machine-readable signal for this semantic.

**Security note — wire token never becomes a path component.** `deliverable_id` from the wire
is used ONLY as a frontmatter filter value matched against the parsed frontmatter of scanned
files. It is NEVER used as a filesystem path component, directory fragment, or `open()` argument.
Malformed or injected values (e.g. `../`, absolute paths, embedded nulls) return safe-empty, not
an error, because the token simply does not match any artifact's frontmatter value.

**Resolution-semantics note.** Initiative entity files (`state/initiatives/<id>.yaml`) are
resolved from the **claude-klabauter central-state root** (`coordinator_state_root --central`, confirmed
Claude-klabauter-resident under DR-209 tri-plane state-placement law), with a **worktree-local fail-open
fallback** when the claude-klabauter root is unresolvable (`CLAUDE_KLABAUTER_ROOT` env-miss AND `machine-local get
repos.claude_klabauter` returns nothing). For claude-klabauter's own worktree the central directory and the
worktree-local directory coincide (`coordinator_claude_klabauter_root` IS claude-klabauter's repo root), so
Claude-klabauter-own rollups resolve correctly via either branch.

**No git subprocess — hard invariant.** This op reads on-disk frontmatter and YAML only. It MUST
NOT spawn any git subprocess. It has no staged-index reads to perform; the `commit.anchors`
git-subprocess carve-out does NOT apply here and does NOT transfer. **Single non-git subprocess
carve-out (env-miss only, memoized once per process):** on `CLAUDE_KLABAUTER_ROOT` env-miss, the op may
invoke `machine-local get repos.claude_klabauter` (a non-git, read-only registry lookup) at most
once per process lifetime to resolve the claude-klabauter central-state root; the result is memoized at
module scope (`_RESOLVED_CENTRAL_ROOT` sentinel) so all subsequent calls return the cached value
without spawning a process. This carve-out preserves the `COMPUTE_ONLY` classification — the
subprocess is non-git, non-state-mutating, and read-only; none of the five DR-208 answers flip.

---

## 4. Consumer notes — DoE

### 4.1 Invocation

DoE calls the op via `cc_invoke` (the coordinator-core IPC shim) with the `deliverable.rollup`
op name and `{"deliverable_id": "<the deliverable id>"}` as the wire params.

**TWO-SIGNAL rc 2 → skip rollup, log-and-continue.** DoE's DR-215 `cc_invoke` shim surfaces
the coordinator-core TWO-SIGNAL return-code convention: rc 2 means the op is absent or the
engine is down. On rc 2, DoE skips the rollup sentence and continues to write the completion
entry without it. This is **fail-open**: the entry ships regardless of rollup availability.

### 4.2 Fail-open in both modes

DoE MUST implement fail-open for both failure modes:

| Failure mode | DoE behaviour |
|---|---|
| Engine down (socket unavailable) | Skip rollup sentence; entry ships without it. |
| Op absent (rc 2 / `-32601`) | Same: skip, log, continue. |
| Empty `advances_initiatives` (normal) | Omit the rollup sentence entirely — do not render "advances: none". |
| Non-empty `advances_initiatives` | Render: *"advances initiative `<label>` (`<id>`)"* (or equivalent prose) per DoE's entry format. |

The op returning an empty list is the COMMON case today (~96% of deliverables — see § 2). DoE
must not treat an empty list as a warning or error.

### 4.3 Rendering the `resolution_mode` field

DoE uses `resolution_mode` to select the correct prose template:
- `"direct"` → *"advances initiative R"* framing (direct FK membership).
- A future `"transitive"` value (additive widen — see § 5) → *"unblocks initiative R"* framing.

DoE MUST NOT render the transitive *"unblocks"* framing when `resolution_mode` is `"direct"`.
This is the semantic-honesty gate.

---

## 5. Vendored-pin and bump protocol

### 5.1 Vendored-pin discipline

**DoE MUST vendor-pin a snapshot of this contract.** DoE MUST NOT reference
`coordinator_core/contract/` at live head across repo boundaries. Live-head cross-repo reference
re-introduces the silent-drift failure the vendored-pin discipline exists to prevent
(lesson `2026-07-04-vendored-pin-cross-repo-contract-not-liv.yaml`).

- **DoE:** vendor the contract snapshot under `contract/upstream/deliverable-rollup-producer-contract.md`
  (or equivalent path in DoE's repo). **Record the vendored-at commit SHA and date** in the
  snapshot header or in a companion `.meta` file alongside it.
- DoE MUST NOT read this contract from `coordinator_core/contract/` at live head at the time of
  wiring C5 or subsequently. The vendored snapshot is the source of truth for the consumer build.

On any contract bump (post-freeze), DoE receives a cross-repo memo identifying the changed fields
and the widen-before-flip requirement. DoE re-vendors the updated snapshot after completing its
widen.

### 5.2 Bump protocol (post-freeze)

All post-freeze changes follow **reader-widen-before-writer-flips**:

1. Claude-klabauter notifies DoE via cross-repo memo, naming the added/changed field and the effective date.
2. DoE widens its render (accepts the new value; old behaviour unchanged for existing values)
   and replies confirming readiness.
3. Claude-klabauter begins emitting the new field/value only after DoE confirms.

**Additive changes** (new optional field, new `resolution_mode` enum value, relaxed cardinality,
new sub-field in `advances_initiatives`): follow the widen-before-flip protocol; no schema version
bump.

**Breaking changes** (removed field, renamed field, cardinality tightened, semantics changed):
MUST be flagged as breaking in the notification memo and MUST NOT ship until DoE has landed its
widen AND explicitly acknowledged the breaking shape. Breaking changes to a frozen contract are
expected to be extremely rare; the design intent is additive-only evolution.

**Example additive non-breaking widen — transitive resolution mode.** A future slice may add
`resolution_mode: "transitive"` to the payload (when DAG edges and downstream initiative FKs are
populated). This is a `resolution_mode` enum widen — DoE widens its render before claude-klabauter emits
the new value. The schema shape is unchanged; only the enum vocabulary expands. DoE would
then render *"unblocks initiative R"* when `resolution_mode` is `"transitive"`.

---

## 6. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part of
this contract**:

- **DoE's completion-entry prose fold (C1–C4)** — the entry title, body, `## Execution
  Observations` fold, and all prose outside the rollup sentence. DoE's own design surface.
- **`cc_invoke` shim / DoE-side fail-open wiring** — the call site, error handling, and
  TWO-SIGNAL rc interpretation live in DoE's code. Claude-klabauter authors the contract and the op;
  DoE authors the call site.
- **Historical backfill** of rollups onto past completion entries — going-forward only. Past
  entries are not amended; slice-1 is populate-going-forward only.
- **rag ingest / cockpit display of the rollup payload** — not requested for this op. The fields
  go to DoE's entry prose. If a durable-store or display need surfaces later, that is a separate
  contract and a separate rag/cockpit coordination surface.
- **A JSON-Schema sibling of this contract** — natural follow-on after freeze and DoE's vendored
  pin confirmation; not this plan.

---

<!-- producer-contract: claude-klabauter deliverable.rollup op — coordinator→DoE structured fields.
     FROZEN v1.0 (2026-07-06) — op shipped + AC7 green + command-type dispatch smoke green. -->
