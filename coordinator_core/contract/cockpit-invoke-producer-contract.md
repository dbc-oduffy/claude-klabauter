# coordinator → example-cockpit-repo fleet.* invoke-envelope producer contract (FROZEN)

> **STALE AS OF 2026-08-23 — the emission artifact this contract is built around no longer exists.**
> `state/cockpit-emission.json`, `artifact.emit`, `emit.cadence` and `emission.publish` were deleted
> (DR-351). Every clause below that names the emission as an authoritative surface, a schema_version
> source, or an invocation target describes something that is gone. Nothing here has been rewritten:
> this is a contract with a sibling repo and re-cutting it unilaterally is not claude-klabauter's call. Treat
> the whole document as historical until both sides re-cut it against the `query-*` surface.
> Producer-side detail: `docs/decisions/DR-351-the-emission-is-deleted-not-halted.md`.


> **What this is.** The frozen producer-side contract of the `fleet.*` mutating invoke
> envelope that the coordinator control-plane engine (**claude-klabauter**) exposes to
> **example-cockpit-repo**. It defines the op namespace, per-op request/response wire shapes,
> and the four resolved co-design seam questions so that cockpit can build its `/fleet`
> connector, Next.js route, and WS2 UI against a stable wire contract.
> claude-klabauter *acts*; cockpit *triggers and displays*.
>
> **Who consumes this.** A context-less cockpit EM building the `/fleet` connector +
> Next.js route + WS2 dashboard UI. Everything needed to implement against the wire envelope is
> in this file: transport reference, op wire-shapes with resolved seam questions, mode
> enum extension point, auth surface, and the resolved synchronous execution model.
>
> **What this is NOT.** This is the *producer contract* — not cockpit's consumer
> side. It does not specify the cockpit connector implementation, the Next.js route shape,
> or the WS2 UI. It is NOT the op implementation (no handler code, no registry entries,
> no classification entries). See § "Out of scope — not our surface".
>
> **Status: FROZEN — amended 2026-07-05 for global-multiplex transport (reader-widen).**
> (co-design complete 2026-07-04 — example-cockpit-repo EM accepted all four seams + wire shape;
> Delta 1 candidates[] fields pinned; §6 resolved synchronous; amended 2026-07-05 to
> reconcile with shipped global-multiplex transport.) **Contract version: 1.1.**
> Changes to this contract follow the reader-widen-before-writer-flips co-ratified bump
> protocol; example-cockpit-repo is notified via cross-repo memo before any breaking change.
>
> **Changelog (post-freeze):**
> - **2026-07-25 (citation re-point):** Tri-plane boundary spec-backlink and the § 0
>   cockpit-facing-query-surface citation re-pointed to
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`, the ratified successor
>   authority for `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md`; the 2026-07-03
>   citation is retained alongside as the historical source. Prose/citation-only; no op, wire
>   shape, or seam resolution changed, so this does not reopen the FROZEN v1.1 gate.
> - **2026-07-05 (v1.1 amendment):** reconcile with shipped global-multiplex
>   transport (coordinator-core-global-multiplex-migration workstream, C1a→C9, HEAD `fb0bdeb`).
>   <!-- Review: the Staff Engineer F0 — replaced efcab05 (memo-sweep, zero coordinator_core files) with fb0bdeb (C9 closeout, pinned by trigger memo); F3 — reworded change-class claim to distinguish axes honestly -->
>   Three transport changes: (1) socket is now global —
>   `/tmp/coordinator-svc-<uid>/coordinator.sock`, not per-repo hash-based (§1.1); (2)
>   `_origin_worktree` is now a mandatory top-level envelope field selecting the target
>   repo/partition (§1.1); (3) UDS is no longer ungated — a valid READ_WRITE `_auth_token`
>   is required on every request including `ping` (§5.1). Result/params schemas are unchanged
>   (no wire re-bump). However, the request envelope gains two mandatory fields
>   (`_origin_worktree` and `_auth_token`) — a breaking change for any shipped request-writer,
>   safe here because no cockpit connector has shipped (confirmed by the trigger memo) and
>   this amendment is co-ratified via that memo. Source: inbound memo
>   `cross-repo/inbox/2026-07-05-global-multiplex-superseded-frozen-cockpit-transport.md`.
> - **2026-07-04 (FROZEN, v1.0):** co-design complete — cockpit accepted all four seams + wire
>   shape; Delta 1 candidates[] fields pinned; §6 execution model resolved SYNCHRONOUS
>   (async job-handle shape reserved for future long-running ops); claude-klabauter-internal execution
>   mechanics (substrate-write boundary DR, commit-ownership, async-subprocess) tracked
>   separately and do not reshape the wire. pcore-11 unblocked.
>
> **Spec backlinks.**
> - Plan: `docs/plans/2026-07-04-pcore-06-channel-a-fleet-invoke-envelope-codesign.md`
> - Held handoff (Channel A gate): `state/handoffs/2026-07-04_160343_pcore-06-channel-a-cockpit-invoke-envelope.md`
> - Inbound co-design memo: `cross-repo/inbox/2026-07-04-2026-07-04-cockpit-fleet-invoke-envelope-codesign.md`
> - Amendment trigger: `cross-repo/inbox/2026-07-05-global-multiplex-superseded-frozen-cockpit-transport.md`
> - Global-multiplex DR: `docs/decisions/2026-07-04-coordinator-core-global-multiplex-topology.md`
> - Global-multiplex migration plan: `docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md`
> - Auth model: `docs/decisions/DR-208-invoke-op-authz-model.md`
> - Tri-plane boundary: `docs/decisions/2026-07-03-tri-plane-ownership-boundary.md` (superseded
>   on read-model ownership / dual-write ban by
>   `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`)
> - Substrate-write boundary ratification: `docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`

---

## 0. Contract summary (read this first)

Cockpit triggers claude-klabauter's `fleet.*` ops via the **Unix-domain socket** (UDS) + JSON-RPC 2.0
channel. Three slice-1 ops expose existing coordinator sweep behavior behind a wire envelope:

| op | what it exposes |
|----|----------------|
| `fleet.archive_completed_plans` | `cs_sweep_terminal_plans` — git-mv terminal plans into `archive/` |
| `fleet.archive_completed_handoffs` | `coordinator-handoff-archive.sh` — git-mv consumed/childless handoffs into `archive/` |
| `fleet.prune_closed_bugs` | bug-backlog closure — git-mv closed bug entries into `archive/` |

All three are **MUTATING** ops. They follow a two-call confirm→act flow:

1. `dry_run:true` — compute and return the candidate set (the human-readable preview cockpit
   presents for confirmation). Mutates nothing.
2. `dry_run:false` + `candidate_ids:[...]` — act on the human-confirmed subset, re-verifying
   terminality at act-time, reporting `acted[]/skipped[]/failed[]` per-item.

**Boundary invariant:** `dry_run:true` is the mutation op's own act-scoped preview — it is
NOT a general-purpose query surface. Cockpit's steady-state `/fleet` state display comes from
**example-retrieval-repo's typed query surface**, not from polling claude-klabauter's `dry_run`. Claude-klabauter does NOT
own the cockpit-facing query surface (capability-vs-custody boundary;
`docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`, successor to
`docs/decisions/2026-07-03-tri-plane-ownership-boundary.md`; `CLAUDE.md § Project Overview`).

**Execution model for `dry_run:false` — RATIFIED SYNCHRONOUS (DR-211).** The claude-klabauter-side
execution model — handler mutates synchronously within the request, atomic scoped-pathspec
commit (`git add -- <exact archived paths> && git commit -- <same>`), asyncio-awaited git
subprocesses — is ratified per `docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`.
The op returns `{acted[], skipped[], failed[]}` in a single round-trip. This is correct for
slice-1's bounded, human-confirmed, sub-second archival ops. The async job-handle shape
(`{job_id, status: "pending"}` + `fleet.status(job_id)`) is RESERVED for future long-running
fleet ops and may be added additively without a wire re-bump (§ 6).

---

## 1. Transport

### 1.1 Transport & envelope

The `fleet.*` ops use the **identical transport as Channel B** (coordinator → example-retrieval-repo).
This section is the authoritative Channel-A pin; the Channel-B frozen contract
(`coordinator_core/contract/example-retrieval-repo-producer-contract.md § 1.1`) carries the same
shape — do NOT invent a second transport.

- **Transport:** Unix-domain socket (POSIX-primary), NDJSON framing — one JSON-RPC 2.0
  request object per line, terminated by `\n`; one JSON-RPC 2.0 response object per line.
  Source: `coordinator_core/ipc.py` (NDJSON + JSON-RPC 2.0).
- **Socket path (POSIX):**
  `/tmp/coordinator-svc-<uid>/coordinator.sock` (global — one socket per machine per uid)

  **Source: `coordinator_core/lifecycle.py:310-366` (`global_socket_path()`)**

  Production default (`COORDINATOR_SVC_ROOT` unset):
  `/tmp/coordinator-svc-<uid>/coordinator.sock`

  Injectable override (`COORDINATOR_SVC_ROOT` set):
  `$COORDINATOR_SVC_ROOT/coordinator.sock`
  `COORDINATOR_SVC_ROOT` MUST be short: the resulting socket path must fit in 103 bytes
  (macOS `AF_UNIX sun_path` limit; the daemon raises `RuntimeError` with a clear diagnostic
  if it would exceed 103 bytes — `lifecycle.py:362-367`).

  The parent directory `coordinator-svc-<uid>/` is mode `0700` (user-only security boundary).

  **RETIRED: per-repo hash socket.** The former
  `/tmp/coordinator-svc-<uid>/<hash16-of-canonical-repo-root>.sock` derivation algorithm
  (previously pinned from `lifecycle.py:158-199`) is **RETIRED**. The service now exposes
  exactly **one** socket per machine per uid; it does NOT encode the repo.

  **Negative-spec (source: `coordinator_core/ipc.py:801-802`, `socket_path()` docstring):**
  - Do NOT pass `repo_root` to the socket-path helper — the socket is machine-global (AC-1).
  - Do NOT return `sentinel_dir(repo_root)/'sock'` — path length hazard (macOS;
    `AF_UNIX sun_path` 103-byte limit; source: `ipc.py:802`) and the single global path is
    the correct address.
    <!-- Review: the Staff Engineer F6 — restored source reason "path length hazard (macOS)" from ipc.py:802 docstring; prior text silently rewrote the justification -->

  Repo selection has moved to the mandatory `_origin_worktree` envelope field — see
  "Mandatory top-level envelope fields" subsection below.

- **Windows:** named-pipe/localhost stub is `TODO(D3/pcore-01)` — not part of this frozen
  contract. Cockpit should target the POSIX UDS surface for slice 1.
- **This is a UDS op surface, not an HTTP surface.** The HTTP invoke surface (pcore-10/pcore-11)
  and the single-writer queue (pcore-05) are downstream; fleet.* ops on HTTP are a later slice
  (§ 5).

**Request envelope (full form — both mandatory envelope fields required for `fleet.*`):**
```json
{"jsonrpc":"2.0","id":<int>,"_origin_worktree":"/abs/worktree","_auth_token":"<64hex>","method":"fleet.<verb>","params":{...}}
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

