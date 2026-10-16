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

`args.readyOp.schema` is optional and already sent (c1480cbe72): the JSON schema of the stage manifest (`ask_compose._MANIFEST_SCHEMA`).
A producer should use it as the `readyOpReply` property schema in place of `{}`, so StructuredOutput
enforces the manifest shape on the relaying agent. A producer that predates this ignores it.

## 3. When the op runs

Only when all hold for a plan: the readiness-op reply is honoured, `verdict === 'ready'`, and
`mechanical_repairs` is empty. The agent that ran the readiness op then runs
`coordinator-invoke <op> '<params ∪ {plan_path: <that plan's repo-relative path>}>'` exactly once.
Never for a pulled, replan or judgment plan.

## 4. Where the reply lands

The reply is returned verbatim as `readyOpReply` on that plan's entry in `ready`, and nowhere else:
never on a pulled, replan or judgment entry. plan-blitz never interprets the reply. Claude-klabauter adopts
it as the staged manifest only when it is whole:

- an object with no `error`;
- `rows`, `review_declared_paths` and `test_targets` are arrays;
- every row is an object whose `id`, `brief_path`, `agent_type` and `model` are non-empty strings,
  `wave` is an integer, and `writes` and `deps` are arrays;
- every `gated[]` and `owed[]` entry present carries a non-empty string `id`.

A reply that fails this halts the run with `relayed stage manifest is not whole: <first failing
field>` and the refire `next_action`; no executor dispatches. A ready entry that is a bare string
carries none.

## 5. Absent arg and old plan-blitz

Absent `args.readyOp`, plan-blitz behaves as before. A plan-blitz that predates this contract
ignores the arg and attaches no `readyOpReply`; claude-klabauter then runs its fallback stage agent
unchanged.
