# Linux install friction log — cloud container dogfood, 2026-09-05

**What this is.** A record of installing coordinator-claude + claude-klabauter from
scratch on Linux, by an agent following the published INSTALL.md as written, with no
prior knowledge of the system. Every item below was hit in practice, not inferred from
reading code. Items marked **[patched]** are fixed on this branch; the rest are logged
because they need a design call, a doc change, or work larger than this pass.

**Environment.** Debian-based container, Linux 6.18 x86_64, Python 3.11.15 at
`/usr/local/bin/python3`, GNU coreutils, bash 5.2, git 2.43, node 22, `uv` present,
`jq` present, `scc`/`pwsh` absent. Running as **root**. No GUI, no keychain, no
Homebrew, no GPU. Claude Code CLI 2.1.261.

**Headline.** The install completes on Linux, and the *Python* source is genuinely
well-hardened for portability — three independent audit sweeps over `hooks/` (128 files),
`bin/`+`skills/` (1289 files) and `coordinator_core/` (~4300 files) found essentially no
hardcoded macOS paths, no BSD-vs-GNU flag hazards, and no macOS-only binaries in shipped
code paths.

The friction sits in three other places. Most of it is **the install chain's platform
assumptions** and **probes and diagnostics only ever exercised on macOS**, where Linux
takes a path nobody has walked or gets reported on incorrectly. But two findings are
sharper than that framing allows, and both were missed by the static audits because
neither is visible in the Python at all: the warm-engine door's C build **cannot compile
on Linux** at the dialect the build script pins (§1.3), and the engine's own test suite
**has never been green on Linux** — ~100 failures present identically at this branch's
parent commit (§4b). The second is the one to act on first: it means none of the rest had
a green baseline to be measured against.

---

## 1. Hard breaks

### 1.1 First-run toolchain provisioning is Homebrew-only, and Homebrew refuses to run as root **[patched]**

`coordinator_core/install/first_run.py` provisions the toolchain (bash, python, node,
uv, git-lfs) exclusively through Homebrew. `_install_homebrew()` was called whenever
`env.brew_ok` was false, with no platform guard and no root guard, and shells out to
Homebrew's official installer. On Linux that installer aborts by design ("Running
Homebrew as root is extremely dangerous"), so `_install_homebrew()` returned
`EXIT_FAIL` and killed the entire first-run flow — reported only as a non-zero exit,
with nothing naming the platform mismatch as the cause. Even for a non-root Linux user
the subsequent PATH probe only checked `/opt/homebrew/bin/brew` and
`/usr/local/bin/brew`, omitting Linuxbrew's actual `/home/linuxbrew/.linuxbrew/bin/brew`
(zero occurrences of `linuxbrew` anywhere in the tree).

**Patched:** added `_host_platform()` / `_running_as_root()`, a Linux package-manager
detection ladder (apt-get, dnf, yum, zypper, pacman, apk), a Homebrew-formula →
distro-package name map, and a `_pkg_install()` dispatcher. macOS behaviour is
byte-unchanged. `_install_homebrew()` now refuses off-Darwin and refuses as root, with
a message naming the reason. `build_plan()` — the operator's consent surface — now
prints the command that will actually run on the box rather than the macOS spelling.
`uv` is special-cased because no mainstream distro packages it. Added Linuxbrew to the
PATH candidates. Tests added for all of it.

Note this also surfaced a latent test defect: `test_build_plan_all_missing_lists_every_
install_step` asserted the macOS plan **without pinning the platform**, so it silently
described the wrong plan on any Linux runner. Now pinned, with a Linux counterpart.

### 1.2 `claude-klabauter.warm.residency` — a hard-severity probe that can never pass on Linux **[patched]**

The doctor probe reported *"Found 31 resident warm server process(es); reachability
cannot be established"* and 31 stale-generation warnings, on a box with exactly one
warm server. This looked like a process leak. It is not.

