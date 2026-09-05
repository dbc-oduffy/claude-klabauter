# Linux cloud dogfood — second run, 2026-09-05

Companion to [`linux-cloud-dogfood-friction.md`](linux-cloud-dogfood-friction.md), which
records the first run (session `01FLY84g...`, merged as PR #5). This is a **second,
independent run** on a different container, started before that PR merged and finished
after it. It deliberately records only what the first log does not already have.

**Environment.** Debian, kernel 6.18 x86_64, Python 3.11.15 at `/usr/local/bin/python3`,
bash 5.2, GNU coreutils, **root**, no Homebrew, no GPU, no GUI, no keychain, no TTY,
and — structurally — no way to restart Claude Code. Claude Code CLI 2.1.261.

**Read the overlap note first (§0).** Two sessions found the same warm-server miscount
independently, and on the one place the fixes differ, the first run's is better. That is
worth more than either patch.

---

## 0. Overlap with the first run, including where this run was wrong

Both runs independently hit the resident-warm-server miscount and fixed it the same way
(drop a match whose parent is also a match). The first run's version is the better one and
is what is on `main`: it pops `ppid` before returning, leaving the enumerator's documented
return shape unchanged, where this run's left it in and silently widened a contract two
probes read. Nothing from this run's version was carried; the only thing added here is the
**test** that fix shipped without (`bin/tests/test_claude_klabauter_doctor_forked_pool_workers.py`).

More important, and the reason this section leads: **the first run fixed the cause, this run
only fixed the symptom.** This run saw 31 warm-server processes, concluded the *probe* was
miscounting, and stopped there. The first run also capped `DISPATCH_PROCESS_POOL_SIZE` at
the core count — the 30 forked workers at ~78 MB each (~2.4 GB on a 4-core box) were a real
resource defect, not just a counting error. Had only this run's patch landed, the probe
would report `PASS` while 2.4 GB of unnecessary processes stayed resident. A probe fix that
makes a real problem *stop being visible* is worse than no fix, and that is what this run
had until the two were compared.

The `.gitignore` entries for `*.egg-info/` and `.coordinator-local/` were also found twice,
independently, from the same trigger: `check-persona-names` failing on `PKG-INFO`'s author
field. The first run's landed; this run's is dropped as duplicate.

---

## 1. The warm-engine door does not compile on Linux **[fixed here]**

`build_posix.py` compiles `door_posix.c` + `door_core.c` with `-O2 -Wall -Wextra -std=c11`.
On glibc that dialect is **strict ISO C**, which hides every POSIX declaration behind the
feature-test macros. So `readlink`, `sigemptyset`, `sigaddset`, `CLOCK_MONOTONIC` and
`O_CLOEXEC` are all undeclared on Linux despite `<unistd.h>`, `<signal.h>`, `<time.h>` and
`<fcntl.h>` all being correctly included — and under C99-and-later rules an implicitly
declared function is an **error**, not a warning:

```
door_posix.c:390:17: error: call to undeclared function 'readlink'
door_posix.c:1045:9: error: call to undeclared function 'sigemptyset'
door_posix.c:257:19:  error: 'CLOCK_MONOTONIC' undeclared
door_posix.c:294:36:  error: 'O_CLOEXEC' undeclared
7 errors generated.
```

Darwin's libc exposes these regardless of dialect. The module docstring's standing claim —
*"VERIFIED ON macOS 2026-08-22 … It has NOT been run on Linux"* — was accurate, and this is
what was waiting there: not a runtime bug but a build that could never start.

**`door_posix.c` itself is not at fault.** It carries a correct `__APPLE__`/`#else` split
with a real Linux branch using `/proc/self/exe`. The Linux branch was right all along; it
simply could not be compiled.

**Why three portability audits missed it.** This run swept `hooks/` (128 files),
`bin/`+`skills/` (1289) and `coordinator_core/` (~4300) for hardcoded macOS paths,
BSD-vs-GNU flag hazards and macOS-only binaries, and came back essentially clean — correctly.
A Python-level audit cannot see a C compiler dialect flag. Worth remembering before reading
a clean grep sweep as a portability verdict.

Fixed with `-D_POSIX_C_SOURCE=200809L` applied **only off Darwin**: defining it on macOS
switches those headers into strict-POSIX mode and would hide the Darwin extensions the file
uses under `__APPLE__` (`<mach-o/dyld.h>`'s `_NSGetExecutablePath`). Verified: both
translation units compile and link clean; `test_door_install_posix_build.py` 5P/2F → 7/7.
The docstring and `--help` are corrected in the same change — it now *compiles* on Linux
but still has not been *invoked* there.

**Open question for whoever owns the door:** a build that could never succeed on Linux means
the door leg has never run there, so whatever it would have surfaced downstream is still
unknown.

---

## 2. The plugin denies Bash, user questions, and repo writes on this host **[not fixed]**

The most severe finding in this run, and one neither run could observe until a session
**resumed** with the plugin already loaded — installing it mid-session does not activate its
hooks, so the first run's "structurally no session restart" note stops one step short of
this consequence.

Once the coordinator plugin is active in Claude Code on the web, three denials appear in
sequence:

1. **Every Bash call**, including `echo`:
   > `http-hook-forwarder: the env override channel was declared but vetoed by an httpHookAllowedEnvVars setting -- the Bash guard did not run, denying rather than forwarding an emptied env`

   The mechanism is documented in `hooks/forwardable-env-vars.json`: on the `type:"http"`
   transport a caller's environment reaches the guard **only** through
   `X-Coordinator-Env-<NAME>` registration headers. This host's `httpHookAllowedEnvVars`
   policy vetoes them, and the forwarder fails closed. That is the correct direction to fail,
   but the result is a shell-less session.
2. **`AskUserQuestion`**, via the same forwarder — so the agent cannot even ask the operator
   how to proceed.
3. **Writes into the repo**, via `bump_out_of_repo_tool_write`, which classified a
   *top-level* session as a dispatched subagent — *"no PM here — report to the EM that
   dispatched you"* — and confined it to a subagent sandbox.

**Probable root cause of (3), and it ties back to the restart gate.** `baton-assemble`
reports `repo_identity: UNRESOLVED — no registry record for this session (registry holds 1
file(s), 0 parsed)`. `/coordinator:repo-setup` never ran, because it is restart-gated and an
agent cannot restart itself. No session record exists → the session reads as unregistered →
its writes are sandboxed.

So the failure composes: **a host that cannot restart Claude Code cannot complete
registration, and the guards then progressively deny the shell, the ability to ask, and the
ability to write.** The system becomes unusable on the platform being dogfooded, and each
individual denial is behaving exactly as designed.

No override was attempted. The guards are safety mechanisms and the one that fired said to
report rather than proceed — which is what this section is.

---

## 3. `/handoff` cannot run in this repo **[not fixed]**

`baton-assemble apply` commits the artifact into `state/handoffs/`, but this repo's own
`.gitignore` ignores `state/` wholesale (line 22). The result is an unhandled `RuntimeError`
out of `coordinator_core/contract/apply_base.py:1357`, not a diagnosed refusal:

```
RuntimeError: git add state/handoffs/<file>.md failed (rc=1): The following paths are
ignored by one of your .gitignore files: state
```

Two parts of the same repo contradict each other. The handoff for this run was written from
`coordinator-claude` instead, which ignores only three specific `state/` files. The failed
attempt also leaves empty `state/handoffs/` directories behind that need manual `rmdir`.

Note this interacts with §2: on an ephemeral cloud container, a handoff written to an ignored
`state/` is not merely untracked, it is **lost** when the container is reclaimed.

---

## 4. `repos.doe_claude` blocks ceremonies outright, not just the shim

The first run fixed this in `setup.py` (persist the resolved coordinator-claude root), which
is the right layer. Recording the symptom for completeness, because it is stronger than
"the `claude()` shim breaks": with the key unset, `baton-assemble brief` dies in
`coordinator_core/resolution/facade.py` with

```
OperatorConfigError: 'doe_root' resolved to a corrupt value '' (empty or whitespace-only)
```

so the entire baton/handoff lane is unavailable, and the message blames operator-authored
config for a value the installer was holding. On a box predating that fix the manual
remedy is `machine-local set repos.doe_claude <coordinator-claude-clone>`.

Separately: the error `gen-doe-root-pointer` prints cites *"complete step 3.5a first"*, and
there is no §3.5a in the shipped `commands/install.md`. `bin/ensure-doe-clone.py`'s docstring
still cites "lines 731 and 747 of the DoE-claude source", and `docs/safety.md` still narrates
the step as live. A restructure miss: the citation outlived the step.

---

## 5. The documented test command does not work on a documented install

The PR template gates on `python .github/scripts/run-tests.py`, which invokes pytest with
`-n` and therefore needs `pytest-xdist` — which the installer classifies as **advisory** and
does not provision. So a by-the-book install leaves the required command failing with
`unrecognized arguments: -n`, an argparse error that reads as a broken script rather than a
missing dependency. `scripts/setup.py --with-test-deps` fixes it, with two traps: combining
it with `--register-only` silently skips the install (register-only short-circuits first),
and without a fleet-env opt-out it drags the multi-GB fleet-env step along behind it.

Also: `--check` is advertised in this repo's INSTALL.md as a "deterministic check-only" but
prints only `check mode: setup.py is present and executable` and exits 0 — it returns green
on a box whose engine install crashed. coordinator-claude's INSTALL.md is blunt about this;
this repo's is not. `--preflight` is the flag that actually probes.

---

## 6. Not carried into this branch, and why

This run also produced two source fixes that could **not** be transported here, because by
the time the branch needed rebuilding the session had no shell (§2) and remote files can only
be replaced by uploading their full content. Hand-retyping a 600–1400 line module through a
read-and-re-emit loop risks a silent transcription error, which would be worse than not
shipping. Both are correct and reviewed on branch
`claude/klabauter-linux-compat-6tl37i` (PR #7), for anyone with a shell to cherry-pick:

- **`coordinator_core/install/first_run.py`** — the toolchain leg is Homebrew-only with no
  platform or root guard. Homebrew's installer aborts by design as EUID 0, so
  `_install_homebrew()` returned `EXIT_FAIL` and killed the whole first-run flow, reported
  only as a bare non-zero exit. The patch adds host-platform and root detection, a
  package-manager ladder (apt-get/dnf/yum/zypper/pacman/apk), a Homebrew-formula →
  distro-package name map, and a dispatcher; `uv` is special-cased since no mainstream distro
  packages it; `build_plan()` — the operator's consent surface — prints the command that will
  actually run on the box. macOS stays byte-identical. Also adds Linuxbrew
  (`/home/linuxbrew/.linuxbrew/bin/brew`) to the PATH candidates, absent from the whole tree.
- **`coordinator_core/install/shell_rc_guard.py`** — `_resolve_rc_path` defaults to zsh when
  `$SHELL` is unset. Right for macOS, wrong for Linux, and `$SHELL` is most likely unset in
  exactly the containers where the box is bash, so the sentinel block landed in `~/.zshrc` on
  a machine with no zsh. The sibling resolver in `ops/install_shell_init_guard_seam.py`
  already defaulted to bash, so the two disagreed.

Both patches carry tests, and both fix a **test defect worth generalising**: two test files
asserted macOS behaviour *without pinning the platform*, so they described the wrong thing on
any Linux runner and passed only by accident of where they ran. Worth sweeping for that
pattern.

---

## 7. Fleet env, for the record

Same root cause the first run identified (`overrides 7.7.0` → removed `typing.ByteString`
under the 3.14 lock), reached independently. Two additions:

- The step ran **unconditionally with no opt-out**, so every install pays it. Observed cost
  before the failure: `/root/.cache/uv` at **7.0 GB**.
- It resolves a **CUDA** build of torch (`torch==2.13.0+cu130` plus `nvidia-cudnn-cu13`,
  `nvidia-nccl-cu13`, `nvidia-nvshmem-cu13`, `nvidia-cusparselt-cu13`) pinned for
  `sys_platform == 'linux' or 'win32'` with no GPU-presence check and no CPU-only variant.
  On a GPU-less container that is hundreds of MB per package that can never be used — a cost
  macOS does not pay, since it gets CPU wheels by construction.

A `--skip-fleet-env` flag for `scripts/setup.py` was written for this and is on PR #7,
un-transported for the same reason as §6.
