# Codex route to an engine-ask: submit, then fire

A Codex agent has no `Workflow` tool, so it reaches the engine-ask through two `coordinator-invoke`
calls. The gates are the ones the Claude PreToolUse hook (`hooks.submit_engine_ask`) runs; both doors
call one function, `coordinator_core/hooks/_engine_ask_gates.py :: submit_ask`. No gate is relaxed for
Codex. The ask still enters through the engine-ask contract with `ask_source` `pm` or `relayed`, so the
PM-verbatim ledger applies unchanged.

## Before submitting

`--repo` is the absolute checkout path, for example `/absolute/path/to/checkout`.
The JSON `repo` parameter is a separate routing input; do not substitute it for the CLI path.
Read the thread UUID from `CODEX_THREAD_ID`, then pass that value explicitly as `session_id`.
Use the peer name announced by the SessionStart hook as `caller`; its binding is in
`<settings_home>/unimatrix/peers.json` (`vendor: codex`, `target: <thread UUID>`).
Do not guess a peer name from the UUID. Preserve PM wording in `ask`; keep your own framing
in `em_context`. A sizing submission supplies a path string to `sizing` instead of `ask`.

The commands below require an installed engine exposing `engine_ask.submit`. If the running
engine predates that op, update it through the host's supported install procedure before submitting.
Do not repeat a mutation after a timeout without reconciling whether it already created a run.

## 1. Submit

```
coordinator-invoke engine_ask.submit --params-file - --repo <repo> <<'EOF'
{"ask": "<the PM's words verbatim, or the relayed ask>",
 "ask_source": "pm",
 "session_id": "<your Codex thread id>",
 "caller": "<your peer name, e.g. the name peers.json binds>",
 "cwd": "<absolute path of your checkout>",
 "em_context": "<optional EM framing, kept separate from the ask>"}
EOF
```

- `ask` xor `sizing`; `ask_source` (`pm` | `relayed`) is required with `ask`.
- `session_id`, `caller` and an absolute `cwd` are required and never read from the environment.
- Optional: `repo`, `agent_id`, `em_context`, `context`, `brief`, `brief_file`, `route_preference`, `em_rationale`,
  `em_size`, `stop_after`, `chatty`, `cross_repo_approved`, `supersedes`, `writes`, `fire`, `notify_to`.
- Unknown params are refused. Full contract: the `coordinator_core/ops/engine_ask_submit.py` docstring.

The CLI normally wraps successful replies as `{"jsonrpc":"2.0","id":...,"result":...}`.
Read the handle from `result`, or add `--bare` to print just the successful result object.
An error still prints the full error envelope with a nonzero exit code when `--bare` is used.
A successful transport can return an application refusal: check `refused` before using the handle.

Reply, never `{}`: `{run_id, script_path, sha256}`, or `{refused, reason}` with `refused` one of
`bad-request`, `gate-refused`, `no-run`. A refusal is the hook's own gate verdict; fix the cause, do
not retry unchanged.

## 2. Fire

```
coordinator-invoke workflow.fire '{"script_path": "<script_path>", "expected": "<sha256>", "notify_to": "<your peer name>"}' --repo <repo>
```

`expected` is the submit reply's `sha256`, passed through as-is; `workflow.fire` refuses the fire if
the script bytes changed since submit. Passing `"fire": true` in step 1 does both calls: the reply then
carries `fire` (the registry record), or `fire_error` with the run handle kept when the fire failed
after a good submit. With `fire: true`, `notify_to` defaults to `caller`; an explicit peer name overrides it.
The warm engine observes driver exit and delivers the completion notice. The caller
waits for that notice without polling or relaying the live run. After a terminal notice,
`workflow.fire_status` can reconcile the recorded handle and outcome; driver exit and
`outcome: clean` do not establish phase success.

For Windows or arbitrary free text, write a UTF-8 JSON file with Python and supply
`--params-file <absolute JSON path>`; the heredoc example is for POSIX shells.

A matching verified script already reserved or executed returns its existing fire handle and
`already_fired`; it does not start another driver. A pending reservation whose owner disappeared
can report `execution_started: null`: inspect the handle and run artifacts before taking action.
An exited driver with a failing phase is still an executed run; use the revision route to repair
the work rather than firing the same digest again.

## Revise

```
coordinator-invoke workflow.revise '{"run_id": "<run_id>", "verbatim": "<PM words>"}' --repo <repo>
```

`verbatim` adds a requirement row; `note` is EM context only. Same op Claude callers use; params are
in `coordinator_core/ops/dispatch_emit/requirements_ledger.py :: revise`.

## Completion

`workflow.fire` persists `notify_to` in the fire record. The warm engine's idle watchdog
observes these records, refreshes driver state, and delivers terminal notices through
`agent_pipe`. Pending deliveries to live or unknown recipients keep the engine active; terminal notices for known-ended default UUID recipients remain on disk without holding the engine open. Resuming that thread allows retry. a successor discovers persisted
records after restart. Refused or unroutable attempts retain `notify_error` and retry on
later watchdog ticks. The caller never polls to trigger delivery.

Notification attempts use the shared nonblocking kernel-backed per-record lock, also used by status reads and ordinary fire sweeps. Stable message IDs allow recipient deduplication.
Delivery is at least once across a crash after bridge acceptance but before persistence;
a persisted `notified_at` suppresses subsequent attempts. The kernel releases a crashed observer's lock automatically.
Directory discovery advances 128 records in one repo per watchdog tick, rotating repos;
one active delivery is refreshed per tick, rotating records (currently five seconds per tick). Registry unavailability does
not block other repos. An installed warm engine must be running for observation; after
an operator stops it, restart the engine through its supported launch path.

Notification persistence failure preserves the fire handle; do not fire a second driver
to repair a notification failure.

`agent_pipe.roster.resolve` reads `roster.json` first, then the SessionStart hook's `peers.json`.
A resolved Codex identity still requires a working delivery leg. A `handed-off` delivery status
means the leg started; it does not prove the receiving thread consumed the message.
Codex workflow notices use `thread/queue/add` instead of starting another turn against the
TUI's held writer. Their `queued` status confirms persistent queue acceptance, not consumption.

## Codex context compaction

Codex automatically compacts history using the active model's native token limits. To also
compact between turns before the usable window fills, set this top-level value in
`$CODEX_HOME/config.toml` (default `~/.codex/config.toml`):

```toml
model_post_turn_compact_threshold_percent = 80
```

This applies after a final response and scales with the model's usable context window.
Native auto-compaction limits still apply during long turns. Omit the setting or set it to
zero to disable the additional turn-end threshold. Keep `model_auto_compact_token_limit`
unset to use model defaults; a fixed token count does not scale across different model windows.
Existing sessions may require a resume or a new session to pick up configuration changes.
Validate installed-version support with `codex app-server --strict-config --print-startup-config`.
Reference: https://developers.openai.com/codex/config-reference/.

## Native workers and named reviewers

See [external-agent-workers.md](external-agent-workers.md) for POSIX worker launch, native
Windows PowerShell launch, and asynchronous `codex_runner.start` / `codex_runner.result` named
reviews. See [non-claude-session-identity.md](non-claude-session-identity.md) for native session
bootstrap and engine attribution. These routes preserve the caller identity across the warm engine.