`ProcessPoolExecutor` defaults to the **`fork`** start method on Linux and **`spawn`**
on macOS and Windows. The warm server builds a 30-worker dispatch pool
(`WORKER_POOL_SIZE = 30`). A forked worker inherits its parent's `cmdline` verbatim, so
all 30 matched `_WARM_SERVER_CMDLINE_SIGNATURE` and were counted as separate residents;
on macOS `spawn` re-execs with a different cmdline, so the platform where this was
developed never saw it. `1 + 30 = 31` matched the reported count exactly.

Consequences: 31 residents exceeded `_WARM_REACHABILITY_PROBE_CAP` (16), so residency —
a **hard-severity** probe — returned `inconclusive` on every Linux run and could not
reach a verdict at all; and one stale generation token was multiplied into 31 warnings.

**Patched:** `_enumerate_resident_warm_servers` now collects `ppid` and drops any match
whose parent is itself a match (`_drop_forked_pool_workers`). Verified live: 31 matched
processes collapse to 1, and residency moved from `inconclusive` to
`PASS — 1 resident warm server process(es), all reachable.` Regression test added
covering the fork case, genuinely independent servers, indirect parentage, and a
missing `ppid`.

### 1.3 The warm-engine door does not compile on Linux **[patched]**

`build_posix.py` compiles `door_posix.c` + `door_core.c` with `-O2 -Wall -Wextra
-std=c11`. On glibc that dialect is **strict ISO C**, which hides every POSIX
declaration behind the feature-test macros — so `readlink`, `sigemptyset`, `sigaddset`,
`CLOCK_MONOTONIC` and `O_CLOEXEC` are all undeclared on Linux despite `<unistd.h>`,
`<signal.h>`, `<time.h>` and `<fcntl.h>` all being correctly included. Under C99-and-later
rules an implicitly-declared function is an **error**, not a warning, so the build fails
outright:

```
door_posix.c:390:17: error: call to undeclared function 'readlink'
door_posix.c:1045:9: error: call to undeclared function 'sigemptyset'
door_posix.c:257:19: error: 'CLOCK_MONOTONIC' undeclared
door_posix.c:294:36: error: 'O_CLOEXEC' undeclared
7 errors generated.
```

Darwin's libc exposes these regardless of dialect, which is exactly why it never
surfaced. Note this is *not* the case the portability audits were looking for — the C
file has a correct `__APPLE__`/`#else` split and a real Linux branch using
`/proc/self/exe`. The Linux branch is right; it just cannot be compiled.

