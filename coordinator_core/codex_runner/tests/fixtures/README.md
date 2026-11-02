# codex runner fixtures

Captured with codex-cli 0.166.0 (ChatGPT sign-in, darwin-arm64) on 2026-11-02. One JSON event per line from `codex exec --json`.

| File | Basis | Command |
|---|---|---|
| `review_run.jsonl` | real capture | `codex exec --json --skip-git-repo-check --sandbox read-only "<review prompt>"` in a scratch dir holding a 2-line `artifact.py` |
| `execute_run.jsonl` | real capture | `codex exec --json --skip-git-repo-check --sandbox workspace-write "<edit via apply_patch>"` in a scratch dir |
| `limit_exhausted.jsonl` | **synthesised** | not reproducible on demand |

Note: the `review_run.jsonl` final message predates the strict findings schema (`title`/`location`/`detail`); tests replay it as events and supply `last.json` separately.

## Event names (C3/C4 key on these)

- Stream: `thread.started` (`thread_id`), `turn.started`, `item.started`, `item.completed`, `turn.completed` (`usage`: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`).
- Item types seen: `agent_message` (`text`), `command_execution` (`command`, `aggregated_output`, `exit_code`, `status`), `file_change` (`changes`: list of `{path, kind}`, `kind` in `add`/`update`; paths are absolute; `status` `in_progress` then `completed`).
- Review findings arrive as the final `agent_message.text`, a JSON string the caller must parse.
- Trap: a file written through the shell (`printf ... > f`) emits only `command_execution`, no `file_change` (observed in a first execute run, not kept). The scope audit must not rely on `file_change` alone; only `apply_patch` edits emit it.
- stderr prints `Reading additional input from stdin...` when no stdin is given; stdout is clean JSONL.

## limit_exhausted.jsonl (synthesised)

The real event could not be provoked. The message text is taken from strings in the installed native binary (`@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`), which contains `You've hit your usage limit.` with variants: `... Visit https://chatgpt.com/settings/usage to purchase more credits`, `... Upgrade to Plus to continue using Codex`, `... Upgrade to Pro`, `... To get more access now, send a request to your admin`, `... for <model>`. The binary also names `rate_limit_reached`, `workspace_owner_usage_limit_reached`, `workspace_member_usage_limit_reached`, `workspace_*_credits_depleted`. The `error` and `turn.failed` envelope (`{"type":"error","message"}`, `{"type":"turn.failed","error":{"message"}}`) is from codex's documented exec JSONL shape; `turn.failed` is present as a string in the binary, the exact envelope is unverified here. C3's detector should match the message substring `hit your usage limit` and the quoted codes, and name this basis.

No auth tokens or secrets are in any fixture. Thread ids are throwaway.
