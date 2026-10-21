# plan-blitz ready-op — cross-repo contract

> **What this is.** The statement of the `readyOp` arg `planBlitz(args)` accepts and the
> `readyOpReply` it attaches to a `ready` entry. Lets the agent that already proved a plan ready
> run one more engine op, so the consumer needs no separate agent for it.

## 1. Producer and consumer

- **Producer:** DoE `plan-blitz.mjs` (`readinessStep`, run by the last plan reviewer).
- **Consumer:** claude-klabauter `coordinator_core/ops/dispatch_emit/ask_compose.py`, through
  `ask_plan_blitz.wrap_stage`. Claude-klabauter's op is `dispatch.ask_stage`.

## 2. Arg shape

`args.readyOp = {op: string, params: object}`, optional. `op` is an engine op name; `params` is
the op's params without `plan_path`. Claude-klabauter sends `op: 'dispatch.ask_stage'` with
`{run_id, writes, cross_repo_approved, session_id, commit_sizing_path?}`. The key is omitted when
the plan is already staged and in a `stop_after` script, which never stages.

`args.readyOp.schema` is optional and already sent (c1480cbe72): the JSON schema of the stage reply
(`reply_sidecar.STAGE_REPLY_SCHEMA`), which is a `manifest_handle` (`{path, sha256, bytes}`) or a
refusal (`error`, `owed_em_ids`, `owed_plan`), never the manifest. A producer should use it as the
`readyOpReply` property schema in place of `{}`, so StructuredOutput enforces the reply shape on the
relaying agent. A producer that predates this ignores it.

## 3. When the op runs

Only when all hold for a plan: the readiness-op reply is honoured, `verdict === 'ready'`, and
`mechanical_repairs` is empty. The agent that ran the readiness op then runs
`coordinator-invoke <op> '<params ∪ {plan_path: <that plan's repo-relative path>}>'` exactly once.
Never for a pulled, replan or judgment plan.

## 4. Where the reply lands

The reply is returned verbatim as `readyOpReply` on that plan's entry in `ready`, and nowhere else:
never on a pulled, replan or judgment entry. plan-blitz never interprets the reply. Claude-klabauter adopts
the reply's `manifest_handle` as the stage manifest when the sidecar it names is whole, and only then.
The engine leaves `scratch/stage-readers/<sha256>.js` beside every handle; the script reads it with
`workflow({scriptPath})` and checks the text's length and sha256 against the handle before parsing
it. A mismatch halts the run with `stage reply sidecar does not match its handle: <reason>`. The
`readyOpReply` shape is unchanged: nothing on the plan-blitz side moves. The parsed
manifest must be:

- an object with no `error`;
- `rows`, `review_declared_paths` and `test_targets` are arrays;
- every row is an object whose `id`, `brief_path`, `agent_type` and `model` are non-empty strings,
  `wave` is an integer, and `writes` and `deps` are arrays;
- every `gated[]` and `owed[]` entry present carries a non-empty string `id`;
- `head_sha` is 40 lowercase hex.

When the reply is absent and the op did not run (no error, or the `did not run` error), claude-klabauter composes
one conditional haiku leg, `blitz-stage-read`: it runs the stage op and returns the op's reply as
`stage_reply`, which is adopted by the rule above. A reply that is present but carries neither a
refusal nor a handle halts with `stage manifest relay is not whole: manifest_handle`, and a stage leg
that returns no reply with `no stage manifest on disk: <manifest_rel>`; both carry the refire
`next_action`; no executor dispatches. A ready entry that is a bare string carries none. A producer
that puts a caller-supplied reply schema on `readyOpReply` removes the leg.

## 5. Absent arg and old plan-blitz

Absent `args.readyOp`, plan-blitz behaves as before. A plan-blitz that predates this contract
ignores the arg and attaches no `readyOpReply`; claude-klabauter then runs its fallback stage agent
unchanged.
