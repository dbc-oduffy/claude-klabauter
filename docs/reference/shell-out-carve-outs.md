# Sanctioned shell-out carve-outs

> The closed list referenced by `CLAUDE.md` § Runtime conventions. New automation is naked
> Python (3.11+); these are the ONLY sanctioned residual shell-outs. Extended only by a new PM
> ruling, never by local judgment call.
>
> Source mandate: `cross-repo/archive/2026-07-15-claude-central-em-bash-to-python-mandate.md`
> (DR-047/DR-059).

## The two governing rules

**Contingent irreducibility is not a reason — it is the migration debt itself.** A spawn whose
necessity depends on some *artifact* remaining a shell script is never carve-out material,
however irreplaceable it looks today: the correct exit is that artifact's port, not a sanction.
This is not theoretical — `claude-doe`, `coordinator-initiative`, `resolve-coordinator-clone.sh`,
`prereq_probe.sh`, and `<atlas>.watch.sh` each looked irreducible and each dissolved within days
of the artifact being ported. The classes below are sanctioned because their irreducibility is
**permanent**: their subject is the interpreter or shell *itself* (c/d), the consumer is `git`
(b), or the artifact is a third party's to own (a). "Only bash can observe what this shell script
does" is true of every shell script ever written and is therefore not an argument.

**Enumeration is constitutive, not illustrative.** A site is a member only if it is **named** in
its class's `Sites:` list. A site that satisfies a class's rationale but is not named is **not**
sanctioned — it is a violation until the PM names it. Arguing membership from a class's rationale
is precisely the local judgment call this forbids.

Sites are anchored on the enclosing function (stable across edits); line numbers are hints only.

**Artifact-class disposition is a distinct question from site sanction, and this doc does not
answer it.** "May a pre-existing `.sh`/extensionless FILE persist at all?" is adjudicated by
`coordinator_core/ops/check_posix_exec_assumptions.py`'s `EXEMPTIONS` / `EXEMPT_PREFIXES`
registers, not by this doc's `Sites:` lists — those govern only "may Python spawn a shell at this
call-site?" The two governing rules above bind both questions alike: contingent irreducibility is
migration debt regardless of which register would record it, and enumeration is constitutive in
both (a granted `EXEMPTIONS`/`EXEMPT_PREFIXES` entry, not a rationale match, is what makes a file
invisible to that guard). See that module's own docstring for the artifact-side admission tests.

## Hot-path classification — commit and session-start

`CLAUDE.md` § Runtime conventions: "a bash-spawning script or hook on the commit/session hot path
is **break-class**, reimplemented in naked Python as touched." That standard is stricter than
"carve-out-worthy" — a hot-path site is fix-by-default even if its rationale would otherwise
survive class (a)-(f) scrutiny. This section names the hot-path subset explicitly rather than
leaving it a property a reader has to infer per-entry.

**Session-start hot path: zero sites below touch it.** No `Sites:` entry in classes (a)-(f) runs
on SessionStart; those hooks are already naked Python outside this doc's scope.

**Commit hot path: zero *Python-side bash spawns* touch it, but one site's *generated artifact*
is what actually execs on every `git commit`.** The distinction matters because it is easy to
conflate "generates/validates a commit-time artifact" with "runs on every commit":

- The three class (b) sites (`install_meta_repo_precommit_hook.py` `main()`,
  `install_publish_repo_precommit_hook.py`'s `_FRESH_HOOK_TEMPLATE`, `edit_live_hook.py`
  `cmd_commit()`) run at **install-time or hook-edit-time** — fresh install, or an operator
  running the `edit-hook` CLI subcommand — never on a user's ordinary `git commit`. `cmd_commit()`
  in particular is a subcommand name of the edit-hook CLI, not literal `git commit` invocation;
  don't let the name imply per-commit execution.
- What DOES run on every `git commit` is the **generated hook file itself** — a `#!/bin/sh` shim
  that git execs directly. That artifact is POSIX-`sh`, not bash, and git-for-Windows ships `sh`
  on Windows — so the actual per-commit hot-path execution is already portable. This is the
  concrete fact AC9's "the only commit-path sites are POSIX-`sh` hook generators" verdict rests
  on: the *generators* are cold-path Python (with a POSIX-sh validation spawn in one case), and
  the *artifact they produce* is the true hot-path exec, and it's `sh`, twinned by construction.

