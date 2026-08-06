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

## (c) bash-as-required-parser — RESOLVED/retired 2026-07-22, no site remains

Syntax-checking an externally-authored `.sh` script with `bash -n` where bash itself is the tool
under test — there is no native substitute for "does bash accept this syntax," so this was not
the reimplementable bridge the mandate targets; scoped strictly to parse-checking, never
execution. Its only named site, `coordinator_core/snippet_sync/verify_registry_consistency.py`
`_bash_n()`, was deleted with the retired four-script (`verify-<X>-sync.sh`) leg it parse-checked
— those scripts are retired everywhere (example-doctrine-repo `b644d5a9`), so there was nothing left to
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
- `coordinator_core/install/sandbox_check.py` `_tier1b_mirror_and_cold_tier()` (~:971, cold-shell `source '<sandbox>/.claude/shell/claude-doe-shim.sh'` reading back `REPO_EXAMPLE_DOCTRINE_REPO`; the shim is generated by claude-klabauter's own `coordinator_core.ops.gen_claude_doe_shim`)

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
(rendered in `main()`) both only assemble `#!/bin/sh` hook-body *text*; git
execs that text as a separate process at hook-invocation time, not as a
Python subprocess call this module makes. `edit_live_hook.py` `cmd_commit()`
is the one class-(b) site with a real subprocess call and is entered below.

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
  argv_digest: "f946de6da819"
  reason: "live-shell artifact under test: sourcing claude-klabauter-generated claude-doe-shim.sh to assert its runtime effect"
  ruled_on: "2026-07-21"
```

## Adjudicated and CLOSED 2026-07-21 — no longer a residual

`coordinator_core/install/substrate.py` `_write_agent_forwarder` (formerly a generated
`#!/bin/sh` bin-forwarder onto the coordinator-claude/example-doctrine-repo tree) was the one open scope decision
under this section. Resolved as **native port**, not a carve-out (`daca1c74`): the forwarder emits
`#!/usr/bin/env python3`. The carve-out case did not survive inspection — its `sh` shebang was
never load-bearing, because both target classes resolve their sibling `lib/` from their OWN file
location (`BASH_SOURCE[0]` / `__file__`) and are indifferent to what exec'd them. Recorded because
the set above is closed by construction: it must name what execute actually resolved, and this one
resolved to zero.

## Related, not a carve-out

The polyglot trampoline blessing ("~1-line polyglot trampoline inside an otherwise-Python CLI")
is **retired** (2026-07-21): none remain in this repo, and the actioned
`cross-repo/archive/2026-07-21-claude-central-em-cross-repo-memo-depolyglot-pure-python-claude-klabauter.md`
memo names the polyglot shape itself as legacy debt to migrate out, not a pattern to keep minting.
Interpreter resolution is via shebang, never a trampoline.
