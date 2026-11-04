# Capture notes: agy stream-json and codex exec --json (2026-11-02)

Real headless runs from cwd `scratch/c0-probe`. Stdout stored verbatim; no CSRF or token
values appeared (the only "token" strings are usage counters). One edit: the agy init `cwd`
absolute path is replaced by `<repo>/scratch/c0-probe`.

Re-run check (same day, agy 1.4.12 / codex-cli 0.166.0): both CLIs were run again with the
same prompt and argv; event shapes matched the stored fixtures exactly (only ids, durations
and token counts differ). The stored captures are kept because tests pin their ids.

Prompt (both): "Reply with exactly two separate short messages: first say ALPHA, then in a
separate message say BETA. Do not use any tools."

## agy 1.4.12

argv: `agy -p "<prompt>" --output-format stream-json`
No `--dangerously-skip-permissions`, no `--settings`. Exit 0. `permission_mode` in the init
event was `request-review`.

Message shape: `{"event":"step_update","step_update":{"step_type":"agent_response",
"state":"ACTIVE","text_delta":"..."}}`; final `{"event":"result","result":{"response":...}}`.
Events: init, step_update (user_input DONE), step_update (agent_response ACTIVE, text_delta),
step_update (agent_response DONE), result.

Observations:
- The two requested messages were NOT two events: agy merged them into one agent_response
  step (`"ALPHA\n\nBETA"`). One step_update per step, not per message; a consumer wanting
  separate messages must split inside text_delta or use the `send_message` tool.
- `text_delta` on the ACTIVE event held the full text, not an incremental fragment; the DONE
  event carried a trailing "\n".
- Stdout was fully parseable NDJSON, every line valid JSON.
- Message emitted without any tool approval: YES (plain model output, no tool call, no
  permission prompt, no auto-deny). This does NOT prove outbound via the `send_message` tool
  under headless auto-deny; that path was not exercised (spike § 8 reported auto-denied reads).
- init lists a `send_message` tool and `manage_inbox`; untested here.

## codex-cli 0.166.0

argv: `node <node-prefix>/lib/node_modules/@openai/codex/bin/codex.js exec --json --skip-git-repo-check "<prompt>"`
(prefix from `codex_runner.resolve.resolve_codex()`). No dangerously-bypass flag. Exit 0.
stderr: "Reading additional input from stdin..." (stdin should be closed or redirected).

Events: thread.started, turn.started, `item.completed` with `item.type == "agent_message"`
(one per message: item_0 "ALPHA", item_1 "BETA"), turn.completed (usage).

Observations:
- Two distinct agent_message items, emitted mid-run, parseable NDJSON.
- Message emitted without any tool approval: YES (plain model output).

## Verdict

Premise (headless runs emit parseable agent-authored message events with no tool approval):
holds for both CLIs as plain model output. Granularity differs: codex one event per message,
agy one event per step.

## codex app-server