No other class's sites are on either hot path — (a) and (d) are install-time, (e) is scoped
by its own text to "never on an install, ceremony, commit, or session execution path," and (f)
is an optional verification-only tool. See the per-site table below.

## Per-site classification

| Site | Class | Shape | Hot-path? |
|---|---|---|---|
| `install/first_run.py` `_install_homebrew()` | (a) | bash-specific (`curl \| bash` as-published) | No — install-time |
| `install/substrate.py` `_fnm_step()` | (a) | bash-specific (`curl \| bash` as-published) | No — install-time |
| `ops/install_meta_repo_precommit_hook.py` `main()` | (b) | POSIX-sh generic (assembles `#!/bin/sh` shim text; no subprocess spawn itself) | No — fresh-install-time; its *output* is the hot-path artifact (see above) |
| `ops/install_publish_repo_precommit_hook.py` `_FRESH_HOOK_TEMPLATE` (in `main()`) | (b) | POSIX-sh generic (shim-body constant; no subprocess spawn itself) | No — install-time; its *output* is the hot-path artifact (see above) |
| `ops/edit_live_hook.py` `cmd_commit()` | (b) | POSIX-shell generic (`sh -n` validation, narrowed from bash) | No — operator-invoked edit-hook CLI subcommand, not literal `git commit` |
| `install/first_run.py` `_bash_version_ok()` | (d) | bash-specific (interpreter self-probe) | No — install-time |
| `ops/normalize_env.py` `_ne_verify_bash_profile_repair()` | (d) | bash-specific (login-shell self-probe) | No — install-time (post-repair verify) |
| `install/prereq_probe.py` `probe_pwsh()` (pwsh leg) | (d) | PowerShell-specific self-probe | No — install-time prereq probe |
| `install/prereq_probe.py` `probe_pwsh()` (powershell.exe fallback leg) | (d) | Windows-PowerShell-specific self-probe, already the Windows leg itself | No — install-time prereq probe |
| `install/prereq_probe.py` `shell_login_env_reconstruction_source()` | (d) | zsh-specific self-probe (macOS-only path) | No — install-time |
| `install/sandbox_check.py` `_tier1b_mirror_and_cold_tier()` | (e) | bash-specific, scoped to verification harness by its own carve-out text | No — explicitly excluded from install/ceremony/commit/session paths by the class-(e) rationale itself |
| `coordinator/bin/static-check` `run_pyright()` | (f) | 3rd-party Node CLI, PATH-resolved, degrade-to-UNAVAILABLE | No — optional verification tool, not on any required path |

## Adversarial standard applied — no carve-out fails it

Applying the same adversarial standard the plan's `EXEMPTIONS` register audit applies (a carve-out
survives only if its irreducibility is permanent, not merely inconvenient-to-port): every site
above is cold-path already, so none is subject to the hot-path fix-by-default rule in the first
place. Within the cold-path set:

- (a) installer sites are permanently irreducible — reimplementing a 3rd party's installer logic
  is drift-prone churn the mandate's own text excludes, not carve-out convenience.
- (b) sites' `sh` shape is required by git's own hook-exec contract (git execs hooks via `sh` on
  every platform including Windows), so `sh` is not a portability gap to fix — it is the interop
  surface itself.
- (d) sites interrogate the interpreter/shell's own state, which has no native substitute by
  construction (asking bash its own version, or a login shell its own reconstructed PATH).
