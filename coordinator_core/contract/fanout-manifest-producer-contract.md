# fanout-manifest.v1 — consumer-facing contract

> **What this is.** The statement of the manifest the `coordinator:cloud-fanout` verb writes and
> the ops `fanout.compose`, `fanout.census` and `fanout.reconcile` read. **Source of truth:**
> `coordinator_core/contract/fanout-manifest.v1.schema.json`, loaded only through
> `coordinator_core/ops/fanout/contract.py :: load_schema`. This note restates; the schema wins.

## 1. Manifest fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | const `fanout-manifest.v1` |
| `job_id` | yes | slug; tag namespace and idempotency-key prefix |
| `environment_id`, `model` | yes | passed to every child's `create_session` |
| `permission_mode` | yes | `default`, `acceptEdits`, `bypassPermissions` or `auto`; `plan` is refused |
| `max_concurrent` | no | integer >= 1, default 6 |
| `parent` | yes | `{session_id, channel_pr_url}`; no channel address, no create_session args |
| `channel` | yes | `pr` (the only transport) |
| `comms_repo` | when `channel: pr` | `<owner>/<repo>`; the verb resolves it from config and writes it here |
| `repos` | no | job-level extra roster repo names |
| `workers[]` | yes, >= 1 | `{id, source_url, focus, prompt, outcome_branch?, tags?, repos?}` |

`workers[].id` is a slug, unique within the manifest. `source_url` is the child's write repo.
`focus` is a declared `repos.<key>`; it is never derived from the checkout count.
`prompt` is inline text.

## 2. Standard child roster

`STANDARD_CHILD_ROSTER = (example-retrieval-repo, coordinator-claude, claude-klabauter)`. A worker's
effective roster is that tuple, then `manifest.repos`, then `worker.repos`, order-stable and
deduplicated. No field removes a member.

## 3. Identity

- title and idempotency key: `<job_id>/<worker_id>`
- tags, in order: `fanout:<job_id>`, `fanout-worker:<worker_id>`, `fanout-parent:<parent.session_id>`,
  `fanout-focus:<focus>`, then the worker's own tags
- census keys on the `fanout:` + `fanout-worker:` pair only

`create_session` argument keys are `contract.CREATE_SESSION_ARG_KEYS`; they are unverified against
the live tool, and a correction lands in that constant.

## 4. Output shapes (TypedDicts in `contract.py`)

- compose: `{job_id, actions: [{worker_id, idempotency_key, create_session}]}` in worker order.
- census: `{job_id, rows: [{worker_id, state, session_ids, status_bucket, status_detail, cost_usd,
  checked_in, channel_url}], strays: [session_id]}`; `state` is `missing | running | done | failed |
  duplicate | unknown`. `DONE_BUCKETS` map to `done`, `FAILED_BUCKETS` to `failed`; an unrecognised
  bucket is `unknown`.
- reconcile: `{actions: [{kind, worker_id?, session_id?, idempotency_key?, reason}]}`; `kind` is
  `spawn | archive | flag | await_checkin | broadcast`.

## 5. Check-in line

A child posts `[session <id>] channel: <url>` on the parent's channel as its first act;
`transport.parse_checkin` is the only parser.
