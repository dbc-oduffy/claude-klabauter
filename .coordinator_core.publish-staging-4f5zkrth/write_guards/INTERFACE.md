# write_guards — per-guard module interface (READ BEFORE PORTING)

Each ported guard is ONE module `coordinator_core/write_guards/<name>.py` exposing
EXACTLY this interface. The engine (`engine.py`) discovers modules dynamically and runs them.

```python
CLASS = "hard-deny"          # or "advisory". hard-deny can block the tool call; advisory only adds context.
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]   # subset the guard actually acts on
PRIORITY = 50                # int; lower runs first WITHIN its CLASS (assigned per guard in the wave map)

def check(payload: dict) -> dict | None:
    """
    payload: the parsed PreToolUse JSON dict. Keys: tool_name, tool_input(dict),
             session_id, cwd, agent_id (present iff a subagent), agent_type, transcript_path.
             Write/Edit/MultiEdit → tool_input['file_path']; NotebookEdit → tool_input['notebook_path'].
             Write → content; Edit → old_string/new_string; MultiEdit → edits[].
    return:  None to ALLOW/no-op; else the NESTED hookSpecificOutput envelope (see below).
    """
```

## Envelope (return the nested wrapper; flat form silently passes and is WRONG)
hard-deny:
```python
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "deny", "permissionDecisionReason": "<exact reason string, BYTE-FOR-BYTE>"}}
```
advisory:
```python
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "<exact advisory text>"}}
```

## Fidelity rules (faithful engine-ification, NOT redesign — mirror subagent_sandbox/engine.py's discipline)
1. Do NOT change any deny CONDITION. The blocked set must be identical before/after.
2. Preserve every `permissionDecisionReason` / advisory string BYTE-FOR-BYTE from the `.sh`.
3. Preserve every escape-hatch env var (grep the `.sh` for `COORDINATOR_`/`OVERRIDE`/`ALLOW`) — read `os.environ` exactly as bash read `$VAR`.
4. Preserve side-effects (state-file writes / log appends), wrapped so a failure can NEVER flip the decision (mirror bash `|| true`).
5. Subagent detection: `payload.get("agent_id")` present ⇒ subagent (bare-hex `^[a-f0-9]{12,}$` or named-teammate). Replicate the `.sh`'s exact detection source.
6. Fail direction matches the `.sh`: hard guards fail-CLOSED where bash emits deny on error; advisories/fail-open guards return None on error.
7. Import-safe (no logic at import time). No stdin read (engine passes `payload`). Python 3.11+, stdlib only, plus `yaml` if the guard parses YAML frontmatter (already a claude-klabauter dep; see subagent_sandbox).
8. Reuse shared helpers from `coordinator_core.ops._path_guard` (e.g. `contained_path`) and mirror subagent_sandbox's path-normalization helpers where the guard normalizes paths — don't reinvent.
9. Provenance: top-of-file docstring citing the DoE `.sh` reference path (`coordinator/hooks/scripts/<name>.sh`).

Only write your assigned `<name>.py` module(s). Do NOT edit engine.py, __main__.py, or hooks.json.
