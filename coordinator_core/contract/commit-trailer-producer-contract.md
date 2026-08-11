# coordinator → git-history commit-trailer producer contract (FROZEN)

> **What this is.** The FROZEN producer-side contract of the git commit-trailer set that the
> coordinator control-plane engine (**claude-klabauter**) derives and emits via the `commit.anchors` op.
> It defines the trailer key set, per-key source, grain, cardinality, and durable-join semantics,
> so that **example-retrieval-repo** can build its typed ingest and **example-cockpit-repo** can build its
> intent→reality display against a stable schema — without further co-design on the claude-klabauter side.
> claude-klabauter *derives and stamps*; rag *ingests and serves*; cockpit *displays over rag's surface*.
>
> **Who consumes this.** A context-less example-retrieval-repo EM building the commit-edge ingest path, and
> a context-less example-cockpit-repo EM building the intent→reality display. Everything needed to
> build against the trailer schema is in this file: the key table with grain and join semantics,
> the `Nature:` enum, the dual-legibility extraction pattern, the producer precision guarantee,
> the vendored-pin discipline, and the bump protocol.
>
> **What this is NOT.** This is the *producer contract* — not rag's ingest implementation or
> cockpit's display UI. It does not specify rag's temporal model, watermark driver, or
> `workstate-query` surface shape. It does not specify cockpit's filter UI or query-mapping
> entries. See § "Out of scope — not our surface".
>
> **Status: FROZEN — co-design closed 2026-07-05, v1.0.** All three freeze-gate items are
> resolved (see § 0, Freeze-gate items): item 1 (rag↔cockpit `workstate-query` v8 alignment)
> closed bilaterally by rag + cockpit; item 2 (`source_kind: git_commit` enum) notify-at-freeze
> → example-doctrine-repo/central; item 3 (claude-klabauter `commit.anchors` op + invocation shim) shipped. Sibling EMs
> build against this frozen schema. Post-freeze changes follow the reader-widen-before-writer-flips
> co-ratified bump protocol (§ 5.2); consumers are notified via cross-repo memo before any
> breaking change.
>
> **Changelog:**
> - **2026-08-10 (producer-count correction):** § 1.2's "claude-klabauter has TWO independent producers of
>   this one FK" corrected to THREE: `commit.anchors`, `coordinator-prepare-commit-msg`, and the
>   previously-unnamed `coordinator_core/git/commit_trailers.py::compute_missing_trailer_args`,
>   invoked by `coordinator_core/ops/ceremony/git_native.py::commit_scoped`'s diverged/commit-tree
>   branch (which fires no git hooks, which is why that third producer exists at all). Also records,
>   under § 3, the producer-side behaviour change this plan's C2 makes: an ambiguous multi-claim
>   commit now omits `Deliverable-Id:` entirely, a posture `commit_anchors.py` already had. Prose/
>   fact-correction only; no key, grain, enum, cardinality, or extraction pattern changed, so this
>   is outside the § 5.2 bump protocol and does not reopen the FROZEN v1.0 gate. Source:
>   `docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md`, task C5.
> - **2026-08-01:** `Resolves:` added to § 1.1's key table as an eighth recognized trailer,
>   completion-grain, 0..N, author-supplied-shape but now ALSO stamped by `commit.anchors` at
>   the workstream-complete / ship-handoff completion event (a one-key extension of the
>   already-firing stamper — see `commit_anchors.py`). This does **not** reopen the FROZEN
>   v1.0 co-design gate — it is an additive new key on a producer whose shape (dual-legible
>   trailer emitted by `commit.anchors`) is unchanged; it follows the reader-widen-before-
>   writer-flips protocol (§ 5.2) like any other additive change. Before this, `Resolves:`
>   was a documented convention (`coordinator/docs/wiki/resolves-commit-trailer.md`) with
>   **zero** producers fleet-wide (0 commits in claude-klabauter's entire history carried it)
>   — `rollup_derive.py`'s sole ship-state oracle join
>   (`--grep=Resolves: {artifact_id}`, exact-key-verified) therefore returned
>   `no-resolving-commits` for every deliverable ever shipped. The oracle's join is left
>   semantically UNCHANGED by this entry — it is NOT widened to accept `Deliverable-Id:`
>   (that trailer is workstream-MEMBERSHIP grain, stamped from a workstream's first commit;
>   `Resolves:` is COMPLETION grain, stamped only at the ceremony completion event, gated
>   on an `archive/completed/*.md` completion entry also being present in the same staged
>   diff). Source: `docs/plans/2026-08-01-baton-spine-information-integrity.md` § A1.
> - **2026-07-25 (citation re-point):** Tri-plane boundary spec-backlink and the DD#1 freshness
>   cross-check citation re-pointed to `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`,
>   the ratified successor authority for `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md`'s
>   DD#1 (read-model ownership / dual-write ban); the 2026-07-03 citation is retained alongside as
>   the historical source. Prose/citation-only; no key, grain, enum, cardinality, or extraction
>   pattern changed, so this is outside the § 5.2 bump protocol and does not reopen the FROZEN v1.0
>   gate.
> - **2026-07-25:** Role sentence above scoped from "rag *ingests and stores*" to "rag *ingests
>   and serves*". Prose-only; no key, grain, enum, cardinality, or extraction pattern changed, so
>   it is outside the § 5.2 bump protocol and does **not** reopen the FROZEN v1.0 gate. Rationale:
>   an ownership sentence silently reads as a residency sentence, and this contract's stated reader
>   is "a context-less example-retrieval-repo EM" — precisely who would take "stores" as a residency licence
>   (PM-ratified at example-store-repo 2026-07-25: example-retrieval-repo owns capability code, not custody of data).
>   The narrower statements at § "Who owns what" and § 4 remain accurate as written: they scope
>   storage to the trailer stream specifically, which is in-fleet work-state the rule permits.
>   Source: `cross-repo/inbox/2026-07-25-example-cockpit-repo-em-commit-trailer-contract-residency-phrasing.md`.
> - **2026-07-17:** `Closes:` added to § 1.1's key table. This does **not** reopen the FROZEN
>   v1.0 co-design gate — `Closes:` is not derived/stamped by `commit.anchors` (the surface this
>   freeze governs); it is a pre-existing author-supplied git-trailer convention that a *separate*
>   claude-klabauter subsystem (the emit-time `commit_closures` section porter) now reads. Documented here
>   so this table stays the single registry of recognized commit trailers (the Staff Engineer F9). Source:
>   `docs/plans/2026-07-17-commit-closure-emission-fact.md`.
> - **2026-07-05 (FROZEN, v1.0):** co-design closed — all three freeze-gate items resolved.
>   Item 1 (rag↔cockpit `workstate-query` v8 alignment) closed bilaterally (rag `d5370eeab`
>   trailer-edge-query-spec + `4bff8ce43` trailer-edge-key-pin; cockpit confirmed; DR §7 resolved).
>   Item 2 (`source_kind: git_commit`) notify-at-freeze → example-doctrine-repo/central (ships with rag surface (b)).
>   Item 3 (claude-klabauter `commit.anchors` op + shim) shipped (`043b214` C1–C4, `9f3ad55` shim,
>   `da13148` workstream-complete). Trigger memo:
>   `cross-repo/inbox/2026-07-05-cockpit-rag-alignment-closed-freeze-gate-a.md`.
> - **2026-07-04 (PROPOSED):** initial authoring from plan D1 + co-design resolution; rag
>   `Plan-Id:` widen ACCEPTED; `Anchor:` demoted to human-legible breadcrumb; transitive-handoff
>   walk retired in favour of direct minted-id edges; cockpit `Nature:` filter facet accepted;
>   vendored-pin confirmed by both consumers.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-07-04-claude-klabauter-commit-anchor-stamper.md`
> - Co-design resolution: plan § Co-design resolution (2026-07-04)
> - Deliverable spine: `docs/decisions/DR-207-deliverable-spine-initiative-entity.md`
> - Tri-plane boundary: `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md` (superseded
>   on read-model ownership / dual-write ban by
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`)
> - example-doctrine-repo ratification memo: `cross-repo/inbox/2026-07-04-claude-klabauter-stamper-ratify-and-wedge-closed.md`
> - rag co-design reply: `cross-repo/inbox/2026-07-04-rag-consumed-subset-trailer-contract.md`