Op namespace uses dotted method names. Handlers are stateless: one request line → one
response line; no cross-request state.

#### Mandatory top-level envelope fields (global-multiplex transport)

Two additional fields are **mandatory** on every `fleet.*` request — both are top-level
JSON-RPC envelope siblings of `jsonrpc`/`method`/`id`/`params` (NOT nested in `params`).

##### `_origin_worktree` — repo/partition selector

| | |
|---|---|
| Field name | `_origin_worktree` (constant `_ORIGIN_WORKTREE_FIELD`, `ipc.py:157`) |
| Type | string — canonical absolute path of the originating worktree toplevel |
| Required for | **all** `fleet.*` ops (`common_dir`-scoped per `_OP_KEY_SCOPE` table, `ipc.py:209-211`) |
| Error on absent/empty/non-string | `INVALID_PARAMS (-32602)` — `"Missing required routing key: op '<method>' (scope='common_dir') requires _origin_worktree but it was absent or not a valid string"` (pattern — `resolve_op_repo_key`, `ipc.py:244-287`; checked before auth) |
<!-- Review: the Staff Engineer F2/F5 — corrected message to real prefixed form (ipc.py:274/496); F5 noted inconsistent fidelity vs exact _auth_token quotes; engine pre-auth guard now fires -32602 BEFORE auth for fleet.* missing this field -->