- (e)'s one site is scoped by its own anti-loophole teeth to verification-harness-only, and its
  artifact is intrinsically shell-shaped (mutates the sourcing shell's own environment) — porting
  it would destroy the thing under test.
- (f)'s one site is an optional, PATH-resolved, degrade-to-UNAVAILABLE convenience outside
  `coordinator_core/`'s own gate scope.

No entry in this doc rests on "porting was inconvenient" — the standard this audit checked for.

## (a) 3rd-party upstream installer invoked as-published

Homebrew's and fnm's own `curl | bash` / `curl | sh -s` installer contracts, run verbatim as
upstream publishes them. Reimplementing a 3rd party's installer logic in Python is drift-prone,
negative-value churn — not our surface to own.

Sites:
- `coordinator_core/install/first_run.py` `_install_homebrew()` (~:500, Homebrew curl|bash spawn)
- `coordinator_core/install/substrate.py` `_fnm_step()` (~:1089, `curl -fsSL .../install | bash -s -- --skip-shell`)

## (b) git-hook execution surface — ONLY where git itself execs the artifact

Generating or validating a `#!/bin/sh` git-hook body, because git execs hook files via `sh` on
every platform (Windows included), so a POSIX-sh shim that execs Python **directly** adds ZERO
dependency beyond what running git hooks at all already requires.

Anti-loophole teeth (PM ruling 2026-07-21 — a contained carve-out, not a loophole for a camel to
stroll through):

- Sanctioned ONLY for (i) generating a git-hook shim body, or (ii) validating a git-hook artifact
  before installing/swapping it — never for general script execution, convenience wrappers,
  "hook-adjacent" tooling, or any artifact git itself does not exec. Proximity to hooks is not
  membership.
- Every invocation MUST name the specific hook file it acts on; an unnamed/generic target is out
  of the carve-out by construction.
- This rationale does not transfer to non-hook surfaces. If the artifact is not exec'd by git,
  this carve-out does not apply, full stop.
- Standing reduction target (see `docs/decisions/` C20 git-hook-minimization work): the *number*
  of local git hooks is itself minimized, preferring GitHub Actions CI/CD; a new local git hook
  requires PM approval.

Sites:
- `coordinator_core/ops/install_meta_repo_precommit_hook.py` `main()` (~:190, fresh-install `#!/bin/sh` shim-body generation)
- `coordinator_core/ops/install_publish_repo_precommit_hook.py` `_FRESH_HOOK_TEMPLATE` (~:64, shim-body constant rendered in `main()`)
- `coordinator_core/ops/edit_live_hook.py` `cmd_commit()` (~:227, `sh -n` validation before an atomic live-hook swap — narrowed from `bash -n` to `sh -n` at execute)
- `coordinator_core/ops/install_lfs_pre_push_hook.py` `_HOOK_TEMPLATE` (the `#!/bin/sh` **`pre-push`** LFS-gate body, rendered by `install()`; tracked source of truth so the gate survives a re-clone — C8/AC7, DR-223's `pre-push` row)

## (c) bash-as-required-parser — RESOLVED/retired 2026-07-22, no site remains

Syntax-checking an externally-authored `.sh` script with `bash -n` where bash itself is the tool
under test — there is no native substitute for "does bash accept this syntax," so this was not
the reimplementable bridge the mandate targets; scoped strictly to parse-checking, never
execution. Its only named site, `coordinator_core/snippet_sync/verify_registry_consistency.py`
`_bash_n()`, was deleted with the retired four-script (`verify-<X>-sync.sh`) leg it parse-checked
— those scripts are retired everywhere (DoE `b644d5a9`), so there was nothing left to
parse-check.

Kept as historical record per the enumeration-is-constitutive rule: the class's site list
resolved to zero, it is not silently dropped.

## (d) interpreter/shell self-probe

Invoking bash/sh to interrogate its OWN version, presence, or login-shell environment — never to
execute a `.sh` file's logic. There is no native substitute for asking the interpreter about
itself (bash's own version banner) or for asking a fresh *login* shell whether a profile repair
took effect — the login shell is the subject under test and is unobservable from a Python
process's own env.

Anti-loophole: scoped strictly to probing the interpreter/shell's own state; the moment a spawn
hands bash a script's logic to execute, it is out of this class.

Sites:
- `coordinator_core/install/first_run.py` `_bash_version_ok()` (~:189, `[bash_path, "--version"]`)
- `coordinator_core/ops/normalize_env.py` `_ne_verify_bash_profile_repair()` (~:437, `[bash, "-lc", …]` login-shell PATH/`claude`-resolution verify after a `~/.bash_profile` repair)
- `coordinator_core/install/prereq_probe.py` `probe_pwsh()` (~:274, `["pwsh", "--version"]` interpreter version self-probe)
- `coordinator_core/install/prereq_probe.py` `probe_pwsh()` (~:305, `["powershell", "-Command", "$PSVersionTable.PSVersion.ToString()"]` legacy-fallback interpreter version self-probe)
- `coordinator_core/install/prereq_probe.py` `shell_login_env_reconstruction_source()` (~:661, `["zsh", "-lc", 'printf %s "$PATH"']` intact-login-shell PATH read used as a reconstruction anchor)

## (e) live-shell-environment artifact under test

Sourcing a shell artifact claude-klabauter's own install path **generates**, solely to assert its runtime
effect on a live shell's environment, inside install-verification harness code. Sanctioned only
because the artifact class is **intrinsically shell-shaped and therefore permanently
unportable**: the artifact's entire contract is to set variables in the shell that sources it,
which no Python process can perform or observe — porting the artifact would destroy its function,
so the contingent-irreducibility exclusion does not reach it. A native reimplementation of the
check would assert claude-klabauter's Python rather than the shipped artifact, converting a real end-to-end
assertion into a tautology and turning a green install-verification into a lie.

Anti-loophole teeth — a one-artifact carve-out, not a testing exemption:

- The artifact's contract must be **to mutate the sourcing shell's own environment** (exported
  variables, shell functions, PATH). An artifact that merely *runs* under a shell — a
  health-check script, a resolver, a probe library, a generator — is portable by definition and
  is out of this class, full stop. Being a `.sh` file is not membership.
- **Observe only, never consume.** Sanctioned solely to *assert* the artifact's behavior in a
  verification harness. A spawn whose output feeds claude-klabauter's own execution is a dependency, not a
  test, and is out of this class regardless of the artifact.
- Sanctioned **only** inside verification/sandbox harness code — never on an install, ceremony,
  commit, or session execution path.
- Every invocation MUST name the specific artifact file it sources; a glob, a directory walk, or
  a caller-supplied path is out of the carve-out by construction.
- The artifact must be one **claude-klabauter itself generates**. A third party's or a sibling repo's
  pre-existing script consumed as a dependency is out of this class — that is the shape class (e)
  exists to keep out.
- This rationale does not transfer. If the artifact under test would still function as a Python
  file, this carve-out does not apply.

Sites:
- `coordinator_core/install/sandbox_check.py` `_tier1b_mirror_and_cold_tier()` (~:1203, cold-shell `source '<sandbox>/.claude/shell/claude-doe-shim.sh'` then calling the `claude()` wrapper it defines, to read back the argv it hands `claude-doe`; the shim is generated by claude-klabauter's own `coordinator_core.ops.gen_claude_doe_shim`)

## (f) optional 3rd-party static-verification tool, PATH-resolved, degrades to UNAVAILABLE

Invoking an optional, verification-only 3rd-party CLI (not a coordinator/claude-klabauter dependency) that
this repo's own runtime NEVER requires — resolved only via `shutil.which` (never a hardcoded
path), and required to degrade to a truthful UNAVAILABLE verdict + exit 0 when the tool is absent,
rather than fail obscurely. Distinct from class (a): this is not an installer run as-published,
it's an ordinary CLI invocation of a tool that may simply not be on the host. Sanctioned because
the artifact is a genuinely optional convenience — nothing on a hot path, install path, or test
may depend on its presence (`coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py`
is the standing gate that keeps `coordinator_core/` production code free of any such dependency;
this site lives in `coordinator/bin/`, outside that gate's scan root, and calls the tool directly
rather than shelling to node).

Provenance, stated precisely — 2026-08-11. The PM directed that the gap be FIXED rather than
queued (`state/improvement-queue/2026-08-11-a-confined-executor-cannot-verify-any-st-9b6d190ee07e.yaml`,
filed and then superseded by that direction). That is authorization for the fix; the class-(f)
shape below — a new class rather than a fold into (a)-(e) — is the EM's implementing decision,
not a PM ruling on the taxonomy. If the PM would rather this list not gain a class, the site is
the thing to keep and the row is the thing to re-home.

The problem it answers: a confined dispatched executor's Bash allowlist has no path to answer a
"zero new Pyright errors" brief without an in-repo, `python3 <script>`-invocable wrapper. `pyright`
is a Node.js tool and this repo requires no Node runtime for its own work, so the resolution had to
be narrow, optional, and degrade-to-UNAVAILABLE rather than either porting pyright to Python or
abandoning static-check verdicts in briefs.

Sites:
- `coordinator/bin/static-check` `main()`/`run_pyright()` (pyright, PATH-resolved via `shutil.which("pyright")`/`shutil.which("pyright.cmd")`, `--outputjson` invocation; UNAVAILABLE + exit 0 when unresolved)

## Machine-readable register

The block below is the parseable form of the `Sites:` bullets above, read by
`coordinator_core.spawn_policy.allowlist.load_allowlist`. **Enumeration is
constitutive, not illustrative** — this block carries the same rule as the
prose: a site that satisfies a class's rationale but is not named as an entry
below is NOT sanctioned, full stop. The parser must never be able to infer
membership from a rationale, and this block must never be treated as a
second, independent list — it is the existing register, machine-readable.

`argv_digest: null` on every entry is a deliberate, time-boxed bootstrap:
the digest cannot exist until the detector (`spawn_policy.detect`) lands.
Chunk C2b backfills every digest once it does; until then an entry matches
on `(path, enclosing, argv0, ordinal)` alone, which is weaker than a pinned
digest match. See `tasks/shell-spawn-regrowth-gate/PINNED-API.md` § "The
unpinned-digest bootstrap".

Class (c) is omitted below — it resolved to zero sites (see above) and has
no entries to seed.

Two `Sites:` bullets above are NOT represented as entries because they name
no AST-detectable spawn call — `install_meta_repo_precommit_hook.py`
`main()` and `install_publish_repo_precommit_hook.py`'s `_FRESH_HOOK_TEMPLATE`
(rendered in `main()`) only assemble `#!/bin/sh` hook-body
*text*; git execs that text as a separate process at hook-invocation time,
not as a Python subprocess call this module makes. `edit_live_hook.py`
`cmd_commit()` is the one class-(b) site with a real subprocess call and is
entered below.

```yaml shell-out-allowlist
- cls: a
  path: coordinator_core/install/first_run.py
  enclosing: _install_homebrew
  argv0: bash
  ordinal: 0
  argv_digest: "11830d9482fa"
  reason: "3rd-party installer consumed as-published (Homebrew curl|bash)"
  ruled_on: "2026-07-21"
- cls: a
  path: coordinator_core/install/substrate.py
  enclosing: _fnm_step
  argv0: bash
  ordinal: 1
  argv_digest: "eda08125d181"
  reason: "3rd-party installer consumed as-published (fnm curl|bash)"
  ruled_on: "2026-07-21"
- cls: b
  path: coordinator_core/ops/edit_live_hook.py
  enclosing: cmd_commit
  argv0: sh
  ordinal: 0
  argv_digest: "12f4f3e95915"
  reason: "sh -n validation of a git-hook artifact before an atomic live-hook swap; git itself execs hooks via sh"
  ruled_on: "2026-07-21"
- cls: d
  path: coordinator_core/install/first_run.py
  enclosing: _bash_version_ok
  argv0: <dynamic>
  ordinal: 0
  argv_digest: "d8cdba69fe01"
  reason: "interpreter self-probe: bash --version banner"
  ruled_on: "2026-07-21"
- cls: d
  path: coordinator_core/ops/normalize_env.py
  enclosing: _ne_verify_bash_profile_repair
  argv0: bash
  ordinal: 0
  argv_digest: "c660e23c1bfc"
  reason: "shell self-probe: login-shell PATH/claude-resolution verify after a ~/.bash_profile repair"
  ruled_on: "2026-07-21"
- cls: d
  path: coordinator_core/install/prereq_probe.py
  enclosing: probe_pwsh
  argv0: pwsh
  ordinal: 0
  argv_digest: "a0925537d581"
  reason: "interpreter self-probe: pwsh --version banner"
  ruled_on: "2026-07-21"
- cls: d
  path: coordinator_core/install/prereq_probe.py
  enclosing: probe_pwsh
  argv0: powershell
  ordinal: 1
  argv_digest: "0807be5774fb"
  reason: "interpreter self-probe: legacy Windows PowerShell 5.1 version banner fallback when pwsh is absent"
  ruled_on: "2026-07-21"
- cls: d
  path: coordinator_core/install/prereq_probe.py
  enclosing: shell_login_env_reconstruction_source
  argv0: zsh
  ordinal: 0
  argv_digest: "6d95989913ab"
  reason: "shell self-probe: intact macOS-default zsh login shell's own effective PATH, used as reconstruction anchor for a corrupted bash login PATH"
  ruled_on: "2026-07-21"
- cls: e
  path: coordinator_core/install/sandbox_check.py
  enclosing: _tier1b_mirror_and_cold_tier
  argv0: bash
  ordinal: 0
  argv_digest: "525a2a1c1a87"
  reason: "live-shell artifact under test: sourcing claude-klabauter-generated claude-doe-shim.sh to assert its runtime effect"
  ruled_on: "2026-07-21"
- cls: f
  path: coordinator/bin/static-check
  enclosing: run_pyright
  argv0: <dynamic>
  ordinal: 0
  argv_digest: "aacb8d29a475"
  reason: "optional 3rd-party static-verification tool, PATH-resolved only, degrades to UNAVAILABLE + exit 0 when absent"
  ruled_on: "2026-08-11"
```

## Adjudicated and CLOSED 2026-07-21 — no longer a residual

`coordinator_core/install/substrate.py` `_write_agent_forwarder` (formerly a generated
`#!/bin/sh` bin-forwarder onto the coordinator-claude/DoE tree) was the one open scope decision
under this section. Resolved as **native port**, not a carve-out (`daca1c74`): the forwarder emits
`#!/usr/bin/env python3`. The carve-out case did not survive inspection — its `sh` shebang was
never load-bearing, because both target classes resolve their sibling `lib/` from their OWN file
location (`BASH_SOURCE[0]` / `__file__`) and are indifferent to what exec'd them. Recorded because
the set above is closed by construction: it must name what execute actually resolved, and this one
resolved to zero.

## Reconciled against the directive-verb migration — 2026-08-19

`docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md` § C9 reconciled this list against
every directive-dispatch verb the plan's migration touched (`pickup_assemble`, `baton_assemble`,
the three completion tables, `consolidate_assemble`, `merge_assemble`, `backlog_grind_assemble`).
Each migration chunk (C3-C8) recorded, at the site of its own dispatch table, a live discriminator
finding of whether any of its verbs spawn `bash`/`sh` and therefore fall inside this doc's scope.
Every chunk found **none** — verbs either resolved to a registered op, called `git` plumbing
directly off a hardcoded argv, invoked an existing `coordinator/bin/*.py` script via
`sys.executable`, or (one case, `node-ceremony-gate`) spawned `node --test` — never `bash`/`sh`.

**Outcome: no entry above is obsolete, and no verb needs to be added.** Nothing in the migrated
population was ever a member of this list (its `Sites:` are install/hook-generation/interpreter-
self-probe/pyright surfaces, disjoint from the assembler `apply.py` modules by construction), so no
verb "stopping spawning" could retire an entry here, and no verb "must keep spawning" (bash/sh
specifically) was found un-listed. Per this doc's closed-list rule, that finding is recorded rather
than acted on — there is nothing to remove and nothing to flag to the PM.

## Related, not a carve-out

The polyglot trampoline blessing ("~1-line polyglot trampoline inside an otherwise-Python CLI")
is **retired** (2026-07-21): none remain in this repo, and the actioned
`cross-repo/archive/2026-07-21-claude-central-em-cross-repo-memo-depolyglot-pure-python-claude-klabauter.md`
memo names the polyglot shape itself as legacy debt to migrate out, not a pattern to keep minting.
Interpreter resolution is via shebang, never a trampoline.