---

## 0. Contract summary (read this first)

Claude-klabauter's `commit.anchors` op (`COMPUTE_ONLY` — see plan D2, AC2) derives a structured git trailer
block from the live operational read-model and returns it as text. The existing
`coordinator-prepare-commit-msg` hook (example-doctrine-repo surface) stamps that block onto every coordinator-session
commit, extending the already-in-use `Session-Id:` trailer injection.

The result is a set of **six trailer keys** per commit — five derived by claude-klabauter, one already-in-place
(`Session-Id:`). Dual-legible by construction: plainly human-readable in `git log`; machine-extractable
via `git interpret-trailers` and `git log --format='%(trailers:key=...)'`.

A **seventh key, `Closes:`**, is documented in § 1.1 alongside these six for registry
completeness, but it is architecturally distinct: it is **not derived or stamped by
`commit.anchors`** — it is a pre-existing author-supplied convention (the commit author writes
`Closes: <ID>` themselves) that claude-klabauter's *emit-time* `commit_closures` section porter reads via
a git-native trailer scan (see `example-retrieval-repo-producer-contract.md § 2.5`). It is included here so
this table remains the single source of truth for every commit trailer claude-klabauter's tooling
recognizes, derived or not.

An **eighth key, `Resolves:`**, is a completion-grain trailer ALSO derived and stamped by
`commit.anchors` (2026-08-01 addition) — unlike `Closes:`, it IS produced by this op, at the
workstream-complete / ship-handoff completion event only (gated on a staged
`archive/completed/*.md` completion entry alongside the plan file; see § 1.1). It reuses the same
`deliverable_id` already resolved for `Deliverable-Id:` — same value space, different grain
(membership vs. completion) and different emission gate (every commit vs. completion-event-only).