**Value to pass:** the canonical absolute toplevel of the originating worktree —
`git rev-parse --show-toplevel`, then `Path(result).resolve()` (all symlinks expanded;
`/var/...` → `/private/var/...` on macOS).

**Server-side resolution** (`resolve_request_repo`, `ipc.py:294-313`): `Path(raw).resolve()`
— all symlinks expanded. Absent / empty / non-string → `None` → structured fail-loud error
(`INVALID_PARAMS`) for `fleet.*` ops.

The engine internally chooses `--git-common-dir` vs `--show-toplevel` per the op's scope
(`common_dir` for all three `fleet.*` ops). The consumer **always passes the literal worktree
toplevel**; internal scope-routing is the engine's responsibility.

##### `_auth_token` — per-partition bearer token

| | |
|---|---|
| Field name | `_auth_token` (top-level envelope, option (b) ratified — `ipc.py:644-647`) |
| Type | string — 64 hex chars (READ_WRITE token) |
| Required for | **every** request including `ping` (DR-208 Invariant 3: no anonymous tier) |
| Missing/empty | `INVALID_REQUEST (-32600)` — "missing bearer token (_auth_token)" (`ipc.py:648-650`) |
| Invalid | `INVALID_REQUEST (-32600)` — "invalid token" (`ipc.py:673-677`) |

**Token file location** (`token.py:96-101`, sentinel-dir resolution at `token.py:45-73`):

```
<git-common-dir>/coordinator-service/token
```

