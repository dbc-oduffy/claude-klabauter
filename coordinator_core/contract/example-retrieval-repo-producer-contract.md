# coordinator → example-retrieval-repo producer contract (frozen)

> **STALE AS OF 2026-08-23 — the emission artifact this contract is built around no longer exists.**
> `state/cockpit-emission.json`, `artifact.emit`, `emit.cadence` and `emission.publish` were deleted
> (DR-351). Every clause below that names the emission as an authoritative surface, a schema_version
> source, or an invocation target describes something that is gone. Nothing here has been rewritten:
> this is a contract with a sibling repo and re-cutting it unilaterally is not claude-klabauter's call. Treat
> the whole document as historical until both sides re-cut it against the `query-*` surface.
> Producer-side detail: `docs/decisions/DR-351-the-emission-is-deleted-not-halted.md`.


> **What this is.** The frozen producer-side contract that the coordinator control-plane
> engine (**claude-klabauter**) exposes to the **example-retrieval-repo** data plane. It defines exactly what
> claude-klabauter emits as authoritative disk-truth and which engine ops are observable, so that
> example-retrieval-repo can build its CQRS projection / typed query surface against a stable input
> **without further co-design**. Claude-klabauter *emits*; example-retrieval-repo *ingests and projects*.
>
> **Who consumes this.** A context-less example-retrieval-repo EM building `workstate_store` — a
> derived, rebuildable projection over claude-klabauter's disk-truth (capability-vs-custody: rag owns
> the projection mechanism, claude-klabauter holds custody of the source bytes). Everything needed to
> build the projection is
> in this file: op wire-shapes, the emission-envelope schema pin, the ProvenanceEnvelope
> vendor pin, and the R5 content-hash change-signal format.
>
> **What this is NOT.** This is the *input* example-retrieval-repo builds against — not example-retrieval-repo's
> output. It does not specify example-retrieval-repo's HTTP projection responses, its pull-vs-SSE
> mechanics, or its CQRS implementation. See § "Out of scope — not our surface".
>
> **Spec backlink.** Roadmap stub `pcore-06`, Channel B —
> `state/handoffs/2026-07-02_230106_roadmap-pcore-06.md`.
> Gates (both shipped): pcore-03 beachhead (op surface proven) + pcore-09 R5 primitive.
> Tri-plane boundary authority: `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`;
> `CLAUDE.md § Project Overview`.
>
> **Status: FROZEN — amended 2026-07-10 for DR-215 transport supersession.** Changes to this
> contract follow the reader-widen-before-writer-flips co-ratified bump protocol; example-retrieval-repo
> is notified via cross-repo memo before any breaking change.
>
> **Changelog (post-freeze corrections):**
> - **2026-07-25 (PM ruling — capability-vs-custody reframe, doc-only, no wire/version
>   change):** § "Who consumes this", § 0 boundary invariant, and § 5 out-of-scope corrected
>   from the retired "rag is the fleet system-of-record / claude-klabauter is store-less" framing to
>   capability-vs-custody: rag's `workstate_store` is a derived, rebuildable projection over
>   claude-klabauter's disk-truth, not a system-of-record claude-klabauter is store-less against; claude-klabauter holds
>   custody of its own `state/` and does not write into rag's store (dual-write ban
>   unaffected, strengthened). Spec backlink re-pointed from the superseded
>   `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md` to
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`. rag notification is
>   CONTRACTUALLY REQUIRED here under this contract's own bump protocol (not a courtesy) and
>   is discharged by two 2026-07-25 memos already in example-retrieval-repo's inbox —
>   `2026-07-25-claude-klabauter-em-pm-ruling-rag-is-capability-not-custody.md` and
>   `2026-07-25-claude-klabauter-em-workstate-store-is-projection-not-custody-correction.md` —
>   which must be read together: the second retracts the first's unqualified "rag holds no
>   bytes" overclaim. No field, schema version, or emission-wire shape changed.
> - **2026-07-24 (review-integration correction — vendored schema version reconciled to
>   3.4.0):** the two bilateral bumps narrated below (C6, C8) are two independent additive
>   events per this changelog's prose, but the vendored
>   `cross-repo-memo-summary.schema.json` `version` field only recorded ONE increment
>   (3.2.0→3.3.0, landed by C8's commit) — C6's `archived`/`decision_note` addition left the
>   field untouched. Reconciled by bumping the vendored `version` an extra step
>   (3.3.0→3.4.0) so the artifact records two increments for the two additive events this
>   prose describes, rather than silently absorbing C6's fields into C8's bump. Flagged by
>   code-reviewer, applied by review-integrator.
> - **2026-07-24 (C8 — `cross_repo_memos` full `body` content, additive minor, SECOND
>   BILATERAL BUMP, PENDING RAG ACCEPTANCE):** `cross_repo_memos` array entries now also
>   carry an optional, BOUNDED `body` string — the memo's full markdown content (frontmatter
>   stripped), not just the capped `decision_note` excerpt C6 added — so the fleet can
>   content-search memo prose, not just filter frontmatter. Widens the same bilateral seam
>   C6 opened: claude-klabauter owns this in-spine contract-text edit; **rag-EM is the accepting
>   party** for the rag-side ingestion widening (§ 2.6 below), a SECOND acceptance on top of
>   the C6 one (C6's `archived`/`decision_note` ingestion and this `body` ingestion are both
>   asked in the same first-wave cross-repo ask — see plan § Cross-repo coordination ask #2,
>   "per PM no-deferrals (C8), the ingestion widening also extends to full memo body
>   content"). Bounded/streaming-safe: `body` is capped at `_BODY_MAX_CHARS` (50k chars) and
>   truncated with a trailing ellipsis — never silently dropped — only for a pathologically
>   oversized memo; the everyday case ships the body in full. See plan
>   `docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md` § C8.
> - **2026-07-24 (C6 — `cross_repo_memos` archived rows + `decision_note`, additive minor,
>   PENDING RAG ACCEPTANCE):** `cross_repo_memos` array entries now carry an additive
>   `archived` bool (default `false`) and an optional, CAPPED `decision_note` string
>   (key-absent when the source memo carries none). Bilateral bump — same shape as the
>   cockpit-contract D13/D21 gate: claude-klabauter owns this in-spine contract-text edit; **rag-EM is
>   the accepting party** for the rag-side ingestion widening this depends on (§ 2.6 below).
>   Until rag's ingestion lands, these fields are emitted but not yet projected by rag's
>   query surface — see plan `docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md`
>   § Cross-repo coordination ask #2 (first-wave, sequenced beside this chunk).
> - **2026-07-10 (DR-215 transport supersession):** § 1 transport superseded — DR-215 retired
>   the UDS op surface; consumers invoke via cc_invoke command-type
>   (`python -m coordinator_core.invoke`); see
>   `cockpit-invoke-producer-contract.md` 2026-07-05 changelog for the parallel transport
>   story. Doc-only amendment catching this frozen contract up to already-shipped DR-215
>   reality (`docs/decisions/DR-215-coordinator-core-command-type-execution-model.md`,
>   ratified 2026-07-05, landed 2026-07-06); no op semantics changed. Amended: § 1.1 transport
>   description marked `[SUPERSEDED by DR-215]` with the current cc_invoke mechanism annotated
>   alongside; § 0 summary op-surface bullet; § 1.2 `ping`/`health` classification rows marked
>   retired; § 4 future-HTTP-surface auth paragraphs marked superseded (pcore-10/pcore-11
>   retired wholesale by DR-215, not merely provisional); § 6 item 3 Windows-transport note
>   marked moot (no socket transport left to be POSIX-only).
> - **2026-07-04 (code-reviewer integration):** ProvenanceEnvelope vendor pin corrected
>   1.10.0 → 1.11.0 (DR-207 Decision #7, accepted 2026-07-03); `ref` field updated from
>   OPTIONAL to D9 conditional (required non-null for git-backed sources; present-as-null
>   for local_fs/coordinator_artifact); hooks.* op count updated 7 → 11 (pcore-08 bookkeeping).
> - **2026-07-04 (Channel A co-design spillover):** § 1.1 socket-path derivation algorithm
>   spelled out (SHA-256 over Path.resolve()-canonicalized root, 16-hex prefix) — documents
>   existing behavior, no wire change; both rag and cockpit derive the path themselves and hit
>   the same gap.

---

## 0. Contract summary (read this first)

Example-retrieval-repo ingests from claude-klabauter across **two planes**, in priority order:

1. **The disk-truth emission artifact** — `cockpit-emission.json`, a single
   schema-versioned envelope claude-klabauter writes to disk. This is the **authoritative,
   primary** surface example-retrieval-repo projects from. It contains every work-state section
   (handoffs, backlogs, review_trail, completion_rollups, plans, roadmaps, trackers,
   health, initiatives, exec_summaries, …) plus per-section provenance. See § 2.

2. **The observable op surface** — a small set of read-only JSON-RPC ops
   (`coverage.gate`, `handoff.has_live_children`). These are the *live verdict/lineage
   compute* surface. rag MAY call them for on-demand recomputation; it MUST NOT invoke
   claude-klabauter's mutating ops. **[SUPERSEDED by DR-215 (2026-07-06)]** — the ops are still live
   logic, but the transport is no longer a Unix-domain socket; rag invokes them command-type
   via `python -m coordinator_core.invoke <op> '<params>'`. See § 1.1.

Cache-invalidation across both planes uses the **R5 content-hash change-signal** (sha256 of
source body, NOT mtime, NOT git-rev). See § 3 — this is a **HARD, non-optional** requirement.

Boundary invariant (capability-vs-custody, DR-236): **claude-klabauter holds custody of its own
disk-truth and does not write into rag's `workstate_store`.** claude-klabauter emits authoritative
disk-truth; example-retrieval-repo owns the projection capability — `workstate_store` is a derived,
rebuildable read-model over that disk-truth, plus the query surface built over it. Destroying
`workstate_store` is a cache-loss event, not a data-loss event.

---

## 1. Observable op surface

### 1.1 Transport & envelope

> **[SUPERSEDED by DR-215 (2026-07-06)]** Everything in this subsection below the notice
> block describes the **retired** resident-daemon UDS transport. DR-215
> (`docs/decisions/DR-215-coordinator-core-command-type-execution-model.md`, ratified
> 2026-07-05, landed 2026-07-06) retired claude-klabauter's resident daemon and its UDS/HTTP invoke
> surface wholesale in favor of **command-type spawn-per-call dispatch**. There is no socket
> to connect to, no socket path to derive, and no resident process to ping — the daemon and
> its liveness surface no longer exist. `coordinator_core/lifecycle.py`'s `uds_socket_path`
> is now a tombstone stub that unconditionally raises `NotImplementedError` (see
> `lifecycle.py:254-264`, "Retired by C3 — UDS transport removed").
>
> **The ops themselves are still live logic** — `coverage.gate` and
> `handoff.has_live_children` (§ 1.3, § 1.4) did not go away; only the socket/JSON-RPC
> transport wrapping them did. rag invokes them **command-type**, one spawn per call:
>
> ```
> python -m coordinator_core.invoke <op> '<json-params>' [--repo <path>]
> ```
>
> Source: `coordinator_core/invoke/__main__.py` (module docstring, "Generic in-process op
> dispatcher... no socket, no service loop"). The CLI builds the same JSON-RPC 2.0 request
> envelope described below and calls `dispatch_message` directly in-process; result/error
> shapes and error codes are unchanged (§ "Request/response envelope, JSON-RPC error codes"
> below still applies verbatim to the params/result/error bodies — only the transport that
> carries them has changed from a persistent UDS connection to a one-shot process spawn).
> `--repo` is required-or-resolved via `find_repo_root` from cwd; there is no per-repo socket
> hash because there is no socket. Exit codes: `0` success (result printed to stdout), `1`
> error (error envelope printed to stderr or stdout per the CLI's contract).
>
> See `coordinator_core/contract/cockpit-invoke-producer-contract.md`'s 2026-07-05 changelog
> entry for the parallel transport story on Channel A (cockpit's `fleet.*` surface), and this
> file's own "Changelog (post-freeze corrections)" 2026-07-10 entry above for the pointer.
>
> The remainder of this subsection is retained **verbatim, struck through in spirit, kept for
> paper-trail** — it was the transport pin from contract v1.0 through 2026-07-04 and MUST NOT
> be read as current.

~~- **Transport:** Unix-domain socket (POSIX-primary), NDJSON framing — one JSON-RPC 2.0
  request object per line, terminated by `\n`; one JSON-RPC 2.0 response object per line.
  Source: `coordinator_core/ipc.py` (D1 — "NDJSON + JSON-RPC 2.0").~~
~~- **Socket path (POSIX):** `/tmp/coordinator-svc-<uid>/<hash16-of-canonical-repo-root>.sock`
  (`coordinator_core/ipc.py:303-310`, `lifecycle.uds_socket_path`). The socket is
  per-repo-root; the hash is derived from the canonical repo root path.~~

  ~~**Socket-path derivation (pinned from `coordinator_core/lifecycle.py:158-199`):**~~
  ```
  canonical  = Path(repo_root).resolve()         # symlinks fully resolved
                                                  # /var/... → /private/var/... on macOS
  repo_hash  = sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
  sock_path  = /tmp/coordinator-svc-<uid>/<repo_hash>.sock
  ```
  ~~Canonicalization rule: `Path.resolve()` on POSIX — real path, all symlinks expanded, no
  trailing slash. The string fed to SHA-256 is `str(resolved_path)` (standard POSIX path
  string, no trailing slash). The 16-char prefix is the first 16 hex digits of the SHA-256
  hexdigest (lowercase). A consumer deriving the socket path itself (example-retrieval-repo calling ops,
  cockpit on the fleet.* channel) MUST use the identical canonicalization; a single
  missing `resolve()` call (e.g. passing a symlink path variant) produces a different hash
  and fails to connect.~~

~~- **Windows:** named-pipe/localhost stub is `TODO(D3/pcore-01)` — not part of this frozen
  contract. rag should target the POSIX UDS surface.~~
~~- **This is a UDS op surface, not an HTTP surface.** A future HTTP adapter (pcore-10/pcore-11)
  is out of scope here; do not build against an HTTP shape.~~

**Request/response envelope, JSON-RPC error codes (STILL CURRENT — unchanged by DR-215; the
transport carrying these bodies moved from UDS to command-type spawn, the bodies did not):**

**Request envelope:**
```json
{"jsonrpc":"2.0","id":<int>,"method":"<op.namespace>","params":{...}}
```
**Success response:**
```json
{"jsonrpc":"2.0","id":<int>,"result":{...}}
```
**Error response:**
```json
{"jsonrpc":"2.0","id":<int>,"error":{"code":<int>,"message":"..."}}
```
JSON-RPC error codes (from `coordinator_core/ipc.py`): `-32700` PARSE_ERROR,
`-32600` INVALID_REQUEST, `-32601` METHOD_NOT_FOUND, `-32602` INVALID_PARAMS.

Op namespace uses dotted method names (`coverage.gate`, `handoff.has_live_children`; `ping` is
retired by DR-215 — see § 1.2 below). Handlers are stateless: one request → one response; no
cross-request state.

### 1.2 Op classification (read-only vs MUTATING)

rag **observes**; it does not mutate. From `coordinator_core/ops/__init__.py`:

| op | classification | rag may call? |
|----|----------------|---------------|
| ~~`ping`~~ | ~~read-only (health)~~ **[SUPERSEDED by DR-215 — retired]** | no — op no longer exists; there is no resident daemon to ping |
| `coverage.gate` | read-only (compute) | yes — coverage-verdict source (command-type, see § 1.1) |
| `handoff.has_live_children` | read-only (query) | yes — handoff-lineage source (command-type, see § 1.1) |
| `artifact.emit` | **MUTATING** (writes `cockpit-emission.json`) | **no** |
| `backlog.record` | **MUTATING** (backlog-history recorder) | **no** |
| `hooks.*` (11 ops: 7 advisory + 4 bookkeeping) | advisory / bookkeeping-mutating | **no** |
| ~~`health`~~ | ~~read-only~~ **[SUPERSEDED by DR-215 — retired]** | no — daemon liveness op no longer applies |
<!-- Review: code-reviewer (F4) — pcore-08 added 4 bookkeeping hooks (track_touched_files, session_heartbeat, agent_completion_log, track_dispatched_agents); disk-verified coordinator_core/ops/__init__.py:25 -->
<!-- 2026-07-10 amendment: `ping`/`health` rows marked [SUPERSEDED by DR-215] — both were resident-daemon liveness ops; DR-215 retired the daemon wholesale, so there is nothing left to ping/health-check. coverage.gate and handoff.has_live_children rows annotated to point at the command-type invocation in § 1.1 — the ops themselves survive, only the transport changed. -->

**Invariant:** example-retrieval-repo never invokes `artifact.emit`, `backlog.record`, or any `hooks.*`
op. Claude-klabauter owns emission; rag consumes the emitted disk-truth. rag's only writes are to its
own store.

### 1.3 `coverage.gate` — coverage-verdict source

Source of truth: `coordinator_core/ops/coverage_gate.py` (do not paraphrase; the module
docstring is the frozen wire contract).

**method:** `"coverage.gate"`

**params** (all optional):

| param | type | meaning |
|-------|------|---------|
| `range` | str | git rev-range; flat mode; defaults to `merge-base..HEAD`. |
| `from_handoff` | str | closing handoff absolute path; enables DAG mode. |
| `scope_paths` | list[str] | path-scope for flat-mode chain_set only (asymmetric — never applied to reviewed_set). A single string is also accepted defensively. |
| `closing_session_id` | str | active Claude Code session ID of the closing handoff; enables D3 case 3 (unpublished closing handoff attribution). Falls through to git-log add-commit lookup when absent. |

**result:**

| field | type | meaning |
|-------|------|---------|
| `verdict_line` | str | frozen CLI contract line (AC11): `range=<r> chain_commits=N covered=M uncovered=K VERDICT=...`. This is the canonical coverage-verdict string. |
| `notes` | list[str] | diagnostic messages: INDETERMINATE reasons and `uncovered: <sha>` lines. |
| `exit_code` | int | `0` = COVERED/UNCOVERED, `2` = INDETERMINATE, `1` = error. |

**Exit-code contract** (AC11):
`0` COVERED-or-UNCOVERED · `2` INDETERMINATE (a halt) · `1` usage/setup error.
rag consuming a verdict MUST branch on `exit_code` and parse `verdict_line`; treat
`exit_code=2` as "no definitive verdict".

### 1.4 `handoff.has_live_children` — handoff-lineage source

Source of truth: `coordinator_core/ops/handoff_children.py`.

**method:** `"handoff.has_live_children"`

**params:**

| param | type | required | meaning |
|-------|------|----------|---------|
| `candidate` | str | **yes** | absolute or repo-relative path of the handoff to test. |
| `exclude` | list[str] | no | paths to drop from the scan set before checking. |
| `edge_kinds` | str \| list | no | CSV string or list of edge-kind names to follow. Defaults to all three: `predecessor`, `additional_predecessors`, `forked_from`. |

**result:**

| field | type | meaning |
|-------|------|---------|
| `referenced` | bool | `true` → has live children (do NOT archive); `false` → safe to archive. **ABSENT on error/indeterminate** (`exit_code=2`) — callers MUST check `exit_code` before `referenced`. |
| `children` | list[str] | sorted absolute paths that reference the candidate. |
| `live_session_count` | int | count of currently-live coordinator sessions (informational). |
| `exit_code` | int | **authoritative field:** `0`, `1`, or `2`. |
| `error` | str | set only on internal error / indeterminate (`exit_code=2`). |

**Exit-code contract:**
`0` → `referenced=true` → do-not-archive · `1` → `referenced=false` → safe-to-archive ·
`2` → error/indeterminate → **fail-closed, treat as do-not-archive**.
`referenced` is deliberately omitted on `exit_code=2` so a careless caller cannot read a
`false` (safe-to-archive) verdict from an error. **Always read `exit_code` first.**

Semantics rag should encode: "live" = present in the combined handoff index
(`state/handoffs/` + `archive/handoffs/`). Reverse-membership is **single-hop** (mirrors
`referencedBy`); the candidate's ancestors are NOT walked transitively.

**Version-stability guarantee (skew-tolerant for reads).** `handoff.has_live_children` is a
read-only query whose live-set computation is **version-stable**: a stale service and current
source resolve the same lineage answer for the same on-disk frontmatter. Consumers MAY degrade a
client-side version-skew abort to a warn-and-proceed on this op (as DoE's `--read-only-skew-degrade`
does for `--stamp-only`) without risking a wrong answer. This is a producer-side guarantee, not a
consumer assumption: **claude-klabauter owns a change-detection obligation** — any future release that alters
the live-set logic (edge-kind set, reverse-membership hop count, index-membership definition) MUST
be treated as a skew-relevant change and surfaced so consumers can re-tighten their skew guard.
The guarantee is scoped to this read-only op; it does NOT extend to the MUTATING ops, whose R8
fail-closed-on-skew contract is unchanged.

---

## 2. The disk-truth emission artifact — `cockpit-emission.json`

**This is the authoritative surface example-retrieval-repo projects from.** claude-klabauter's `artifact.emit`
op (MUTATING, claude-klabauter-owned) assembles and writes a single schema-versioned envelope to the
canonical path. rag reads that file; rag never invokes the emit op.

Source of truth: `coordinator_core/ops/emit/envelope.py` — see `_empty_skeleton()` for the
full, always-present envelope shape (the "graceful-absent" contract: every array is present,
empty when there are no records).

### 2.1 Canonical output path

`<central_state_root>/cockpit-emission.json`
(`coordinator_core/ops/emit/envelope.py:88`, `DEFAULT_OUTPUT_NAME`). `central_state_root`
is resolved through the `coordinator_state_root --central` seam. A live example lives at
`state/cockpit-emission.json` in this repo.

### 2.2 Envelope top-level shape

Every field below is always present (empty arrays / null when no records):

| field | type | notes |
|-------|------|-------|
| `schema_version` | str | e.g. `"2.3.0"`. **Pin this** — see § 2.4. |
| `emitted_at` | str (RFC3339 `…Z`) | UTC emit timestamp. |
| `emitted_by_machine` | str | emitting host slug. |
| `coordinator_roots` | list | |
| `branches` | list | |
| `handoffs` | list | handoff records (each carries `provenance`; see § 2.3). |
| `completion_rollups` | `{day: [], week: []}` | split by grain. |
| `backlogs` | `{bug: [], debt: [], improvement: []}` | split by family. |
| `review_trail` | list | review-state source data. |
| `routine_signals` | list | |
| `goals_current` | list | |
| `lessons` | list | |
| `plans` | list | |
| `cross_repo_memos` | list | |
| `roadmaps` | list | |
| `trackers` | list | |
| `health` | list | |
| `decision_guides` | list | |
| `session_hierarchies` | list | |
| `file_attributions` | list | |
| `initiatives` | list | |
| `exec_summaries` | list | |
| `commit_closures` | list | `(repo, item_id, sha)` commit-closure facts derived from `Closes:` git trailers; see § 2.5. |
| `narrative_views` | object \| null | |
| `malformed_records` | object | per-section buckets of records that failed to parse; every section key present, empty by default. Keys: `handoffs, backlogs, review_trail, coordinator_roots, plans, lessons, cross_repo_memos, roadmaps, trackers, health, decision_guides, session_hierarchies, file_attributions, initiatives, exec_summaries, commit_closures`. |

rag should treat `malformed_records` as a first-class signal (records that exist on disk but
could not be parsed) — not silently drop them.

The three ops in § 1 are *live-compute* views over the same underlying substrate that the
emission artifact snapshots: `review_trail` is the review-state source; `handoffs` +
`handoff.has_live_children` are the lineage source; `coverage.gate` is the coverage-verdict
source. rag projects the artifact for durable state and MAY call the ops for on-demand
freshness.

### 2.3 ProvenanceEnvelope sub-shape (vendored — do NOT redefine)

Each emitted work-state record carries a `provenance` object. This sub-shape is **owned by
the coordinator artifact-shape-contract**, landed at **v1.11.0** (original v1.10.0 commit
`05b60b15b` in `~/.claude`; bumped to v1.11.0 by DR-207 Decision #7 carrying the D9
ref-conditional + deliverable-spine `$defs`; source:
`cross-repo/archive/2026-07-03-provenance-envelope-landed-v1-10-0.md`).
**This producer contract references it; it does NOT redefine it.** example-retrieval-repo vendors the
`1.11.0` `artifact-shape-contract/` directory (schema JSON + DECISIONS.md travel together);
`ProvenanceEnvelope` is under `$defs.ProvenanceEnvelope`.
<!-- Review: code-reviewer (F1) — version pin updated 1.10.0→1.11.0; DR-207 Decision #7 accepted 2026-07-03 -->

Canonical shape (closed — `additionalProperties: false`):

| field | type | required | values |
|-------|------|----------|--------|
| `source_kind` | enum | **yes** | `github_graphql \| github_rest \| local_fs \| coordinator_artifact` |
| `repo` | str | **yes** | |
| `path` | str | **yes** | |
| `observed_at` | str (date-time) | **yes** | |
| `derivation` | enum | **yes** | `raw \| parsed \| rolled_up` |
| `ref` | object `{branch, sha}`, closed — or **`null`** | **conditional** (see D9 below) | REQUIRED non-null for `github_graphql`/`github_rest`; D9 present-as-null for `local_fs`/`coordinator_artifact` |

<!-- Review: code-reviewer (F2) — "OPTIONAL" replaced with D9 conditional per DR-207 Decision #7 -->

> **Canonical schema wins.** This table is a convenience summary; the canonical shape is the
> vendored `1.11.0 artifact-shape-contract/` sub-schema under `$defs.ProvenanceEnvelope`. If
> this table contradicts the vendored schema, the vendored schema wins.
> <!-- Review: code-reviewer (F5) — hedge added to prevent stale-table drift -->

**The `ref` D9 conditional (DR-207 Decision #7 — authoritative):** `ref` is always present
(never key-absent), but its value is source-kind-conditional:
- **`github_graphql` / `github_rest`** — `ref` MUST be a non-null `{branch, sha}` object
  (both fields required; `additionalProperties: false`). A git-backed envelope without a real
  ref is invalid at v1.11.0.
- **`local_fs` / `coordinator_artifact`** — `ref` MUST be `null` (D9 present-as-null, key
  present, value null). These sources carry no git ref; emitting a fake `{branch, sha}` is
  rejected by the v1.11.0 schema.

This is implemented as two `allOf if/then` clauses in the vendored `1.11.0` schema — see
DR-207 Decision #7 for the exact JSON-Schema clauses. rag validators MUST implement the
conditional (not treat `ref` as freely optional or freely required).

<!-- Review: code-reviewer (F3) — replaced single pre-v1.11.0 local_fs example (which had ref:{branch,sha},
     forbidden under D9) with two conformant examples showing both D9 shapes. -->

**Example A — git-backed source (`github_rest`): `ref` is a required non-null `{branch, sha}`.**
```json
"provenance": {
  "source_kind": "github_rest",
  "repo": "dbc-oduffy/example-retrieval-repo",
  "ref": { "branch": "main", "sha": "a1b2c3d4e5f6…" },
  "path": "state/handoffs/2026-07-01_120000_abcdef.md",
  "observed_at": "2026-07-03T12:18:48Z",
  "derivation": "parsed"
}
```

**Example B — filesystem source (`local_fs`): `ref` is D9 present-as-null.**
```json
"provenance": {
  "source_kind": "local_fs",
  "repo": "dbc-oduffy/.example-doctrine-mirror-repo",
  "ref": null,
  "path": "state/handoffs/2026-06-30_021546_12e715f3.md",
  "observed_at": "2026-07-03T12:18:48Z",
  "derivation": "parsed"
}
```

> **Pre-v1.11.0 note.** The live `state/cockpit-emission.json` was emitted by the shell-based
> emitter before D9 landed. It shows `"source_kind": "local_fs"` with a non-null `ref` — valid
> under v1.10.0's key-absent-optional shape, but non-conformant under v1.11.0. Once the
> coordinator_core Python emitter implements D9, `local_fs` records will carry `"ref": null`
> as shown in Example B.

Claude-klabauter already emits conformant provenance on git-backed sources; the D9 null-for-local_fs
correction lands with the coordinator_core Python emitter (see DR-207 Decision #7).

### 2.4 Schema-version pin (dependency)

- **Current emitted `schema_version`: `2.3.0`** (observed in `state/cockpit-emission.json`).
- **ProvenanceEnvelope vendor pin: `1.11.0`** (the artifact-shape-contract sub-shape version;
  distinct from the envelope `schema_version`; bumped from 1.10.0 by DR-207 Decision #7).
<!-- Review: code-reviewer (F1) — §2.4 pin updated 1.10.0→1.11.0 to match §2.3 and DR-207 -->

Example-retrieval-repo's projection MUST record which envelope `schema_version` it built against and
which ProvenanceEnvelope vendor version it validates against.

**Reader-first is a consumer responsibility, not a producer-side runtime hold.** For
minor/additive `schema_version` bumps, forward-compatibility is example-retrieval-repo's job: rag
tolerates a higher minor than the one it was built against and passthrough/drops unknown
additive keys (the same posture cockpit's `checkSchemaVersion` takes, and rag's own
quarantine-tolerant ingestion already assumes). Claude-klabauter does **not** hold emission open
waiting for rag to catch up on an additive bump — producers emit freely.

A hard reader-first **hold** is reserved for a genuinely breaking (MAJOR / shape-changing)
delta. As of a 2026-07-25 PM ruling
(`docs/decisions/DR-234-major-cockpit-bump-producer-side-hold-adopted.md`), that hold has two
parts, in sequence, and rag should expect both:

1. **Producer-side process hold (new, 2026-07-25).** claude-klabauter holds merge-to-main and production
   emit of the MAJOR bump until rag (and any other consumer) confirms it has widened to accept
   the new shape. This is a human/EM-owned process discipline exercised BEFORE the bump ships —
   never a runtime check inside `artifact.emit`, never an on-disk sentinel file, never an
   `EmitHeldError`. rag will not see a MAJOR emission land until it has confirmed re-vendor
   readiness back to claude-klabauter via the usual PM/cross-repo coordination channel.
2. **Consumer-side re-vendor-time gate (unchanged since 2026-07-08).** Separately, rag's own
   re-vendor tooling requires an inline `--ack-major` acknowledgment before adopting the new
   shape. This gate is unaffected by (1) — both apply, neither substitutes for the other.

There is still no on-disk sentinel file and no runtime `EmitHeldError` gating `artifact.emit` —
that mechanism was removed from `coordinator_core/ops/emit/envelope.py` in 2026-07-08 and is
NOT reinstated by the 2026-07-25 ruling; the new hold is a process discipline, not a code path.
The one guardrail this contract still relies on beyond that is the schema-declaration
placeholder-rejection probe, `validate.contract_declares_backlog_history()`, which is unrelated
to reader-first sequencing and continues to guard against emitting a schema-declared-but-
unimplemented section.

There is a named escape from the producer-side process hold: if rag cannot confirm re-vendor
readiness within a bounded, per-bump, PM-set window, the PM may rule to proceed, making the
resulting outage for rag deliberate and announced rather than incidental.

This was previously documented (2026-07-08) as a co-ratified protocol with example-retrieval-repo where
Claude-klabauter unwound the producer-side hold unilaterally as redundant with rag's quarantine-tolerant
ingestion, notified via a `consult` cross-repo memo rather than this contract asserting rag's
agreement — that MINOR/PATCH posture (producers emit freely, no hold) is UNCHANGED and still
governs non-MAJOR bumps. For MAJOR bumps specifically, the 2026-07-25 ruling above supersedes
that 2026-07-08 posture and reinstates the producer-side hold, earlier and as a process
discipline rather than a runtime one. Authority: `docs/decisions/2026-07-08-producer-emit-hold-removal-reader-first-consumer-owned.md`
(MINOR/PATCH, and the retired-runtime-mechanism history) plus
`docs/decisions/DR-234-major-cockpit-bump-producer-side-hold-adopted.md` (MAJOR, current).

### 2.5 `commit_closures` array — `CommitClosure` entity shape

**What it is.** One row per `(repo, item_id, sha)`: a commit whose `Closes:` git trailer
referenced a work item, plus whether that commit is reachable on the repo's default branch.
This is the deterministic "did a landed commit close this work item" fact cockpit's `recs-05`
(B4) code-complete auto-assert consumes — claude-klabauter emits it via the `commit_closures` section
porter; cockpit reads it from `cockpit-emission.json` (this contract's canonical surface),
never from git directly (store-less consumption).

Source: `coordinator_core/ops/emit/sections/commit_closures.py` (porter) +
`coordinator_core/contract/cockpit_schema/entities/commit_closure.py` (entity). Trailer
grammar: `coordinator_core/contract/commit-trailer-producer-contract.md § 1.1` (`Closes:` row).
Spec backlink: `pln-commit-closure-emission-fact-e-c22b04`.

| field | type | notes |
|-------|------|-------|
| `repo` | str | owner-qualified `<owner>/<repo>`; cross-entity join anchor (per-repo emission scope). |
| `coordinator_root_path` | str | additive connector key; **not** part of the logical identity. |
| `provenance` | ProvenanceEnvelope | `derivation: "parsed"`; see § 2.3. |
| `item_id` | str | work-item id recovered from the commit's `Closes:` trailer value (DECISION-2). |
| `sha` | str (40-char) | full commit SHA. |
| `reachable_on_default_branch` | bool \| null | `true`/`false` = resolved default-branch (`origin/main`) ancestry; `null` = indeterminate (fetch-unavailable / degrade case) — **never** coerced to `false`. |
| `content_hash` | `ContentHash` \| absent | R5 content-hash change-signal, sibling of `provenance`; follows the same not-yet-emitted / version-gated status as every other section — see § 3.3 open item 1. |

<!-- Review: code-reviewer (Finding 2, P2) — §2.5's field table omitted content_hash,
     contradicting §3.3's "every emitted work-state artifact/record carries a content_hash
     field" rule in the same document. Row added once CommitClosure entity gained the field
     (Finding 1). -->

**Logical identity: `(repo, item_id, sha)`** — mirrors `roadmap_dag_node.py`'s identity note.
A re-close, cherry-pick, or trailer copy-paste that lands the same `item_id` in two commits
emits **two** distinct rows (one per distinct triple); there is no cross-commit dedup at write
time (DECISION-4). Multi-commit resolution ("is item X actually closed") is cockpit's read-side
query over these rows — e.g. "any row for `(repo, item_id)` with `reachable_on_default_branch
== true`" — not a claude-klabauter write-time concern.

**Scan scope and staleness bound.** The section's `collect(ctx)` scans commits reachable from
`origin/main` plus the current branch's unmerged commits, bounded by a generous `--since`
horizon (DECISION-3). A closure whose commit predates that horizon ages out of the emission by
deliberate design — this is a bound on an unbounded-history scan, not a durable-store guarantee.

**No cockpit-side or rag-side git access.** claude-klabauter performs the one bounded `git log` scan and
the reachability check; rag/cockpit consume the emitted array only.

### 2.6 `cross_repo_memos` array — archived rows + `decision_note` + `body` (2026-07-24, C6/C8)

**What it is.** The `cross_repo_memos` array (§ 2.2) merges TWO sources into one array: the
actionable inbox set (`cross-repo/inbox/*.md`) and the terminal-flipped archived set
(`cross-repo/archive/*.md`) — previously inbox-only. Each row also carries an optional,
CAPPED excerpt of the memo's `decision_note` frontmatter field, so "what have I promised and
how was it answered" is a single feed instead of requiring a second, un-emitted corpus read
(C6). C8 (same plan, PM-directed "no deferrals") widens this again: each row ALSO carries the
memo's full `body` content (bounded), so the fleet can content-search memo prose, not just
filter on frontmatter fields — full-body emission was previously out-of-scope (D1); it is now
in-scope. DEC-2/DEC-3, plan `docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md`
§ C6/C8.

Source: `coordinator_core/ops/emit/sections/cross_repo_memos.py` (porter) +
`coordinator_core/contract/cockpit_schema/entities/cross_repo_memo_summary.py` (entity).

| field | type | notes |
|-------|------|-------|
| `archived` | bool | additive, default `false` (present on every row). `true` when sourced from `cross-repo/archive/*.md` (`records.query type=archived-memo`); `false` for the actionable inbox set (`type=cross-repo-memo`). |
| `decision_note` | str, KEY-ABSENT when unset | capped excerpt of the source memo's frontmatter `decision_note` field (`_DECISION_NOTE_MAX_CHARS = 500` chars, truncated with a trailing `…` when longer — never dropped, never raised). **Bounded, NOT full memo-body text** — a still-open ask with no disposition yet omits the key entirely (never emitted as `null`; the vendored schema's `decision_note` property has no null variant, matching the existing `content_hash`-style key-absent-when-unset convention). |
| `body` (C8) | str, KEY-ABSENT when unreadable | the memo's FULL markdown body (frontmatter block stripped) — re-read directly off disk by the porter, since `records.query` never returns body text. Bounded/streaming-safe, NOT unbounded: capped at `_BODY_MAX_CHARS` (50,000 chars) and truncated with a trailing `…` when a memo body is pathologically oversized — never silently dropped. The everyday case (a normal-sized memo) ships the body in FULL, uncapped in practice. Key-absent only on a genuine read failure (e.g. the source file vanished between the records.query scan and the porter's body-read pass), never emitted as `null`. The row's `content_hash` (§ 3) is stamped generically against the FULL source file (frontmatter + body bytes together) for every section's records, `cross_repo_memos` included — it detects that the source file's bytes changed since some other observation, which is a weaker and differently-scoped signal than "is this shown `body` truncated": a consumer cannot use it to verify truncation directly, since the hash covers frontmatter bytes this field never carries. |

**All three fields are additive-optional** (present with a default / omittable, never added
to the schema's `required` array) — no MAJOR bump, no re-vendor `--ack-major` needed; rag's
existing higher-minor tolerance covers both bumps per § 2.4's reader-first discipline.

**Ingestion is NOT yet live.** Emission (C6, then widened by C8) and rag's ingestion widening
into `current_cross_repo_memos` (rag-EM's surface) are sequenced FIRST-WAVE together, not
emit-then-v2 — see plan § Cross-repo coordination ask #2 (which explicitly extends the
ingestion ask from `decision_note` to full `body` content per the C8 "no deferrals"
direction). Until rag's ingestion lands, `archived`, `decision_note`, and `body` are present
in `cockpit-emission.json` but read empty/absent through any rag-side query surface built
before that widening ships.

---

## 3. R5 content-hash change-signal (HARD requirement — non-optional)

**Purpose:** the cache-invalidation signal rag uses to know when to re-project a record.

### 3.1 The rule: content-hash, NOT mtime, NOT git-rev

- **mtime alone is REJECTED and NOT shippable** for product-facing reads (synthesis §3 R5).
  Two writes to the same path within one filesystem-timestamp granularity (the "same-second
  mtime staleness hazard") are indistinguishable by mtime — a projection keyed on mtime would
  serve stale data. This is a hard prohibition, not a preference.
- **git-rev is also REJECTED** as the change-signal. `git rev-parse HEAD -- <path>` resolves
  only to the last *committed* SHA; it cannot see in-progress working-tree edits within the
  same timestamp granularity. The engine serves working-tree state including uncommitted
  handoff/verdict edits.
- **The shipped choice is content-hash: `sha256(file_body)` hex-digest.** It is
  working-tree-correct (no dependency on git commits), catches uncommitted edits, and
  sidesteps the same-second mtime hazard. Rationale is pinned in
  `coordinator_core/cache.py` **Design Decision D2** (cite it) and in the tri-plane DR §1
  ("correctness-critical reads key on content-hash/git-rev, not mtime"; **the shipped
  disambiguation is content-hash**, per cache.py D2).

Note on the DR wording: the tri-plane DR §1 phrases this as "content-hash/git-rev". The
**shipped implementation chose content-hash**, deliberately, for the working-tree-correctness
reason above. This contract specifies **content-hash**. Where the DR says "or git-rev", read
"content-hash is the disambiguation claude-klabauter ships".

### 3.2 The hash convention

The hash is computed exactly as the shipped internal primitive computes it
(`coordinator_core/cache.py:compute_stamp`):

```
content_hash = sha256(<raw file body bytes>).hexdigest()   # 64-char lowercase hex
```

- Input is the **raw file body bytes** of the source artifact on disk (the `.md` / `.yaml`
  record file), read whole — not a canonicalised or parsed form.
- Output is a lowercase 64-character hex string.
- This is the same convention `compute_stamp` uses to re-key `dag._read_meta`; rag SHOULD use
  the identical convention so a hash computed on either side of the boundary agrees for the
  same bytes.

### 3.3 Wire-emission of the change-signal (what claude-klabauter emits for rag to consume)

**Critical distinction the consumer must understand:** `compute_stamp` in
`coordinator_core/cache.py` is today an **INTERNAL cache primitive** — it re-keys the engine's
own frontmatter/meta read cache. It is **NOT currently a wire-emitted field** on the
`cockpit-emission.json` records (as of the live artifact, records carry `provenance` but no
content-hash field). This contract **DEFINES** the wire-level change-signal that rag consumes,
using that same sha256-content-hash convention. It is the frozen shape claude-klabauter's emitter will
carry.

**Contract:** every emitted work-state artifact/record carries a `content_hash` field so rag
can invalidate its projection cache on change. The frozen shape:

```json
"content_hash": {
  "algo": "sha256",
  "hex": "<64-char lowercase hex of the source file body>",
  "source_path": "<repo-relative path of the hashed source artifact>"
}
```

- `algo` — fixed literal `"sha256"` (reserved for future algorithm agility; rag may assert
  it equals `"sha256"`).
- `hex` — `sha256(file_body).hexdigest()` per § 3.2.
- `source_path` — the repo-relative path of the source artifact the hash was taken over
  (matches `provenance.path`), so rag can key its cache on `(source_path, hex)`.

**rag's invalidation rule:** cache each projected record under key `(source_path, hex)`.
On a new emission, if `hex` for a `source_path` differs from the cached value, re-project that
record; if unchanged, the cached projection is valid regardless of mtime. This gives rag a
correct, working-tree-aware freshness signal without recomputing projections on every emit.

A machine-readable JSON Schema for this signal ships alongside this document at
`coordinator_core/contract/change-signal.schema.json`.

> **Open design question flagged for the EM (§ 6, item 1):** the `content_hash` field is
> *defined* here but is **not yet emitted** by `coordinator_core/ops/emit/`. Claude-klabauter must wire
> `compute_stamp` (or an equivalent body-hash over `provenance.path`) into each section
> porter's record shape and bump the envelope `schema_version` (additive minor) before rag can
> consume it live. Until that lands, rag can compute the hash itself from
> `provenance.path` + the on-disk body, but the contract's intent is that claude-klabauter emits it.

---

## 4. Auth / token surface (brief — not the core of Channel B)

rag ingests **disk-truth artifacts** (`cockpit-emission.json` and the on-disk record files)
by direct filesystem read — **no token is required** for file ingestion.

> **[SUPERSEDED by DR-215 (2026-07-06)]** The paragraphs below describe a **future HTTP
> surface** (pcore-10/pcore-11) that was never built and is now itself retired wholesale by
> DR-215 (`docs/decisions/DR-215-coordinator-core-command-type-execution-model.md`
> — "pcore-10 HTTP invoke surface retired wholesale", ratified 2026-07-05). There is no HTTP
> surface, live or planned, for rag to read the ops over. **Current mechanism:** rag invokes
> `coverage.gate` / `handoff.has_live_children` command-type, in-process, via
> `python -m coordinator_core.invoke <op> '<params>'` (§ 1.1) — no network layer, no token
> presented over the wire for this local spawn path. File ingestion (this section's opening
> sentence) is unaffected and remains a direct filesystem read with no token. The text below
> is retained for paper-trail only.

~~If rag ever reads the ops (§ 1) over a **future HTTP surface**, it uses a **READ_ONLY** token.
pcore-09 R6 shipped a two-tier token model (`coordinator_core/authz/token.py`) consuming
pcore-05's DR-208 authz contract:~~

- ~~**READ_WRITE** (privileged) and **READ_ONLY** (install-time) tiers.~~
- ~~Tokens **rotate on every service start**; a consumer MUST re-read the token file on every
  connect and MUST NOT cache a token across a possible restart.~~
- ~~There is **no anonymous tier** — absent/invalid token → denied at the HTTP layer (even
  `ping`). This applies only to a future HTTP surface; the UDS op surface and file ingestion
  are not gated by it in this contract.~~

rag never needs a READ_WRITE token: it observes, never mutates. ~~This surface is provisional
for a future HTTP adapter (pcore-10/pcore-11) and is included only so rag knows the tier to
request if/when that surface exists.~~ **[SUPERSEDED by DR-215]** — that HTTP adapter
(pcore-10/pcore-11) is retired wholesale, not merely provisional; there is no tier to request.
See the supersession note above.

---

## 5. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part of
this contract** and are **example-retrieval-repo's own decisions**:

- **example-retrieval-repo's HTTP projection response shapes** — `GET /v1/coverage`,
  `/v1/handoffs/{id}/lineage`, `/v1/review-state`, and any other projection endpoint. This
  contract is the *input* rag builds against, not rag's output.
- **Pull-vs-SSE / push mechanics** for rag's query surface — rag's decision.
- **The CQRS projection implementation** itself (the example-initiative/tc-5 lineage; supersedes the
  pre-beachhead `2026-07-02-pcore-08-readmodel-ownership.md` memo) — rag's build.
- **Channel A — example-cockpit-repo invoke-envelope** — a deferred-with-seam, NOT built now and
  NOT part of this contract. Cockpit is store-less and routes verbs to the authoritative
  plane; claude-klabauter is one outward target, not an action bus. Do not couple rag's projection to
  Channel A.
- **A claude-klabauter-side HTTP invoke surface / CQRS HTTP adapter** — pcore-10 (internal) /
  pcore-11 (cockpit-facing), both downstream; not this contract.
- **The dual-write ban.** `workstate_store` is rag's projection, built and owned by rag's
  capability code; claude-klabauter MUST NOT write into it. Claude-klabauter emits disk-truth and holds custody
  of its own `state/`; claude-klabauter does NOT own the store or the cockpit-facing query surface
  built over it (capability-vs-custody, `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`).

---

## 6. Open design questions flagged for the claude-klabauter EM

1. **`content_hash` is defined but not yet emitted.** `compute_stamp` is an internal cache
   primitive (`cache.py`), not a wire field on `cockpit-emission.json` records. Wiring it into
   each section porter + an additive `schema_version` bump is follow-on claude-klabauter engine work
   (a natural pcore-10/pcore-11-era task, or a small dedicated slice). Until then rag computes
   the hash itself from `provenance.path`. **This contract freezes the *shape*; the *emission*
   is an open claude-klabauter task.** Recommend surfacing to the PM as a small follow-on.
2. **DR §1 wording "content-hash/git-rev".** The DR leaves both open; the shipped choice is
   content-hash (cache.py D2). This contract commits to content-hash. If a future
   correctness-critical read needs a committed-SHA anchor, `provenance.ref.sha` already carries
   it (optional) — but it is NOT the change-signal.
3. ~~**Windows transport.** The UDS op surface is POSIX-only today (`TODO(D3/pcore-01)`). If rag
   must run on Windows against the ops, that stub is unbuilt. File ingestion is unaffected.~~
   **[SUPERSEDED by DR-215]** — moot: there is no UDS/socket transport left to be POSIX-only.
   Command-type invocation (`python -m coordinator_core.invoke`, § 1.1) is a plain process
   spawn and is platform-portable in the same way any Python CLI invocation is; it carries no
   POSIX-socket dependency. File ingestion remains unaffected, as before.

---

<!-- producer-contract: pcore-06 Channel B — coordinator→example-retrieval-repo. Frozen post pcore-03 + pcore-09. Amended 2026-07-10 for DR-215 transport supersession (UDS retired, cc_invoke command-type is current). -->