**Three planes, three roles:**
- **claude-klabauter** — derives anchors from its live read-model; stamps trailers; owns this contract.
- **example-retrieval-repo** — ingests the trailer stream from git history into its typed store; owns the query surface.
- **example-cockpit-repo** — displays the intent→reality graph over **rag's** `workstate-query` surface,
  not directly from claude-klabauter.

**Producer precision guarantee.** A *present* trailer key is a high-confidence edge; claude-klabauter stamps only
what it can resolve unambiguously. An *absent* key is a safe null, never a guess. A wrong stamped anchor
is worse than none — cockpit renders it with confidence as a false edge.

### Freeze-gate items (three items — all resolved at freeze 2026-07-05)

| # | Item | Owner | Status |
|---|------|-------|--------|
| 1 | **rag ↔ cockpit `workstate-query` v8 alignment** — rag exposes trailer edges keyed on logical-repo identity so cockpit adds one query-mapping entry, not a second transport. | rag + cockpit bilateral | **RESOLVED** — closed bilaterally (rag `d5370eeab` + `4bff8ce43`; cockpit confirmed; DR §7 resolved 2026-07-05) |
| 2 | **`source_kind: git_commit`** provenance enum extension — rag needs a new `source_kind` value for the commit stream; the enum is coordinator-owned and routes via example-doctrine-repo/central on freeze notification. | coordinator (example-doctrine-repo surface, notified at freeze) | **notify-at-freeze** — example-doctrine-repo/central notified at freeze; ships with rag surface (b), not a pre-freeze blocker |
| 3 | **claude-klabauter ships `commit.anchors` op + invocation shim + finalizes this contract** (plan AC1/AC2/AC7) → example-doctrine-repo wires the `prepare-commit-msg` hook (gated on this delivery). | claude-klabauter (op + shim) → example-doctrine-repo (hook wiring) | **SHIPPED** — `043b214` C1–C4, `9f3ad55` shim, `da13148` workstream-complete |

*Sibling EMs build against this frozen schema. Any post-freeze key-set delta surfaces through the
reader-widen path (§ 5) — no breaking change, no re-dispatch.*

---

## 1. Trailer key schema

### 1.1 Key table

Six trailer keys constitute the commit-anchor set (derived/stamped by `commit.anchors`). Slice-1
keys are described below, plus a seventh recognized-but-not-derived key (`Closes:`, see the note
directly above and § 1.2a), and an eighth, `Resolves:` (2026-08-01), which — unlike `Closes:` — IS
derived/stamped by `commit.anchors`, at the completion event only.