**Patched:** `-D_POSIX_C_SOURCE=200809L`, applied **only off Darwin**. Defining it on
macOS would switch those headers into strict-POSIX mode and hide the Darwin extensions
the file uses under `__APPLE__` (`<mach-o/dyld.h>`'s `_NSGetExecutablePath`), so
restricting it to the platform that needs it leaves the macOS compile byte-identical.
Verified: both translation units compile and link clean, and
`test_door_install_posix_build.py` goes from 5 passed / 2 failed to **7/7**.

This one is worth a second look by someone who owns the door: a build that cannot
succeed on Linux suggests the door leg has never actually run there, so whatever it
would have surfaced downstream is still unknown.

---

## 2. Install-chain gaps

### 2.1 `repos.doe_claude` is never set by either installer

The engine install prints:

> `[ADVISORY] doe-root pointer install skipped ... coordinator will NOT load in any interactive session on this box until this step completes.`
> `doe_root_pointer: skipped (repos.doe_claude not resolved — complete step 3.5a first)`

"DoE-claude" is the pre-scrub internal codename for **coordinator-claude itself**, not a
third repo. Nothing in the shipped install chain ever writes `repos.doe_claude`: the
only writer is an interactive seed prompt in `commands/install.md` Phase 3 that is
**skipped whenever a registry file already exists** — which it does on any box where the
engine installer has run. The registry ends up with `engine.working_repos.doe_claude`
(a different namespace, answering "does this repo work on the engine") but not
`repos.doe_claude`.

Worse, the remediation cites a step that no longer exists: `gen-doe-root-pointer.py`
says "complete step 3.5a first", but the shipped `commands/install.md` is ~400 lines and
has no §3.5a — `ensure-doe-clone.py`'s docstring still cites "lines 731 and 747 of the
DoE-claude source". That is a rename/restructure miss, not operator error.

**Impact is narrower than the message claims.** `coordinator_doe_root()` has a rich
resolution ladder (env → registry → plugin mirror → marketplace cache path →
`CLAUDE_PLUGIN_ROOT`), and the plugin loads fine in Claude Code without the pointer —
the doctor probe confirms `launch.shim_chain PASS` with `doe_root` resolved from the
marketplace cache. What genuinely breaks is (a) the `claude()` shell shim, which has a
hard guard and simply never defines the function if both pointer rungs are absent, and
(b) doctrine-maintainer CLIs (`coordinator-lesson-promote.py`, `cross-repo-memo.py`,
`publish.py`, the `verify-*-sync.py` family) that insist on the canonical key.

**Needs a design call, not a patch here.** The obvious fix is to auto-seed
`repos.doe_claude` from the plugin root at install or SessionStart, mirroring how
`engine.working_repos.doe_claude` already self-heals. That is a registry-contract
change and belongs to whoever owns the codename-scrub migration. Two smaller things are
worth doing regardless: fix the stale §3.5a citation, and soften the message, which
currently overstates the blast radius considerably.

### 2.2 The `machine-local not found` diagnostic was misleading **[patched]**

Compounding 2.1, `_resolve_machine_local()` in `gen_doe_root_pointer.py` probed
`shutil.which("machine-local")` — PATH only. The installer deposits that forwarder at
`<settings-home>/bin/machine-local`, which is **not on PATH** for the process running
`scripts/setup.py`. So on a box where the CLI was present and working, the install
printed "machine-local not found — cannot read registry" and sent the reader to fix a
missing install, when the real cause was an unset key.

**Patched:** checks `<settings-home>/bin/` (and the `.cmd` sibling) before PATH.
Module negative-spec updated to match, since it previously documented PATH-only
resolution as deliberate.

### 2.3 The fleet shared environment: multi-GB, unconditional, GPU-less CUDA, and broken **[partially patched]**

`install_fleet_shared_environment` runs **unconditionally** on every `setup.py` invocation
and had **no skip flag**. It provisions a union of every sibling repo's dependencies —
torch, torchvision, triton, transformers, spacy, umap-learn, trimesh, chromadb,
playwright, 9 tree-sitter grammars, ~200 packages. Observed cost: `/root/.cache/uv`
reached **7.0 GB**.

Two distinct problems:

1. **It pulls a CUDA build of torch on a GPU-less box.** `torch==2.13.0+cu130` plus
   `nvidia-cudnn-cu13`, `nvidia-nccl-cu13`, `nvidia-nvshmem-cu13`, `nvidia-cusparselt-cu13`
   are pinned for `sys_platform == 'linux' or sys_platform == 'win32'` with no
   GPU-presence check and no CPU-only variant. Hundreds of MB per package that can never
   be used. macOS gets CPU wheels by construction, so this cost lands on Linux and
   Windows only.
2. **It then throws the whole environment away.** The health probe fails and the build
   is discarded, so the entire download is pure waste. Root cause: the fleet env targets
   Python **3.14** (`LOCK_PYTHON_MINOR`), and `chromadb` → `overrides==7.7.0` references
   `typing.ByteString`, removed in Python 3.13+. Not platform-specific — it would fail
   identically on macOS — but on Linux you pay 7 GB to discover it.

**Patched:** added `--skip-fleet-env` so a bandwidth-, disk- or GPU-constrained box can
opt out. The step was already advisory-on-failure, so nothing in the engine install
depends on it.

**Still needs work:** unpin or replace `overrides`/`chromadb` and regenerate
`fleet-env.lock` under 3.14; gate the CUDA index behind a GPU check or offer a `+cpu`
variant; and run `_FLEET_ENV_IMPORT_PROBES` at **lock-generation** time so a
`LOCK_PYTHON_MINOR` bump can't ship a lock that every downstream install discovers is
broken.

### 2.4 The installer is not first-run clean

First `setup.py --i-am-agent` exited **94** ("complete, but a HARD-severity health probe
failed"); an immediately following identical run exited **0**. Nothing was done in
between. An agent following the docs — which state "a non-zero exit or a traceback means
the install did not complete, however many PASS lines preceded it" — would reasonably
conclude the install failed and start remediating. Worth either making the first run
converge, or teaching the exit-code contract about the probes that only settle on a
second pass.

### 2.5 The install-chain's own step ordering is stated two different ways

`README.md` Quick Start: install plugin → **restart** → `/coordinator:install` → install
engine → `/coordinator:setup`.
`commands/install.md` § Requirements: "(1) clone the engine repo; (2) run this
coordinator install; (3) **restart** Claude Code; (4) only then run the engine repo's own
installer."

The restart sits on opposite sides of `/coordinator:install`. Both files call their
ordering load-bearing. For a fresh agent these cannot both be followed.

### 2.6 `--check` returns green on a broken install

`python3 scripts/setup.py --check` printed exactly `check mode: setup.py is present and
executable` and exited 0 — before the engine was installed at all. coordinator-claude's
INSTALL.md is admirably blunt about this ("it returns green on a box whose engine
install crashed"), but klabauter's own INSTALL.md advertises it as a "deterministic
check-only" and the flag name promises far more than it delivers. `--preflight` is the
flag that actually probes.

**Partially patched:** `--help` now states the limitation inline, where someone choosing
a flag will actually read it.

### 2.7 An agent cannot complete the documented install by itself

Step 2 is "restart Claude Code", and steps 3, 5 and 6 are slash commands
(`/coordinator:install`, `/coordinator:setup`, `/coordinator:repo-setup`) that only exist
*after* that restart, because plugins load at boot. The README's own Quick Start asks the
**agent** to perform the install ("You don't install this — your agent does"), but a
running agent cannot restart its own session, so it cannot reach steps 3–6. This is
arguably the single largest friction in the whole exercise: the documented happy path is
addressed to an actor that structurally cannot walk it. Everything below step 2 had to be
driven by reading `install.md` and executing its fences by hand.

---

## 3. Linux capability gaps (features that are Windows/Darwin-only)

### 3.1 `process_time` has no Linux implementation

`coordinator_core/benchmarks/process_time.py` gates on `IS_WINDOWS` / `IS_DARWIN` and
raises `NotImplementedError` on everything else, so `claude-klabauter.invoke.latency` is
permanently `inconclusive` on Linux. Windows uses job objects
(`QueryInformationJobObject`); Darwin uses `posix_spawn` + kqueue `EVFILT_PROC` +
`os.wait4` rusage.

The module's own docstring already concedes that the **process-time half is POSIX and
verified against Linux's `kernel/exit.c :: wait_task_zombie()` rollup** — only the
**spawn-count half** (kqueue `NOTE_FORK`) is Darwin-specific. Linux has no kqueue, but it
does have `prctl(PR_SET_CHILD_SUBREAPER)` — which the module notes as precisely the
primitive *Darwin lacks* to close its own "orphan CPU is lost permanently" hole. So a
Linux implementation could be strictly better than the Darwin one on the accounting it
cares most about, using `/proc` PPid-chain walking for descendant enumeration.

**Deliberately not patched here.** The existing arms are ~200 lines each with careful
documented traps, the module is explicit that this is "PM-gated, not implemented in this
chunk", and `/proc`-polling gives a lower-bound `procs_per_call` on short-lived fan-out
rather than Darwin's event-driven exactness. That honesty boundary should be a deliberate
decision, not a drive-by. A worked implementation sketch exists in the investigation
notes if someone wants to pick it up.

### 3.2 No scheduled-task wiring on Linux

> `[ADVISORY] not running on Windows — skipping host-sampler task registration (Task Scheduler is Windows-only; cron wiring is not yet in scope).`

Host-resource sampling is registered only via Windows Task Scheduler. Linux (cron/systemd
timers) and macOS (launchd) both go unserved, so the sampler simply never runs there.

### 3.3 PowerShell dialect guard is permanently disarmed

`tree_sitter` / `tree_sitter_pwsh` are not among the declared dependencies, so the guard
reports `SILENT — missing-package` and PowerShell command classification stops. Advisory,
and arguably moot on a Linux box with no `pwsh` — but it prints a WARN and writes a
durable degrade record on every install, which is noise on a platform where the guarded
condition cannot arise. Worth gating the warning on `pwsh` actually being present.

### 3.4 `coordinator-invoke` not on PATH; warm-engine door not installed

> `[ADVISORY] door not installed at /root/.coordinator-claude-settings/bin -- install_bin_forwarders (this chunk's sole build site) did not land it`

`install_bin_forwarders` reports `380/380 forwarders verified` in the same run, yet the
door is absent and `coordinator-invoke` fails to resolve on a login shell. The
remediation ("re-run substrate --setup-only, then re-run setup.py") did not change it
across two full runs. Not root-caused in this pass.

---

## 4. Smaller observations

- **`shell_rc_guard._resolve_rc_path` defaulted to zsh when `$SHELL` is unset**
  **[patched]** — correct for macOS, wrong for Linux, and `$SHELL` is *most* likely to be
  unset in exactly the containers and non-login contexts where the box is bash. The
  sentinel block landed in `~/.zshrc` on a machine with no zsh. Now defaults per-platform
  (zsh on Darwin, bash elsewhere). The sibling resolver in
  `ops/install_shell_init_guard_seam.py` already defaulted to bash — the two disagreed.
- **The documented test command does not work on a documented install** — the PR
  template gates on `python .github/scripts/run-tests.py`, which invokes pytest with
  `-n` and therefore needs `pytest-xdist`. But the installer classifies the test tier as
  **advisory** and does not provision it, so a by-the-book install leaves the required
  command failing with an argparse error (`unrecognized arguments: -n`), not a missing-
  dependency message. `scripts/setup.py --with-test-deps` fixes it. Two traps around
  that flag: combining it with `--register-only` silently skips the install (register-only
  short-circuits first), and without `--skip-fleet-env` it drags the multi-GB fleet-env
  step along behind it.
- **A documented install leaves the working tree dirty** **[patched]** — `.coordinator-local/`
  (per-machine session machinery, written by `coordinator_core.session.machinery_paths`)
  and `*.egg-info/` (regenerated by the `pip install -e .` the installer performs) were
  ignored nowhere. `fleet_machinery_sweep` lands a `.coordinator-local/` ignore stanza in
  every sibling repo it sweeps, but this repo — the sweep's own origin — was the tree
  still missing it. The egg-info also fed `check-persona-names` the packaged author name
  out of `PKG-INFO`, failing the repo's own gate on an artifact no commit created.
- **`bin/tests/test_commit_path_budget_citations.py` breaks suite collection** —
  `FileNotFoundError: bin/commit-path-budget-citations.py`. The test outlived the script
  it tests, so `pytest bin/tests/` cannot collect at all without `--ignore`. Not
  Linux-specific; blocks the documented test command on any platform.
- **`coordinator_core/ops/tests/test_deliverable_cascade_kinds.py`** has two pre-existing
  failures from vendored-schema pin drift (`sizing-object.schema.json` is 1.20.0, pinned
  at 1.17.0). Confirmed present on a clean tree; unrelated to this branch.
- **`machine-local keys` emits a malformed-registry warning on every invocation** —
  one of the seeded concerns is registered in `concerns=[...]` but its TOML is absent, so
  its keys resolve not-found. A fresh install ships a registry that warns about itself on
  every read.
- **`_sentinel_write_guard.py` case-folds sentinel names unconditionally** — justified by
  APFS case-insensitivity, but on ext4 it over-blocks writes to genuinely distinct files.
  Over-blocking, never under-blocking, so not a safety issue; noted for completeness.
- **Documentation coverage is asymmetric.** Windows gets a shell table, an App-Execution-
  Alias workaround, and a Defender-exclusion section; macOS gets Homebrew guidance. Linux
  is addressed almost entirely by omission. Concretely: `docs/safety.md` names `brew` and
  `winget` as the consent-gated prerequisite installers and never names apt/dnf, so a
  Debian box hitting a missing prerequisite gets no remediation path at all.
  `bin/doctor-probes.toml:410` offers only `brew install bash` for a bash-too-old finding.

---

## 4b. The test suite does not pass on Linux, and that is the real headline

Measured, not estimated. `pytest coordinator_core/install/ bin/tests/` (excluding the
one module that breaks collection outright), run twice — once at this branch's parent
commit, once with the patches:

| | failed | passed |
|---|---|---|
| baseline (`HEAD~1`) | 102 | 2017 |
| with these patches | **101** | **2030** |

Diffing the two failure sets: **zero regressions** — no test fails with the patches that
passed without them. The single delta is `test_fnm_step_consent.py::test_fnm_step_sets_
homebrew_env_vars_on_consented_install`, and that is an artifact of *where the baseline
worktree lived* (`/tmp`), not of any code change: `_fnm_step` refuses to install from a
path under the system temp dir as a test-sandbox signature. So the honest reading is zero
regressions and zero incidental fixes from that comparison; the door fix's 2 recovered
tests are separate and were verified directly.

**The repo's own documented command is worse than that subset suggests.** `python
.github/scripts/run-tests.py` — the fast tier, the thing the PR template gates on — over
the whole `coordinator_core` testpath:

```
417 failed, 21948 passed, 345 skipped, 1620 warnings, 15 errors in 1258.46s
```

and it does not finish cleanly. The run terminates in an xdist `INTERNALERROR` after a
worker dies:

```
INTERNALERROR> AssertionError: ('coordinator_core/tests/test_settings_home.py::
INTERNALERROR>   test_normalize_native_path_converts_msys_mount_form', <WorkerController gw0>)
```

— a crash on an MSYS/Windows path-form test, which also means pytest never prints a short
summary, so the 417 cannot be attributed by module from that run at all. Anyone trying to
triage Linux failures has to work around a crashing test runner first.

**Scope note on the baseline comparison.** The zero-regressions result above is measured
over `coordinator_core/install/` + `bin/tests/` (~2100 tests) at both commits. The
full-suite figure here was measured **only on the patched tree** — no full-suite baseline
was run, so this log does not claim the 417 is unchanged from the parent commit, only
that the suite is far from green on Linux and that the subset which *was* compared shows
no regressions.

The ~100 subset failures are **pre-existing Linux breakage**, present identically at
the parent commit. Sampled attribution:

- 26 `test_claude_klabauter_revendor_schema.py` — needs a sibling clone this box has none of
- 19 `test_shell_init_guard.py` — confirmed identical at baseline, 19 failed / 8 passed both ways
- 11 `test_fleet_env_publish_reachability.py` — the fleet env that never provisioned (§2.3)
- 8 door tests keyed on `PATHEXT`/bare-name resolution — Windows-host tests (26 `PATHEXT`/`win32` references in one module alone)

None of this is caused by the changes here, and none of it was introduced by them. But it
does mean **the engine's own test suite has never been green on Linux**, which is a
stronger statement than any individual item in this log and probably the thing most worth
acting on: every patch above was verified against modules I could reason about
individually, because there is no green baseline to regress against.

---

## 5. What a fresh agent should be told

If the goal is that an amnesiac agent can install this on Linux unattended, the smallest
set of changes that would have made this run clean:

1. Reconcile the restart-ordering contradiction between `README.md` and
   `commands/install.md` (§2.5) — this is the first thing an agent hits.
2. State plainly that steps 3–6 require a human-driven restart, and give the agent an
   explicit non-slash-command path for each (§2.7).
3. Auto-seed `repos.doe_claude`, and fix the §3.5a citation it points at (§2.1).
4. Make `--skip-fleet-env` the documented default posture for containers, and fix the
   lock so the step stops discarding 7 GB (§2.3).
5. Make the first run converge, or document why exit 94 on a first run is expected
   (§2.4).
