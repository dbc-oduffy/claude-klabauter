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