| Trailer key | Source | Grain | Cardinality | Durable join key? | Example value |
|-------------|--------|-------|-------------|-------------------|---------------|
| `Plan:` | plan file present in commit diff; OR active plan in read-model (D2 DD#1 freshness-gated) | display / human-legible | 0..1 | **NO** — rename-fragile path | `docs/plans/2026-07-04-claude-klabauter-commit-anchor-stamper.md` |
| `Plan-Id:` | `plan_id` frontmatter field of the same plan source | plan-grain | 0..1 | **YES** — durable `pln-…` minted id | `pln-claude-klabauter-commit-anchor-stamper-q-29b891` |
| `Deliverable-Id:` | `deliverable_id` frontmatter field (DR-207 deliverable-spine key) | deliverable-grain | 0..1 | **YES** — durable `dlv-…` minted id; cross-entity join | `dlv-claude-klabauter-commit-anchor-stamper-queryable-g-2064ae` |
| `Nature:` | EM-supplied; defaulted from commit subject-prefix taxonomy (§ 1.3) | classification facet | **0..1** (present when subject prefix is resolvable or override supplied) | **NO** — display/filter facet, not a join key | `infra` |
| `Anchor:` | nearest live handoff/pickup basename in read-model (D2 DD#1 freshness-gated) | handoff/continuity-thread | 0..1 | **NO** — human-legible breadcrumb only (§ 1.2) | `handoff/2026-07-04_strang-01-tc3` |
| `Session-Id:` | already stamped by existing `coordinator-prepare-commit-msg` hook | session-grain | **1** (always present) | **YES** — durable commit→session edge; transitive root | `<session-uuid>` |
| `Closes:` | **author-supplied** git-trailer convention — NOT derived/stamped by `commit.anchors`; consumed at emit time by the `commit_closures` section porter (§ 1.2a) | work-item reference | 0..N (per commit; multiple `Closes:` values recognized) | **YES** — but the join key is `(repo, item_id, sha)`, a write-time-per-row identity, not a single durable id (docs/plans/2026-07-17-commit-closure-emission-fact.md DECISION-4) | `RECS-42` |
| `Resolves:` | derived/stamped by `commit.anchors` — same `deliverable_id` as `Deliverable-Id:` (staged plan frontmatter), emitted ONLY when a completion entry (`archive/completed/*.md`) is ALSO in the staged diff (the completion-event gate; § 1.2b) | completion-grain (contrast `Deliverable-Id:`'s membership-grain) | **0..1 from this producer** (this op resolves at most one staged plan per commit, mirroring `Deliverable-Id:`; the `Resolves:` convention itself is 0..N-capable per `coordinator/docs/wiki/resolves-commit-trailer.md` for a hand-authored multi-artifact commit, but `commit.anchors` never emits more than one) | **YES** — durable `dlv-…` minted id; the fleet's sole ship-state oracle join (`rollup_derive.py`, unchanged by this addition) | `dlv-claude-klabauter-commit-anchor-stamper-queryable-g-2064ae` |
| `Commit-Token:` | `coordinator_core.ops.ceremony.commit_pipeline.commit()` — minted `uuid4().hex` per call, appended before the message is written to its temp file (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md, W1) | producer-internal verification token | **1** (always present on every commit `commit_pipeline.commit()` makes) | **NO** — producer-internal verification token, not a join key; matched by this call's own post-commit `git log --grep` to resolve `committed_sha` on the agree branch, never read back by any other consumer | `a1b2c3d4e5f6...` (32 hex chars) |

### 1.2 Grain and join-key semantics — critical distinctions

**`Plan:` (path) is display-only, rename-fragile.** It is the human-legible `git log` token: a reader
can see which plan this commit served. It is NOT a durable join key — plan files move (archive, rename)
and the path joins to zero current-plan rows after a move. Consumers must NOT build durable graph edges
on `Plan:`.

**`Plan-Id:` is the durable plan-grain join.** `pln-…` minted ids are stable across file moves,
renames, and archival. rag joins commit→plan on `Plan-Id:`; `Plan:` is ignored for graph construction.
Both come from the same frontmatter claude-klabauter already reads; emitting `Plan-Id:` is free. `Plan:` is
retained in the schema purely for human legibility in `git log`.

**`Deliverable-Id:` is the durable cross-entity join (DR-207 spine).** `dlv-…` is the coarser
deliverable-grain key spanning plans, commits, and sessions. It is the cross-entity join key from
DR-207's deliverable-spine initiative. Consumers join commit→deliverable on this key.

**The key is `Deliverable-Id:`, never bare `Deliverable:` (ruled 2026-07-28).** claude-klabauter has THREE
independent producers of this one FK (2026-08-10 correction — a prior revision of this paragraph
said TWO): `commit.anchors` (`coordinator_core/ops/commit_anchors.py`, staged plan frontmatter,
ceremony path), `coordinator/bin/coordinator-prepare-commit-msg` (session-shape, every commit —
Example-doctrine-repo's hook), and `coordinator_core/git/commit_trailers.py`'s `compute_missing_trailer_args`,
invoked by `coordinator_core/ops/ceremony/git_native.py::commit_scoped`'s diverged branch (which
routes through `_commit_scoped_private_index` and commits via `git commit-tree` directly). That
last path fires **no git hooks at all**, so the hook-based producer never runs there — this is
precisely why a third, independent producer exists: `commit_scoped`'s diverged branch needs its
own resolution of the same FK because it cannot rely on `coordinator-prepare-commit-msg` firing.
All three spell the key the same way today (`Deliverable-Id:`), same `dlv-…` value space, same
commit→deliverable edge. `Deliverable-Id:` wins on three counts: every consumer already reads it
(claude-klabauter's `coverage.py` DAG attribution and `execute_plan_assemble/close_out_and_stamp.py`,
Example-retrieval-repo's `workstate_store` ingest), it is 84-commits-to-4 in live history, and it matches the
`-Id`-suffix convention the sibling id-valued keys follow (`Plan-Id:`, `Session-Id:` — bare
`Plan:` is the path, not an id).

**`commit_anchors.py` is deliberately separate, not a convergence backlog.** Of the three
producers, `commit_anchors.py` already omits `Deliverable-Id:` when it cannot verify unambiguously
(resolving strictly from staged plan frontmatter) — the § 3 precision posture the other two
producers acquire only via this plan's C2 (an ambiguous multi-claim commit now omits
`Deliverable-Id:` rather than guessing; see § 3). This is not drift to be converged away: each
producer resolves the FK from a different input (staged plan frontmatter vs. session-claim state)
for a different call site, and `commit_anchors.py` already carries the precision guarantee the
plan brings to the other two.

Because `%(trailers:key=X)` is an **exact** key match and not a prefix match, the bare spelling
was not a cosmetic inconsistency: it read as empty for every consumer in the fleet and errored
nowhere. The four `Deliverable:`-stamped commits in claude-klabauter history predate the ruling; they are
still session-attributed via `coverage.py`'s no-`Deliverable-Id` legacy fallback, so no backfill
is required. New consumers MUST NOT add a bare-`Deliverable:` fallback read — there is no live
producer of it.

**`Anchor:` is a human-legible breadcrumb — NOT a durable join key.** It is handoff/pickup-grained
and tells a human `git log` reader which continuity thread the session was on. Handoffs carry no stable
minted handoff-grain id (only a rebuild-unstable surrogate; DR-207 makes `deliverable_id` the durable
spine key), so asserting `Anchor:` as a joinable key would demand a column rag has nothing durable to
fill. **rag consumes `Anchor:` as an optional display string or ignores it.** The durable intent→reality
graph is built from `Plan-Id:` / `Deliverable-Id:` / `Session-Id:`.

**`Session-Id:` is the transitive root.** Already stamped by the existing hook on every
coordinator-session commit. It is the commit→session durable edge — the anchor from which all other
commit-graph context is reachable.

**`Commit-Token:` is producer-internal — explicitly NOT a join key.** `commit_pipeline.commit()`
mints a fresh `uuid4().hex` per call and appends it as a trailer so its own post-commit
verification (a pathspec-scoped, bounded `git log --grep=<token> --fixed-strings`) can
unambiguously resolve `committed_sha` on the agree branch — no peer can ever author this exact
string, so the match is collision-free by construction (see that function's own docstring). It
exists to answer "which commit did THIS call just make", a question internal to one producer
invocation, not "which commit belongs to which plan/session/deliverable" — the question every
other durable key in this table answers. **Consumers must NOT build graph edges on it** — it
carries no cross-call or cross-entity meaning, and nothing outside `commit_pipeline.commit()`
ever reads it back.

### 1.2b `Resolves:` — completion-grain, gated on the ceremony completion event

Unlike `Closes:`, `Resolves:` IS derived and stamped by `commit.anchors` — but only at the
workstream-complete / ship-handoff ceremony's completion event, never on an ordinary mid-flight
commit. The value is the SAME `deliverable_id` already resolved for `Deliverable-Id:` (same
staged plan frontmatter, § 1.2 above) — this is not a second independent resolution path, only a
second emission gate on the same resolved value.

**The gate is an additional staged-diff signal, not a new resolution source.** `commit.anchors`
emits `Resolves: <dlv-id>` only when the staged diff ALSO contains an `archive/completed/*.md`
completion entry (the file `coordinator_core.ops.coordinator_complete_entry._write_entry`
scaffolds on the workstream-complete path) alongside the plan file that resolved `Deliverable-Id:`.
A commit carrying `Deliverable-Id:` with no staged completion entry (the ordinary case — every
commit of a workstream from its first onward) never emits `Resolves:`.

**Why this key needed a producer at all.** `Resolves:` names *completion* of a durable artifact;
`Deliverable-Id:` names *workstream membership*, stamped from a workstream's first commit onward.
Before 2026-08-01, `Resolves:` was a documented convention
(`coordinator/docs/wiki/resolves-commit-trailer.md`) that no skill, hook, or command actually
wrote — `rollup_derive.py`'s sole ship-state oracle join (`--grep=Resolves: {artifact_id}`,
exact-key-verified via `parse_resolves_trailer.py`) matched zero commits fleet-wide, so every
roadmap baton with a deliverable stranded `in_flight` regardless of actual completion. Joining the
oracle to `Deliverable-Id:` instead (the widen this contract does NOT make) would have converted
that permanent false-negative into a systematic false-positive — a deliverable would read as
"shipped" from its first pushed commit — so the fix is this producer, not a consumer-side join
change. See `docs/plans/2026-08-01-baton-spine-information-integrity.md` § Anti-scope.

### 1.2a `Closes:` — recognized, not derived; a different consumption path

Unlike the six keys above, `Closes:` is **never written by `commit.anchors`** — it is the
ordinary git-trailer convention an author already uses (`Closes: RECS-42`) to reference a work
item their commit closes. This contract registers it (§ 1.1) so the trailer key set stays a
single source of truth, but its producer/consumer wiring is distinct:

- **Producer:** the commit author (or their tooling), not `commit.anchors`.
- **Reader:** claude-klabauter's *emit-time* `commit_closures` section porter (not the commit-time
  `prepare-commit-msg` hook), which runs one bounded `git log
  --format=%(trailers:key=Closes,valueonly)` scan (house precedent: this contract's own §2.1
  extraction pattern; also `coverage.py`'s `Session-Id:` extraction) and stamps
  default-branch reachability alongside each row.
- **Emitted shape:** `coordinator_core/contract/example-retrieval-repo-producer-contract.md § 2.5`
  (`commit_closures` array, `CommitClosure` entity) — not this contract's `commit.anchors`
  op output.

See `docs/plans/2026-07-17-commit-closure-emission-fact.md` for the full design (DECISION-1
through DECISION-4).

### 1.3 Durable joins are direct minted-id edges — no transitive walk

`Session-Id:` + `Plan-Id:` + `Deliverable-Id:` are stamped as **direct** commit→{session, plan,
deliverable} edges. There is **no transitive-handoff walk** to close: consumers join directly on the
minted ids. They do NOT traverse commit → Anchor → handoff → plan.

This is strictly better than a transitive design: no walk means no false transitive edge, and no
dependency on handoff surrogate-key stability.

### 1.4 `Nature:` enum — additive-only facet

`Nature:` is a classification facet. Slice-1 enum values:

```
bugfix | infra | roadmap | refactor | docs | chore | session-op
```

The default is derived from the commit subject-prefix taxonomy already in use:

| Subject prefix | Derived default |
|----------------|-----------------|
| `fix:` | `bugfix` |
| `execute:` | `roadmap` (`infra` is override-only in slice-1) |
| `memo:` | `session-op` |
| `pickup:` | `session-op` |
| `session-init:` | `session-op` |
| *(other)* | `chore` |

The EM may override by editing the emitted `Nature:` trailer line before finalizing the commit message.
This is the operative override path: the hook derives the default; the EM edits inline.

**Extension discipline.** The `Nature:` enum is **additive-only**. New values may be added (consumer
widens before producer emits — reader-widen-before-writer-flips, § 5). Existing values are NEVER
renamed or removed without a breaking-change bump. Cockpit consumes `Nature:` as a first-class filter
facet — treat the enum as a stable, versioned vocabulary.

---

## 2. Dual-legibility — human-readable and machine-extractable

Trailers are positioned in the commit message body after the prose body and a blank line, following the
existing `Session-Id:`, `Co-Authored-By:`, and `Substrate-changes-attribution:` trailer precedents.
This positioning satisfies `git interpret-trailers` extraction rules.

### 2.1 Extraction commands

**By-key extraction (single commit):**
```bash
git log -1 --format='%(trailers:key=Plan-Id)'
git log -1 --format='%(trailers:key=Deliverable-Id)'
git log -1 --format='%(trailers:key=Nature)'
git log -1 --format='%(trailers:key=Session-Id)'
```

**Structured extraction (`git interpret-trailers`):**
```bash
git log -1 --format='%B' | git interpret-trailers --parse
```

**Range extraction for ingest (all commits, TSV output):**
```bash
git log --format='%H%x09%(trailers:key=Plan-Id,valueonly,separator=%x2C)%x09%(trailers:key=Deliverable-Id,valueonly)%x09%(trailers:key=Nature,valueonly)%x09%(trailers:key=Session-Id,valueonly)' origin/main..HEAD
```

### 2.2 Worked example — commit trailer block

A commit stamped by the `commit.anchors` op (all keys resolved):

```
execute(C4-contract): add commit-trailer producer contract (PROPOSED)

Authors the coordinator_core/contract/commit-trailer-producer-contract.md,
the PROPOSED trailer producer contract for the claude-klabauter commit-anchor stamper.
Mirrors the cockpit/rag contract shape. Freezes on co-design close.

Nature: docs
Plan: docs/plans/2026-07-04-claude-klabauter-commit-anchor-stamper.md
Plan-Id: pln-claude-klabauter-commit-anchor-stamper-q-29b891
Deliverable-Id: dlv-claude-klabauter-commit-anchor-stamper-queryable-g-2064ae
Anchor: handoff/2026-07-04_strang-01-tc3
Session-Id: <session-uuid>
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

A commit where only the minted ids and session are resolvable (`Plan:` file not in diff, `Anchor:`
multi-live-ambiguous → omitted per precision-over-recall):

```
fix: correct partition registry key collision on concurrent lookup

Plan-Id: pln-claude-klabauter-commit-anchor-stamper-q-29b891
Deliverable-Id: dlv-claude-klabauter-commit-anchor-stamper-queryable-g-2064ae
Nature: bugfix
Session-Id: <session-uuid>
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

A hotfix commit outside any plan (nothing resolvable except session and the always-present `Nature:`):

```
fix: guard against None repo_root in UDS hash derivation

Nature: bugfix
Session-Id: <session-uuid>
Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

### 2.3 Negative-spec — one trailing block, never two

**A producer must never emit a blank line inside the trailer block.** Every trailer key belongs to
the single last paragraph of the message. `%(trailers:key=...)` and `git interpret-trailers --parse`
both read *only* that last paragraph, so a block split by a blank line silently orphans every key
above the split while leaving them perfectly legible in the raw message.

This is a quiet wrong answer, not a visible break, and no consumer detects it (both confirmed
2026-08-08, `cross-repo/inbox/2026-08-08-example-retrieval-repo-em-commit-token-trailer-no-closed-key-set.md`
and `...-example-cockpit-repo-em-commit-token-trailer-no-key-enumeration-here.md`):

- **rag** — `_GIT_LOG_FORMAT` / `_parse_commit_block` return the empty string per slot; `Plan-Id:`
  and `Nature:` simply starve, with no error and no quarantine (that keys off the SHA field).
- **cockpit** — never parses a message at all; it consumes rag's typed projection, so an emptied
  `nature` lands the commit in the `unattached` bucket, reported as *unplanned work*.

The producer side is therefore the only control point on this seam. The house check is an
enumerate-and-compare over a commit range — extract by key per § 2.1, compare against the keys
present in `%B`, and treat any key visible in the body but empty by extraction as an orphan.
Same failure class as the 2026-07-28 `Deliverable:` / `Deliverable-Id:` exact-match miss, which
starved for weeks with nothing surfaced anywhere.

---

## 3. Producer precision guarantee

**A present key is a high-confidence edge. An absent key is a safe null, never a guess.**

Claude-klabauter's `commit.anchors` op resolves only what it can pin unambiguously from the live read-model:

- **`Plan:` / `Plan-Id:` / `Deliverable-Id:`** — emitted only when exactly one plan is unambiguously
  active (plan file in the diff is the self-verifying ground-truth branch; read-model-derived branch
  applies a DD#1 live-state freshness cross-check per
  `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md` (successor to tri-plane DR
  `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § DD#1`)). If multiple plans are
  live-active and the diff contains none of them, all three are omitted.
- **`Anchor:`** — emitted only when exactly one live continuity anchor (handoff or pickup) is
  resolvable. If multiple anchors are live (multi-live ambiguity), `Anchor:` is omitted.
  DD#1 freshness cross-check applies: a stale read-model index entry is confirmed against on-disk
  frontmatter before stamping.
- **`Nature:`** — emitted when resolvable: from the EM-supplied override, or from the
  commit subject-prefix taxonomy (COMMIT_EDITMSG). Omitted when neither source is available
  (e.g. no repo_root passed, or subject has no prefix pattern). Cardinality 0..1; see § 1.1.
- **`Session-Id:`** — always emitted by the existing hook; not derived by `commit.anchors`.

This guarantee is **asymmetric by design**: the precision cost of a false edge (cockpit renders a
confident wrong join) exceeds the recall cost of a missing trailer (rag projects an absent edge as
null, not an error).

**2026-08-10 addition — an ambiguous multi-claim commit now omits `Deliverable-Id:` entirely.**
`compute_missing_trailer_args` (§ 1.2, the third producer) and the `coordinator-prepare-commit-msg`
hook both acquire this posture via this plan's C2: where a commit's session holds more than one
plan claim and the FK cannot be resolved to a single `dlv-…` id, the producer omits `Deliverable-Id:`
rather than guessing. This is the same producer becoming *more* conservative that this section
already sanctions — no consumer widen is needed, since every consumer already treats an absent key
as a safe null (§ 3 above). `commit_anchors.py` already had this posture; C2 brings the other two
producers to parity with it.

---

## 4. Consumer notes

### 4.1 example-retrieval-repo (ingest)

rag ingests the trailer set from git history into its typed store. Claude-Klabauter's producer side is agnostic
to rag's ingest mechanics — the following are **rag-internal** and are NOT part of this contract:

- rag's temporal model (immutable-event, insert-once) and the "no `current_commits` resolver"
  choice — rag's own design.
- The watermark-incremental `git log` walk driver — rag's own ingest driver.
- The `(source_path, content_hash)` cache-invalidation key for rag's projection cache — specified
  in `coordinator_core/contract/example-retrieval-repo-producer-contract.md § 3` (the R5 change-signal).

**`source_kind: git_commit` (freeze-gate item 2).** rag requires a new provenance `source_kind`
value for the commit stream. The `source_kind` enum is coordinator-owned (lives in the
`artifact-shape-contract/`); example-doctrine-repo/central will be notified at contract freeze so the enum can be
extended additively. This extension follows reader-widen-before-writer-flips — rag widens its
validator to accept `git_commit` before claude-klabauter emits it.

**Join keys for rag's store:**

| Trailer | rag join role |
|---------|--------------|
| `Session-Id:` | commit→session FK |
| `Plan-Id:` | commit→plan FK (durable; prefer over `Plan:` path) |
| `Deliverable-Id:` | commit→deliverable FK (DR-207 spine) |
| `Resolves:` | commit→deliverable completion FK (same `dlv-…` value space as `Deliverable-Id:`, but completion-grain — present only on the ceremony completion-event commit; 2026-08-01 addition) |
| `Anchor:` | display string only — do NOT build a durable FK column on this |
| `Nature:` | classification facet / filter column |
| `Plan:` | display string only — do NOT build a durable FK column on this |

**rag ↔ cockpit query-surface alignment (freeze-gate item 1).** cockpit requires trailer edges
exposed via rag's `workstate-query` v8 envelope keyed on logical-repo identity. Rag's trailer-edge
query must land joinable in that envelope. This is a rag↔cockpit bilateral surface; claude-klabauter carries
the freeze flag but does not own or constrain the surface shape.

### 4.2 example-cockpit-repo (display)

Cockpit displays the intent→reality graph over **example-retrieval-repo's `workstate-query` surface** — NOT
directly from claude-klabauter or from raw git history. The tri-plane boundary is load-bearing here:
Claude-klabauter emits authoritative disk-truth; rag is the durable system-of-record and query surface.
Cockpit queries rag, not claude-klabauter.

**`Nature:` as a first-class filter facet.** cockpit consumes `Nature:` as an additive-only filter
dimension. Treat the enum vocabulary (§ 1.4) as stable and versioned — new values arrive via the
reader-widen-before-writer-flips protocol, never as silent renames. Cockpit widens its filter set
before claude-klabauter emits a new value.

---

## 5. Vendored-pin and bump protocol

### 5.1 Vendored-pin discipline

**rag and cockpit MUST vendor-pin a snapshot of this contract.** They MUST NOT reference
`coordinator_core/contract/` at live head across repo boundaries. Live-head cross-repo reference
re-introduces the silent-drift failure the vendored-pin discipline exists to prevent
(lesson `2026-07-04-vendored-pin-cross-repo-contract-not-liv.yaml`).

- **rag:** vendor the contract snapshot under `contract/upstream/commit-trailer-producer-contract.md`
  (or equivalent path in rag's repo). Record the vendored-at commit SHA and date.
- **cockpit:** vendor under `contract/upstream/commit-trailer-producer-contract.md`.

On any contract bump (post-freeze), rag and cockpit each receive a cross-repo memo identifying
the changed keys and the widen-before-flip requirement. Each consumer re-vendors the updated snapshot
after completing its widen.

### 5.2 Bump protocol (post-freeze)

All post-freeze changes follow **reader-widen-before-writer-flips**:

1. Claude-klabauter notifies rag and cockpit via cross-repo memo, naming the added/changed key and the
   effective date.
2. Each consumer widens its ingest/display (accepts the new value; old behavior unchanged for existing
   values) and replies confirming readiness.
3. Claude-klabauter begins emitting the new key/value only after both consumers confirm.

**Additive changes** (new `Nature:` enum value, new optional trailer key, relaxed cardinality):
follow the widen-before-flip protocol; no schema version bump.

**Breaking changes** (removed key, renamed key, cardinality tightened, semantics changed):
MUST be flagged as breaking in the notification memo and MUST NOT ship until both consumers
have landed their widen AND explicitly acknowledged the breaking shape. Breaking changes to a
frozen contract are expected to be extremely rare; the design intent is additive-only evolution.

---

## 6. Resolved co-design seam questions

### 6.1 Transitive vs. direct join (rag §2 — RESOLVED: direct minted-id edges)

**Resolution:** consumers join directly on `Plan-Id:` / `Deliverable-Id:` / `Session-Id:` — all
minted, unambiguous, stable keys. There is **no transitive commit → Anchor → handoff → plan walk**.
The original design considered using `Anchor:` as a join key and walking handoff→plan transitively,
but this was retired at co-design because handoffs have no stable minted id and transitive walks
produce false edges. `Anchor:` is a human-legible breadcrumb; the durable graph is direct-edge only.

### 6.2 `Anchor:` grain (rag §3 — RESOLVED: breadcrumb, not join key)

**Resolution:** `Anchor:` is handoff/pickup-grained and is demoted to a **human-legible breadcrumb
only**. It is neither redundant-with-`Deliverable-Id:` nor an orphan join key. rag consumes it as an
optional display string or ignores it. The durable rag graph has no `Anchor:` FK column.

### 6.3 `Plan-Id:` widen (rag §2 — ACCEPTED and landed in schema)

**Resolution:** `Plan:` (path) is rename-fragile; `Deliverable-Id:` is coarser than plan grain. `Plan-Id:`
(`pln-…`) is the durable plan-grain join. rag accepted this widen in co-design. Both `Plan:` and
`Plan-Id:` are in the schema; rag uses `Plan-Id:` for graph construction.

### 6.4 `Nature:` as display/filter facet (cockpit — ACCEPTED)

**Resolution:** cockpit accepted `Nature:` as a first-class filter facet. The enum is additive-only;
reader-widen-before-writer-flips governs additions. Cockpit does NOT treat `Nature:` as a join key.

### 6.5 Vendored-pin (both consumers — CONFIRMED)

**Resolution:** both rag (confirmed in co-design reply) and cockpit (confirmed in reply) acknowledged
the vendored-pin discipline. Neither will reference `coordinator_core/contract/` at live head.

---

## 7. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part of
this contract**:

- **rag's ingest implementation** — temporal model, watermark driver, `git log` walk mechanics,
  `(source_path, content_hash)` cache-invalidation implementation. Example-retrieval-repo's own design surface.
- **rag's `workstate-query` HTTP projection shapes** — the v8 surface shape, query endpoint design,
  SSE/pull mechanics. Example-retrieval-repo's output, not claude-klabauter's.
- **cockpit's display UI and query-mapping entries** — filter facets, intent→reality graph rendering,
  the single query-mapping entry cockpit adds to join rag's workstate-query surface. Example-cockpit-repo's
  own design surface.
- **The example-doctrine-repo append-hook wiring** — editing `coordinator-prepare-commit-msg` to call `commit.anchors`
  and inject the returned block. Example-doctrine-repo surface (`coordinator-prepare-commit-msg` lives in
  `example-doctrine-repo/coordinator/bin/`); claude-klabauter proposes via memo + PM-relay per
  `CLAUDE.md § Cross-repo writes`. The hook wiring is the example-doctrine-repo consultation deliverable (AC6 ratified).
- **`commit.anchors` op implementation** — handler code, DD#1 freshness cross-check mechanics,
  subject-prefix taxonomy lookup, `OP_CLASSIFICATION` and `_OP_KEY_SCOPE` registration entries.
  Implementation slices C1-op and C2-register.
- **The claude-klabauter invocation shim** (`bin/claude-klabauter-commit-anchors`) — the fail-open shim the example-doctrine-repo hook
  execs. Implementation slice C3-shim.
- **Historical backfill** — stamping trailers onto commits predating this feature. Possible follow-on;
  slice-1 is stamp-going-forward only.
- **A JSON-Schema sibling** of this contract — natural follow-on once the contract freezes and both
  consumers have confirmed their vendored pins.
- **HTTP exposure of `commit.anchors`** — COMPUTE_ONLY ops over HTTP are a pcore-09/10 concern,
  not this plan or contract.

---

<!-- producer-contract: claude-klabauter commit-anchor stamper — coordinator→git-history trailer schema.
     FROZEN (2026-07-05, v1.0) on co-design close — three gate items resolved (see § 0). -->