where `<git-common-dir>` = `git rev-parse --git-common-dir` resolved absolute
(`<repo>/.git` for a regular repo; main worktree's `.git` for a linked worktree).
The READ_ONLY sibling is `token.ro` (same directory). Both files are `0600` (user-only).

**Token tier for `fleet.*`:** READ_WRITE (MUTATING ops). The engine validates the presented
token against the partition keyed by the resolved `_origin_worktree` (git-common-dir).

**Rotation and re-read discipline** (source: `coordinator_core/authz/token.py` module
docstring; `write_tokens`, `token.py:116`):
- Tokens are **regenerated on every service start** — rotation = revocation (DR-208 §7).
- cockpit MUST **re-read the token file on every connect**.
- cockpit MUST NOT cache a token across a possible service restart.
- On a `-32600 "invalid token"` response, re-read the token file and retry once (the service
  may have rotated tokens on restart) before surfacing a hard auth failure.
  <!-- Review: the Staff Engineer F4 — mid-session rotation recovery step; stale token after service restart is indistinguishable from bad token without the retry; reconnect + re-read is the correct recovery shape -->

This is the same discipline §5.2 already required for the HTTP surface — the change is that
it now applies to the UDS path too.

#### Error taxonomy — branch on JSON-RPC code, NOT message string

<!-- Review: the Staff Engineer F2 — full distinguishable taxonomy added; engine-state change since review landed pre-auth routing-key guard so all codes below are now emitted in the documented order; F5 — "branch on code" note added -->

**IMPORTANT: Consumers MUST branch on the JSON-RPC `error.code`, not on `error.message`.** Message strings are informational and may include interpolated values (method name, scope) that vary by call — they are NOT stable branching keys.

| `error.code` | Condition | Example `error.message` | Check order |
|---|---|---|---|
| `-32602` (`INVALID_PARAMS`) | `_origin_worktree` absent/empty/non-string for a key-requiring op | `"Missing required routing key: op 'fleet.archive_completed_plans' (scope='common_dir') requires _origin_worktree but it was absent or not a valid string"` | 1st — before auth |
| `-32600` (`INVALID_REQUEST`) | `_auth_token` missing/empty | `"missing bearer token (_auth_token)"` | 2nd |
| `-32600` (`INVALID_REQUEST`) | `_auth_token` invalid or rotated | `"invalid token"` | 2nd |
| `-32601` (`METHOD_NOT_FOUND`) | READ_ONLY token presented for a MUTATING `fleet.*` op | `"insufficient scope: READ_WRITE token required for mutating op"` | After auth |
| `-32601` (`METHOD_NOT_FOUND`) | Op not classified (fail-closed policy) | `"unclassified op -- denied fail-closed"` | After auth |

The `-32602` routing-key check fires **before** auth: a request missing `_origin_worktree` reaches the pre-auth guard (not the dispatch gate) and returns `-32602` immediately, before any token validation occurs. This means `-32602` and `-32600` are distinguishable — a `-32602` always means missing/malformed routing key, not a token problem.

### 1.2 Op classification (MUTATING)

fleet.* ops are **MUTATING** (git-mv archival). Cockpit **triggers**; it does not observe
the op surface for read-only queries (use rag's query surface for that).

| op | classification | cockpit calls? |
|----|----------------|----------------|
| `fleet.archive_completed_plans` | **MUTATING** | yes — on human confirm |
| `fleet.archive_completed_handoffs` | **MUTATING** | yes — on human confirm |
| `fleet.prune_closed_bugs` | **MUTATING** | yes — on human confirm |
| any other claude-klabauter op | read-only or MUTATING | **no** — cockpit calls only fleet.* |

**HTTP admission invariant.** `coordinator_core/invoke/dispatch.py` gate 6
(`requires_single_writer_queue`) already returns `403` for any MUTATING op over HTTP until
pcore-05's single-writer queue is implemented. This means fleet.* ops are **already kept off
the HTTP path by the existing gate** — slice 1 ships mutation over UDS only (D4). The
"fleet.* over UDS is admissible" argument does not generalize to "mutation over HTTP is
admissible" — those remain gated on pcore-05 + DR-208.

---

## 2. Per-op wire envelope

All three slice-1 ops share one params/result shape. The op is identified by `method`.

### 2.1 Shared params/result schema

**Params:**

```json
{
  "mode":          "already-terminal",
  "dry_run":       <bool>,
  "candidate_ids": [<str>] | null,
  "repo_root":     <str> | null
}
```

| param | type | required | notes |
|-------|------|----------|-------|
| `mode` | str (enum) | **yes** | `"already-terminal"` is the only slice-A value. See § 4 for the extension point. |
| `dry_run` | bool | **yes** | `true` → compute candidates, mutate nothing. `false` → act on `candidate_ids`. |
| `candidate_ids` | list[str] \| null | **required on `dry_run:false`** | The human-confirmed subset from a prior `dry_run:true` call. Absent or empty on `dry_run:false` → `exit_code:1` setup-error. Null / omit on `dry_run:true`. |
| `repo_root` | str \| null | optional | Consistency check only — see § 3.3. NOT a repo selector. |

**Result:**

```json
{
  "exit_code":  <int>,
  "mode":       <str>,
  "dry_run":    <bool>,
  "candidates": [
    {
      "id":             "<repo-relative source path>",
      "title":          "<human label>",
      "status":         "<terminal status>",
      "family":         "<plan|handoff|bug>",
      "terminal_since": "<RFC3339 | null>",
      "note":           "<short evidence string | null>"
    }
  ],
  "acted":   [{ "id": <str>, "archived": true }],
  "skipped": [{ "id": <str>, "reason": <str> }],
  "failed":  [{ "id": <str>, "reason": <str> }]
}
```

| field | type | present when | notes |
|-------|------|-------------|-------|
| `exit_code` | int | always | `0` clean · `2` DETERMINATE-PARTIAL · `1` setup-error. See § 3.2 for the full exit-code contract. |
| `mode` | str | always | Echoed from params. |
| `dry_run` | bool | always | Echoed from params. |
| `candidates` | list | `dry_run:true` | The preview set the human confirms. Each item: `id` (repo-relative source path; matches `provenance.path`), `title` (human label: plan title / handoff heading / bug summary), `status` (terminal status string — for handoffs, `archive_handoffs.py` Branch-A hard-codes the literal `"consumed"` sentinel here regardless of whether the record's own frontmatter says `status: consumed` or `status: claimed`; DR-084 P4 (2026-07-22) narrowed the frontmatter vocabulary but this wire value is legacy display wording retained deliberately — changing an existing value's shape is a compatibility risk this field's producer declined to take, see `coordinator_core/ops/fleet/archive_handoffs.py` `~:965`), `family` (`plan\|handoff\|bug`), `terminal_since` (RFC3339 or null — null acceptable when expensive to source for a family; age drives the human's confirm, so degrade gracefully), `note` (handoff-only short evidence string e.g. `"consumed; no live children; no live claim"`; null/omit for plans+bugs). These are generic display fields — NOT claude-klabauter frontmatter keys. Do not couple cockpit to claude-klabauter internals. |
| `acted` | list | `dry_run:false` | Items successfully archived. Shape: `{id, archived: true}`. `id` is the stable repo-relative source path — the correlation and reversal handle. |
| `skipped` | list | `dry_run:false` | Items that were terminal at preview (T1) but no longer terminal at act-time re-verify (T3). NOT a failure — see § 3.1. |
| `failed` | list | `dry_run:false` | Per-item mutation failures (lock, dirty tree, permission, git-mv conflict). See § 3.2. |

**`id` convention.** `id` is the **repo-relative source path** of the artifact (e.g.
`state/handoffs/2026-07-01_120000_abcdef.md`). It matches the Channel-B
`provenance.path` convention so cockpit can correlate fleet op results against rag's
projection. `from_path` is redundant with `id` and is NOT in the contract. `to_path`
(the archive/ destination) is claude-klabauter-internal layout and MUST NOT appear in the contract —
reversal routes by `id`, not `to_path`.

### 2.2 Op-specific notes

All three ops use the shared schema above. The only behavioral difference is the
terminality predicate the op applies at T1 (preview) and T3 (act-time re-verify):

| op | terminality predicate (T1 and T3) |
|----|----------------------------------|
| `fleet.archive_completed_plans` | `status ∈ {implemented, superseded, abandoned}` (frontmatter, static field — low drift) |
| `fleet.archive_completed_handoffs` | (`status == claimed`, dual-tolerant fallback to the archived-schema grandfather `consumed` **AND** `deployment_state != in_flight`) **OR** (`deployment_state ∈ {shipped, abandoned, continued, closed}`, `shipped` additionally requiring a resolvable `shipped_in`; `abandoned` is retired from the active schema's enum but retained in the terminal-deployment-state set for archived-corpus tolerance) — **AND**, for either branch, `has_live_children == false` **AND** no live session claim (time-varying — see § 3.1). This is the FRONTMATTER-condition predicate; the wire `status` display value stays the literal `"consumed"` sentinel regardless (§2.1). Widened 2026-07-13 at cockpit-em's request (memo) to stop stranding off-baton `active`+`shipped`/`abandoned` handoffs; wire SHAPE unchanged, only the candidate population widens. DR-084 P4 (2026-07-22) narrowed the frontmatter status vocabulary and added `continued`/`closed` as `abandoned`'s successors — see `coordinator_core/ops/fleet/archive_handoffs.py` `_is_terminal` (~:484) for the governing implementation. |
| `fleet.prune_closed_bugs` | `status == closed` (frontmatter/queue field, static — low drift) |

---

## 3. Resolved co-design seam questions (D1–D4)

### 3.1 Act-time terminality re-verification (D1 — memo Q1: TOCTOU)

**Decision: accept cockpit's proposal.** The `dry_run:false` path re-verifies each item's
terminality **at act-time (T3)** against the predicate in § 2.2 — it does NOT act on the
T1 snapshot alone. Items that have drifted out of terminal state between T1 and T3 are
**skipped + reported** (`skipped: [{id, reason}]`); they are never mutated.

- **`candidate_ids` is required on `dry_run:false`.** The caller MUST supply the explicit
  confirmed list. Absent or empty `candidate_ids` on a `dry_run:false` call → `exit_code:1`
  setup-error. The op does NOT fall back to "act on all current candidates" — that would erase
  the human's confirmation boundary.
- **Handoff re-verification** is the time-varying case: (`status == claimed`, dual-tolerant
  fallback to the archived-schema grandfather `consumed`, **AND** `deployment_state !=
  in_flight`) **OR** (`deployment_state ∈ {shipped, abandoned, continued, closed}`, `shipped`
  additionally requiring a resolvable `shipped_in`) — **AND**, for either branch,
  `has_live_children == false` **AND** no live session claim. As with § 2.2's table, this is the
  frontmatter condition; the wire `status` display value is unaffected (§2.1). The op re-runs
  the existing `handoff.has_live_children` op logic + `cs_claim_holder_live` at T3. A handoff
  terminal at preview but live-again at confirm is skipped with `reason: "re-live"`. This reuses
  the Channel-B lineage op (`coordinator_core/ops/handoff_children.py`) — no new liveness
  primitive is introduced.
- **Idempotent replay.** Re-issuing the same `candidate_ids` after a partial failure
  re-attempts only the still-terminal, still-unarchived items. Already-archived items fall out
  via T3 re-verify as a no-op skip. Cockpit's "retry failed" UX is a safe re-call, not a
  special path.

**Residual TOCTOU window (named honestly).** Act-time re-verify at T3 **narrows** the TOCTOU
window to the re-verify→git-mv gap — it does NOT close it. Absent the single-writer queue
(pcore-05), there is no lock covering that gap: a concurrent session can `/pickup` a handoff
or create a live child between T3 re-verify and the actual git-mv. This residual is
**tolerated** for slice A for three compounding reasons:

- (a) The window is the re-verify→mutate gap — sub-millisecond in practice; no network
  round-trip, no IPC hop.
- (b) Archival is git-reversible — any mistaken archive can be undone with a scoped git-mv
  back.
- (c) The handoff liveness index spans both `state/handoffs/` AND `archive/handoffs/`
  (Channel-B §1.4), so a late child spawned after archival still resolves its predecessor
  from `archive/` — lineage is NOT orphaned by the archive operation.

Full TOCTOU closure arrives with pcore-05's single-writer queue.

### 3.2 Partial-failure and atomicity contract (D2 — memo Q2)

**Decision: best-effort-with-report** (correct for idempotent + reversible archival — a
failed item 3 of 10 does not invalidate items 1–2, and every action is git-reversible).
The op is NOT all-or-nothing.

- `acted[]` = items successfully archived (shape: `{id, archived: true}`).
- `failed: [{id, reason}]` = per-item failures (lock, dirty tree, permission, git-mv
  conflict) — for cockpit UX to render "7 archived, 3 failed: \<reasons\>".
- `skipped: [{id, reason}]` = items that drifted out of terminal state at T3 re-verify
  (D1) — NOT failures, NOT in `failed[]`.

**`exit_code` — coarse batch-level status (per-item truth is in `acted[]/skipped[]/failed[]`):**

| value | meaning |
|-------|---------|
| `0` | Clean: every `candidate_id` was either acted OR cleanly skipped (D1 drift). `failed[]` is empty. |
| `2` | **DETERMINATE-PARTIAL**: `failed[]` is non-empty. The batch outcome IS known — exactly which items succeeded and which failed are enumerated. Cockpit branches on `failed[].length`, not on `exit_code`. |
| `1` | Whole-op setup error: bad params (missing `candidate_ids` on `dry_run:false`), unknown `mode`, `repo_root` mismatch (§ 3.3), or unreachable substrate. Nothing was attempted. |

> **IMPORTANT: fleet.* `exit_code:2` is DETERMINATE-PARTIAL — NOT Channel-B `exit_code:2`.**
> Channel-B `coverage.gate`'s `exit_code:2` means INDETERMINATE ("I cannot give a verdict;
> halt and retry the whole batch"). Those two exit-code contracts are independently defined.
> Do NOT port `coverage.gate`'s retry-the-whole-batch semantics to fleet.*. The correct
> fleet.* retry is `failed[]` only — safe by idempotency + D1 T3 re-verify.

**Cross-op exit code independence.** Fleet.* exit codes are the fleet op's own coarse batch
signal. A fleet op's `exit_code` MUST NOT pass through any sub-op's exit code. For example,
`handoff.has_live_children`'s `exit_code:1` means "safe to archive (affirmative verdict)" —
it MUST NEVER surface as the fleet op's `exit_code:1 = setup-error`. Fleet exit codes are
fleet-defined.

### 3.3 `repo_root` addressing (D3 — memo Q3)

**The global-multiplex transport supersedes the per-repo socket.** The socket is no longer
per-repo (the hash16 socket is RETIRED — see §1.1). The target repo/partition is now
selected by the mandatory `_origin_worktree` top-level envelope field (§1.1), not by the
socket path.

**`params.repo_root` remains OPTIONAL and non-selecting.** If present, it is a consistency
check against the resolved `_origin_worktree` — NOT a selector. If it does not match the
resolved `_origin_worktree` (after canonicalization), the op **fails loud** (`exit_code:1`,
`reason: "repo_root-mismatch"`). The op NEVER uses `repo_root` to address a different repo —
`_origin_worktree` is the sole addressing mechanism.

**Canonicalized comparison.** The op canonicalizes the incoming `repo_root` parameter via
`Path(repo_root).resolve()` (symlinks fully expanded, no trailing slash) and compares the
result to the resolved `_origin_worktree`. A cosmetic difference (trailing slash, unresolved
symlink, `/var` vs `/private/var` on macOS) that resolves identically is NOT a mismatch and
MUST NOT produce a spurious `exit_code:1`. Fail loud ONLY on a genuine
post-canonicalization mismatch.

**Scope note.** `fleet.*` ops are `common_dir`-scoped (`_OP_KEY_SCOPE`, `ipc.py:209-211`);
they REQUIRE `_origin_worktree`. The central ops (`artifact.emit`, `backlog.record`,
`goal.append`) are the ops that may omit `_origin_worktree` — cockpit never calls those.

### 3.4 `fleet.*` namespace admission precondition (D4 — memo Q4: the guardrail)

**Contract-level invariant:** admission to the `fleet.*` namespace over the **UDS**
is conditional on the op being **idempotent + commutative + git-reversible** (plus act-time
terminality re-verification per §3.1 D1 — see DR-211 D2 for the full five-bound admission
criterion). Any future `fleet.*` op lacking all three MUST NOT be exposed over UDS —
it waits for pcore-05 + the single-writer queue (the HTTP path with authz + serialization).
<!-- Review: the Staff Engineer F1 — dropped stale "ungated" adjective (two occurrences); "ungated" contradicts §5.1 + the amendment's own changelog; the admission-precondition argument (idempotent+commutative+git-reversible, orthogonal to auth) is unchanged -->
<!-- Review: code-reviewer slice-B F6 — added cross-reference to DR-211 D2 five-bound criterion; §3.4 named only three of five, leaving act-time re-verification gap for future op authors -->

**The three slice-1 ops satisfy all three properties:**

| property | satisfied? | reasoning |
|----------|-----------|-----------|
| idempotent | yes | re-archive = no-op via D1 T3 re-verify |
| commutative | yes | independent items; order-free |
| git-reversible | yes | git-mv is reversible |

**Full concurrency-safety argument.** The three properties make concurrent invocation
*outcomes recoverable and order-free*, but they alone do not constitute the complete safety
argument. The complete argument is:

> **Admission = (idempotent + commutative + git-reversible) AND (concurrency contention
> surfaces as `failed[]` via git's own index/ref locking, safe to retry by idempotency +
> D1 T3 re-verify).**

Two concurrent fleet ops (or a fleet op concurrent with a `/workstream-complete` sweep)
contend on `.git/index.lock`; the loser fails loud into `failed[]` with `reason: "lock"`,
not into silent corruption. Idempotency + T3 re-verify make a targeted retry of `failed[]`
safe. Cockpit's retry/concurrency UX should be coded against the `failed[]` surface, not
against the three properties alone.

**Enforcement (two layers):**
- (a) This contract prose invariant (present section).
- (b) An **admission affirmation** in the classification registry — each `fleet.*` op author
  affirms the three properties in a comment block, mirroring the existing COMPUTE_ONLY
  five-question affirmation discipline in `coordinator_core/authz/classification.py`. The
  mechanical form (a `FLEET_UDS_ADMISSIBLE` registry + drift-guard test) is an
  implementation-slice obligation, not built in this pass.

---

## 4. Mode seam — "A now, design for B" (D5)

`params.mode` is an **enum**. Slice A ships only `"already-terminal"`: act ONLY on items
already stamped terminal in their frontmatter/queue.

Slice B (in-flight completeness adjudication) is designed as a **new `mode` value** —
`"adjudicate-in-flight"` — plus a producer-side adjudicator, NOT a bilateral op re-version.
The enum is reserved and the extension point is documented here so slice B adds a filter
value + adjudicator without a wire schema re-bump.

The result envelope's `skipped[]/failed[]/acted[]` shape is already B-ready: D1's
re-verification generalizes to adjudication — "terminal at T3 by adjudication" replaces
"terminal at T3 by frontmatter" with the same skip/act branching.

**Version-skew safety — fail-closed on unknown mode.** The mode enum design is
version-skew-safe: a pre-B claude-klabauter daemon receiving `mode:"adjudicate-in-flight"` (from an
Cockpit already updated to slice B) yields a clean `exit_code:1` setup-error. It does NOT
fall through to `"already-terminal"` behavior (that would be a silent wrong-mode mutation).
This fail-closed-on-unknown-mode property is the **load-bearing guarantee** that makes the
enum-extension safe across cockpit/claude-klabauter version skew. Slice B can ship as a purely
additive enum value because old daemons fail loud, not silently wrong.

---

## 5. Auth surface

### 5.1 Slice 1 — UDS, per-partition auth (DR-208 live)

**Auth is now LIVE on UDS.** The C2 per-partition-auth landing (commit `0b15f7c`) requires a
valid `_auth_token` on **every** UDS request. The prior "ungated UDS, applies NO
token/scope/classification gate" framing is **stale and corrected here.**

Every `fleet.*` request MUST carry a valid READ_WRITE `_auth_token` top-level envelope field.
Source: `coordinator_core/ipc.py:589-722` (`_dispatch_line`), `coordinator_core/authz/token.py`.

- **DR-208 Invariant 3: NO anonymous tier** — even `ping` requires a valid token.
- Missing/empty token → `INVALID_REQUEST (-32600)` "missing bearer token (_auth_token)"
  (checked before dispatch; `ipc.py:648-650`).
- Invalid token → `INVALID_REQUEST (-32600)` "invalid token" (`ipc.py:673-677`).
- Token tier for `fleet.*` (MUTATING): **READ_WRITE** — **enforced on UDS** (not merely
  documented as required). The engine validates the presented token against the partition keyed
  by the git-common-dir of the resolved `_origin_worktree`. A READ_ONLY token presented for a
  MUTATING `fleet.*` op is **denied** with `-32601` — `"insufficient scope: READ_WRITE token
  required for mutating op"` (UDS scope-tier enforcement, gates 4/5, mirroring HTTP dispatch
  gates). An unclassified op is denied fail-closed with `-32601` — `"unclassified op -- denied
  fail-closed"`.
  <!-- Review: the Staff Engineer F2 — READ_WRITE is now ENFORCED on UDS (scope-tier gates 4/5 landed post-review); -32601 denials documented here to match the error taxonomy in §1.1 -->

The UDS is still user-only by socket permissions (parent dir mode `0700`), but socket
permissions alone are no longer the sole gate.

The token-read discipline (re-read per connect, no-cache across restart, `0600` file perms,
path `<git-common-dir>/coordinator-service/token`) is shared with the HTTP surface in §5.2 —
see §1.1 "Mandatory top-level envelope fields" for the full protocol.

UDS admission for the three slice-1 ops remains permissible **because** they are
idempotent + commutative + git-reversible (§3.4 — the full safety argument). Auth is a
separate gate orthogonal to the admission precondition argument.

Auth is **LIVE on UDS** (not provisional, not deferred). The authz model and token tiers are
documented in `docs/decisions/DR-208-invoke-op-authz-model.md`.

### 5.2 Later slices — HTTP + pcore-05

Cloud/HTTP invoke of `fleet.*` is a later slice, gated on pcore-05 + the single-writer
queue. At that point:

- fleet.* ops over HTTP require a **READ_WRITE** token (two-tier model:
  `coordinator_core/authz/token.py`; tokens rotate on every service start; cockpit MUST
  re-read the token file on every connect and MUST NOT cache a token across a possible
  restart).
- The existing HTTP gate 6 (`requires_single_writer_queue`) already returns 403 for MUTATING
  ops until pcore-05 lands — fleet.* ops are kept off HTTP for free by this gate.

---

## 6. Execution model — RATIFIED SYNCHRONOUS (DR-211)

**Status: RATIFIED (DR-211).** The wire execution model for `dry_run:false` is synchronous
single-round-trip; the claude-klabauter-side execution model (handler-mutates synchronously, scoped-pathspec
commit, asyncio-awaited git subprocesses) is ratified per
`docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`.

### Wire decision (PINNED)

Slice-1 fleet ops are **synchronous**: the handler mutates and returns `{acted[], skipped[], failed[]}`
in the same response. One request → one response. This is correct for slice-1 because its operations
are bounded (a modest human-reviewed set), sub-second (git-mv in a local repo), and human-confirmed
(cockpit presents the dry_run preview for explicit approval before act). A synchronous round-trip
is the natural fit for bounded, fast, human-gated ops; there is no latency budget that requires a
job-poll shape.

**Async reservation (forward-compat — additive, no wire re-bump required).** The envelope RESERVES
an async job-handle shape for FUTURE long-running fleet ops. A future `fleet.<long_op>` MAY return:

```json
{ "job_id": "<str>", "status": "pending" }
```

together with a `fleet.status(job_id)` poll op, added additively WITHOUT a wire schema re-bump —
the same forward-compat discipline as the `mode` enum extension point (§ 4). Slice-1 ops are
synchronous by their bounded nature; this reservation ensures a future long-running op never forces
a contract break. Old cockpit clients receiving an unexpected `job_id` field fail loud (unknown
shape), not silently wrong — the same fail-closed discipline as the mode enum unknown-value guard.

### Claude-Klabauter-internal implementation obligations (DO NOT reshape the wire)

The following items are claude-klabauter-internal implementation requirements tracked separately as a
reconciliation spinoff. They do **NOT** appear in or reshape the frozen wire contract. Cockpit
coding against this contract is NOT gated on their resolution:

**(a) Substrate-write boundary DR — SANCTIONED by DR-211.** The `fleet.archive_*` and
`fleet.prune_closed_bugs` handlers are the first op class to cross the `ipc.py:28-31`
negative-spec boundary — git-mv records (handoffs, plans, bugs) into `archive/`, commit, and
write into `state/`, all three enumerated forbidden nouns. Shelling out to the bash sweeps does
NOT dodge this: an op handler that `subprocess.run`s `cs_sweep_terminal_plans` which then does
`git mv` + `git commit` still crosses the enumerated boundary. This is sanctioned by DR-211
(`docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`), which explicitly ratifies and
amends the negative-spec — not a silent classification entry.

**(b) Commit-ownership invariant — RATIFIED (D3 commit model).** The handler MUST commit
atomically with a scoped pathspec: git-mv the affected items, then
`git add -- <exact archived paths> && git commit -- <same>` in a single transaction, never
leaving a dirty index on the shared `work/*` branch. The explicit two-step (`git add` then
`git commit` with identical pathspec) is the ratified form per DR-211's D3 commit-ownership
model — the same scoped-commit discipline as `docs/wiki/scoped-safety-commits.md`.

**(c) Asyncio subprocess discipline.** The handler MUST `await` git subprocesses (asyncio
non-blocking I/O — NOT blocking `subprocess.run`) so the synchronous wire shape never stalls
Claude-klabauter's single event loop. The round-trip is synchronous from the wire perspective; the
implementation must be non-blocking from the event-loop perspective.

These are claude-klabauter-internal implementation requirements, now RATIFIED per DR-211
(`docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`). They do NOT gate the frozen
wire contract. **Path 2** (op returns a confirmed candidate set; a bash/git surface executes)
is retired. **Path 1 (handler mutates synchronously) is the RATIFIED execution model per
DR-211**: the handler mutates within the request, commits atomically with a scoped pathspec,
and awaits git subprocesses via asyncio.

---

## 7. Shell-out vs. port (D6 — producer-internal; deferred)

The underlying sweeps are bash in `~/.claude` (a soft claude-klabauter prereq):
`cs_sweep_terminal_plans` (`lib/coordinator-session.sh`),
`cs_sweep_actioned_memos` (no slice-1 op — memo archival is not included in this batch; potential future `fleet.archive_actioned_memos` op), `bin/coordinator-handoff-archive.sh`, bug-backlog
closure = git-mv to `archive/`.
<!-- Review: code-reviewer — added parenthetical on cs_sweep_actioned_memos clarifying no slice-1 fleet.* op maps to it; prevents cockpit EM reading §7 as implying a dropped/missing op --> The op registry is Python in `coordinator_core`.

This is a **producer-internal implementation decision that does NOT affect the wire
contract.** The contract records a recommendation — shell-out for slice 1 (reuse the audited,
in-use bash sweeps verbatim; avoids a semantics-fork between the plugin's
`/workstream-complete` sweep and the op), with a port flagged as the eventual direction once
the sweeps' behavior is contract-pinned — but leaves the binding decision to the
implementation slice. This is called out so cockpit and the co-design reviewer see it is
deliberately deferred, not overlooked. The wire contract (§ 6) is agnostic to shell-out vs. port — either path satisfies the synchronous round-trip requirement.

---

## 8. Out of scope — not our surface

To keep the producer/consumer boundary unambiguous, the following are **explicitly NOT part
of this contract**:

- **Consumer side** (cockpit `/fleet` connector, Next.js route, WS2 UI dashboard) —
  example-cockpit-repo's own design surface.
- **Op implementation** — no handler code, no `register_op()` entries, no
  `OP_CLASSIFICATION` entries. That is the downstream implementation slice.
- **Cloud / HTTP invoke of fleet.*** — later slice, gated on pcore-05 + the single-writer
  queue (DR-208).
- **pcore-10 / pcore-11** (internal HTTP invoke surface / cockpit-facing envelope impl) —
  downstream of the frozen spec.
- **The single-writer queue** (pcore-05/DR-208 machinery) — separate workstream.
- **Channel-B §1.1 socket-algorithm amendment** — the global-multiplex socket algorithm is
  ACTIONED for Channel A in this contract (§1.1 above). Channel B (example-retrieval-repo producer
  contract, `coordinator_core/contract/example-retrieval-repo-producer-contract.md`) still carries its
  own per-repo hash pin and is a **SEPARATE reconciliation** — do NOT assume Channel B is
  updated by this amendment.
- **A JSON Schema sibling** (`fleet-invoke-envelope.schema.json`) — not authored in this pass;
  a natural follow-on once the implementation slice lands.
- **cockpit's `/fleet` state display** — comes from example-retrieval-repo's typed query surface;
  `dry_run:true` is NOT a polling mechanism for that display.

---

<!-- producer-contract: pcore-06 Channel A — coordinator→example-cockpit-repo fleet.* invoke-envelope. FROZEN (co-design complete 2026-07-04; amended 2026-07-05 global-multiplex reader-widen v1.1). -->
