# External workers and named reviewers

Workers use a clean checkout on an unprotected branch. Admission checks the box load and any
live Group EM's slot grant, then creates or reuses `work/<host>/worker-<agent>-<date>`.
The launcher refuses dirty shared trees before changing branches. Run from the target project:

```console
python /path/to/coordinator/bin/agent-worker-launch.py codex --session-id SESSION --run RUN -- -m gpt-6.1-sol
```

On Windows invoke the PowerShell script by path from the existing interactive PowerShell:

```powershell
& 'C:\path\coordinator\bin\agent-worker-launch.ps1' -Agent codex -SessionId SESSION -Run RUN -NativeExecutable 'C:\path\codex.exe'
```

The Python helper only prepares admission, branch and argv. PowerShell launches the native `.exe`
directly. `.cmd` wrappers and Python as the interactive parent violate the console input contract;
see [interactive launch chain](interactive-launch-chain.md). The native executable must already be
installed and authenticated. Substitute `grok` or `agy` for the other supported worker providers.
Their autonomous flags are documented in `agent_pipe.worker_mode.WORKER_ARGV_SOURCE`.

Named reviewer routing is optional. Set `COORDINATOR_REVIEWER_PROVIDER` in the caller environment
before emitting the workflow, for example `coordinator:staff-eng=codex`, or `*=agy` for all eligible
reviewer personas. Exact entries override the wildcard. Unset routing retains the existing reviewer
provider. The emitted workflow submits the persona definition and brief through
`codex_runner.start` with mode `named-review`, then reads `codex_runner.result` until completion.
Each op-relay call executes one engine command. Provider execution runs in the detached worker,
returns the persona's ordinary verdict/findings, and never applies fixes.

A direct engine request has this shape (use the warm `coordinator-invoke` installed on the host):

```json
{"mode":"named-review","repo_root":"/absolute/project","agent_type":"coordinator:staff-eng","provider":"codex","model":"gpt-6-astra","brief":"Review the plan at docs/plans/example.md","verdicts":["READY","READY_WITH_CONCERNS","NOT_READY"]}
```

Pass that JSON to `coordinator-invoke codex_runner.start --params-file REQUEST.json --bare`.
Read the returned handle with
`coordinator-invoke codex_runner.result '{"run_id":"named-review-<12 hex characters>"}' --bare`.
A `running` result means read again later; terminal failures remain explicit failures.
Use the actual persona schema's verdicts, or omit the optional list.
The provider binary and the named persona definition must be available in the worker's settings home.

`agent_pipe.model_tiers` records supported model routing roles and their sources. Codex uses Luna
for the fast role, Sol for workhorse and Astra for frontier. Terra's quality tier is unknown.
Grok reviewers use the documented `grok-build` CLI default alias; its attribution tier remains unknown.
No Fable/Mythos equivalent is asserted, and registry roles are operational selections rather than measured quality equivalence. DeepSeek
uses the documented Flash/Pro API IDs; this registry does not provide a DeepSeek execution adapter.
See [engine ask route](codex-engine-ask-route.md) for the complete ask-to-workflow path and
[non-Claude session identity](non-claude-session-identity.md) for caller identity propagation.

Mistral routing selects `mistral-small-2603` for the fast role and `mistral-large-4` for
workhorse/frontier roles. The official [Small 4](https://docs.mistral.ai/models/mistral-small-4-0-26-03)
and [Large 4](https://docs.mistral.ai/models/mistral-large-4-0) pages publish those API IDs.
[Medium 3.5](https://docs.mistral.ai/models/mistral-medium-3-5-26-04), ID `mistral-medium-3-5`,
was deprecated on 2026-10-31; its documented replacement is Large 4. These are operational role
choices, and no Mistral execution adapter is implied by the registry.