Captured 2026-11-04, codex-cli 0.166.0, macOS, against the running app-server daemon through
`codex app-server proxy` (argv: `[*resolve_codex(), "app-server", "proxy"]`). Schema from
`codex app-server generate-json-schema`. `codex_app_server.jsonl` holds redacted lines, one
`{"scenario","dir":"client|server","msg":<json>}` per line; scenarios `owner`, `non_owner`,
`error`. Home and repo paths, installation id, host name and the live TUI thread id are replaced
by placeholders; unrelated notifications (mcpServer/startupStatus, account/*, tokenUsage,
remoteControl) are dropped from the fixture. Throwaway threads were started by the probe
(`thread/start`, ephemeral or not), never the live TUI thread. The transport CAN deliver into a
thread loaded in the daemon; there is no named reason against it. The traps are below.

### Framing (trap: not bare JSON lines)

`proxy` is a byte pipe to the daemon's Unix socket (codex owns Unix socket vs named pipe per OS).
The daemon speaks **WebSocket (RFC 6455) over that pipe**. Plain newline JSON written to the
proxy gets no reply and no error. A client must:
1. write an HTTP upgrade first: `GET / HTTP/1.1`, `Host: localhost`, `Upgrade: websocket`,
   `Connection: Upgrade`, `Sec-WebSocket-Key: <b64 16 random bytes>`, `Sec-WebSocket-Version: 13`,
   blank line; read until the blank line of the `HTTP/1.1 101 Switching Protocols` reply
   (carries `x-codex-websocket-max-unfragmented-message-bytes: 16777216`);
2. then exchange WebSocket frames, one JSON-RPC message per text frame (opcode 1). Client frames
   MUST be masked (FIN+text `0x81`, mask bit set, 4-byte mask; 7-bit / 16-bit / 64-bit length).
   Server frames are unmasked; handle opcode 8 (close) and 9 (ping, reply pong 0xA masked).
Python stdlib has no ws client; C1 hand-rolls these ~40 lines over `Popen` pipes (`bufsize=0`,
binary). No `websockets` dependency.

JSON-RPC shape: client messages carry `"jsonrpc":"2.0"`; server messages OMIT it
(`{"id":1,"result":...}`, `{"error":{"code","message"},"id":3}`, notifications
`{"method","params","emittedAtMs"}`). Match responses on `id` and absence of `method`.

### Handshake

1. `initialize` request, params `{"clientInfo":{"name","version"}}` (required; `title`,
   `capabilities` optional). Result `{userAgent, codexHome, platformFamily, platformOs}`.
2. `initialized` notification (no id, no params). Requests sent before it were not tested.
Server may interleave notifications (`remoteControl/status/changed`, `account/updated`, ...)
before any response: skip anything that is not the awaited id.

### Loaded-thread query (C2 branches on this)

Method `thread/loaded/list`, params `{}` (optional `cursor`, `limit`). Result
`{"data":[<threadId>,...],"nextCursor":null|str}`: ids of threads loaded in the daemon, across
all connections, including the live TUI's. A thread with no user turn (the PM's failing case)
IS listed. Follow `nextCursor` until null. A conversation absent from `data` is not loaded:
`turn/start` on it errors (below), so C2 keeps `exec resume` for it. The unreachable-daemon
case is the proxy exiting non-zero / closing before the 101 reply.

### turn/start

Request `{"method":"turn/start","params":{"threadId":<id>,"input":[{"type":"text","text":<text>}]}}`
(`text_elements` defaults to []). Result `{"turn":{"id":<turnId>,"status":"inProgress",...}}`
returns immediately, before the model runs. Accepted on a loaded thread with no rollout
(scenario `non_owner`), from a connection that did not start the thread.

### Trap: thread/resume reproduces the PM's failure

`thread/resume {threadId}` on a loaded thread with no rollout fails with
`{"code":-32600,"message":"no rollout found for thread id <id>"}` (scenario `non_owner`), the
same text the PM saw. Never call `thread/resume` to attach; it is not needed for `turn/start`.

### Trap: a non-owning connection gets no turn or item notifications

Scenario `owner` (the connection that ran `thread/start`) receives `turn/started`,
`item/started|completed`, `item/agentMessage/delta`, `turn/completed`. Scenario `non_owner`
(any other connection, which is what `agent_pipe` is relative to a live TUI) receives ONLY
`thread/status/changed` for the thread: `{"status":{"type":"active","activeFlags":[]}}` then
`{"status":{"type":"idle"}}`. So completion detection and reply retrieval for a delivery are:
1. `turn/start` -> record `turn.id`;
2. read frames until `thread/status/changed` with `params.threadId == thread_id` and
   `params.status.type == "idle"` (seen after an `active`); if `turn/completed` or
   `item/completed` for the thread arrives first (owner case) use those directly instead;
3. `thread/turns/list {"threadId":...}` -> `result.data[]` of turns (`id`, `status`
   `completed|failed|interrupted|inProgress`, `error`, `items`); select the one whose `id` equals
   the `turn.id` from step 1; reply texts are its `items` with `type == "agentMessage"` (`text`,
   `phase` `final_answer`, ...) in order. A turn with `status != "completed"` and a non-null
   `error` is a failure.
`thread/turns/list`, `thread/read {includeTurns:true}` need a rollout: an **ephemeral** thread
returns `-32600 "ephemeral threads do not support ..."`; `thread/items/list` returned
`-32601 "not supported yet"`. A real TUI thread is non-ephemeral and has a rollout once the
delivered turn is recorded (probe confirmed with a non-ephemeral thread). Cap the idle wait
(the turn may run for minutes); on cap, raise a timeout error rather than hang.

### Errors

JSON-RPC error: `{"error":{"code":-32600,"message":...},"id":N}`. `turn/start` on a thread not
loaded: `-32600 "thread not found: <id>"` (scenario `error`). Unknown method: `-32600` with
`Invalid request: unknown variant ...`.

### Interface C1 builds (pinned)

```
class AppServerError(Exception):  # code: int, message: str
def loaded_threads(argv: list[str]) -> list[str]
    # spawn argv, ws upgrade, initialize/initialized, thread/loaded/list (all pages); kill child.
def deliver(argv: list[str], thread_id: str, text: str) -> Iterator[str]
    # spawn argv (= CodexAdapter.proxy_argv()), ws upgrade, initialize/initialized, turn/start,
    # wait idle (or turn/completed), yield each agentMessage text of THAT turn in order.
    # JSON-RPC error response -> raise AppServerError(code, message). Always kill child in finally.
    # No thread/resume. Proxy exit / EOF / ws close before the turn ends -> AppServerError(-1, ...)
    # or a distinct subclass `AppServerUnreachable` when it happens before initialize returns.
```
C2: `thread_id in loaded_threads(argv)` -> `deliver`; otherwise `exec resume`. A `deliver`
`AppServerError` is the run's error, never an `exec resume` retry; `AppServerUnreachable`
(before initialize) falls back to `exec resume`. `loaded_threads` and `deliver` may share one
connection helper; two spawns per send is acceptable (each ~sub-second).

### turn/steer (schema-derived, not live-captured)

`codex app-server generate-json-schema`: `turn/steer` params `{threadId, input, expectedTurnId}`
(all required), result `{turnId}`. `deliver` finds a running turn via `thread/turns/list`
(`status == "inProgress"`; a thread with no rollout errors and counts as idle), steers with that id,
and falls back to `turn/start` if the steer is refused. The fake proxy's `busy`/`busy_stale`
scenarios are synthesized from the `non_owner` capture, not recorded from the daemon.
