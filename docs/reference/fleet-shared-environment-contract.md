# Fleet shared environment contract

**Status:** landed. Every section below is a contract this plan's chunks (C0–C11) and every fleet
consumer read as a constant, not a proposal to re-derive.

**Audience:** a sibling-repo maintainer binding their repo to the fleet environment. This doc does
not assume familiarity with claude-klabauter's internals.

## Overview

One Python environment — win32, linux, and darwin, GPU-carrying — serves the whole fleet, replacing
each sibling repo building and maintaining its own venv. It is provisioned under the Klabauter root
and reached only through the resolvers this document names, never a hardcoded path.

For a maintainer wiring a repo up, the shape is:

1. **Find the environment's root.** `python3 coordinator/bin/fleet-env.py get` (§ Resolving the
   key). Do not hand-read the registry or guess a path.
2. **Run your code under that root's interpreter**, or, if you need your own package importable
   inside the shared environment rather than merely invoking it, **register a `.pth` binding**
   (§ The sibling `.pth` binding contract) so the binding survives an environment rebuild.
3. **If your repo currently hard-codes an interpreter path elsewhere** (an MCP server registration,
   a host-venv override chain, a sidecar launch), see § Migration paths for the three cases already
   worked out.

The sections below are, in order: the decisions that fixed the environment's shape and the
registry key that locates it (§ DECISIONS), how to resolve that key (§ Resolving the key), how the
environment itself is built and kept healthy (§ Provisioning), how a sibling repo registers itself
durably (§ The sibling `.pth` binding contract), and the concrete migration path for each of the
three repos with a pre-existing hardcoded interpreter pin (§ Migration paths).

## DECISIONS

### (a) One environment, carrying GPU torch — decided, not open

Not a live question. Recorded so a future reader does not re-derive the CPU/GPU split as an
oversight the plan forgot to consider.

**Why the CPU-only case doesn't reopen this.** The one candidate cited for a CPU-only split is
`example-cockpit-repo`'s embed-service. Its `Dockerfile` is `FROM python:3.13-slim` — a container image,
not a venv on a fleet machine — and its own `requirements.txt` states the boundary: *"We therefore
do NOT install from rag's pyproject. … What we DO take from rag: their library VERSION RANGES. …
Honour the ranges, ignore the index."* It builds its own image from its own manifest and never
consumes the fleet environment, so it cannot be evidence for splitting one. With the only CPU-only
case out of the venv plane entirely, the remaining arguments are one-sided: an unused torch costs
disk once and nothing at runtime, and a second environment reintroduces exactly the N-environments
maintenance tax this consolidation exists to delete.

**Sizing context for the disk cost (measured, universal `uv lock` run):** the universal resolution
succeeds at 254 packages across win32/linux/darwin, of which exactly two — `torch` and
`torchvision` — are platform-divergent (locked twice: PyPI for `macosx_14_0_arm64`, `+cu130` for
`manylinux_2_28_{x86_64,aarch64}` + `win_amd64`). A Windows-only run resolves 231/252 distributions
depending on universality flag; the ~21-distribution Linux-only delta is the CUDA split chain
(`nvidia-cublas`, `nvidia-cudnn-cu13`, `nvidia-cufft`, `nvidia-cusolver`, `nvidia-cusparse`,
`nvidia-nccl-cu13`, `nvidia-nvshmem-cu13`, `triton`, `cuda-toolkit`, `cuda-bindings`,
`cuda-pathfinder`, and peers) — absent on Windows because the cu130 wheel vendors CUDA inside
`torch/lib`. A disk cost, not a correctness one.

**The reopening trigger, stated so it is recognisable rather than rediscovered:** a consumer of the
**venv plane** — not a container, not a separate image — that has no GPU and never will. None
exists today. If one appears, a CPU variant earns its place then, and this section is where to
start reading. A containerised service wanting a smaller image is explicitly **NOT** that trigger —
it builds its own image, as cockpit's already does.

**Platform contract, decided:** the environment serves **win32, linux, and darwin** — all three are
live fleet targets (the settings home is Mac-synced; PowerShell routing carries an explicit macOS
branch). Because darwin is in, the cu130 index source **MUST** carry `marker = "sys_platform !=
'darwin'"` on `torch`/`torchvision` in `[tool.uv.sources]`, per uv's PyTorch integration guide.
This is a mandated requirement C3 inherits, not an executor choice.

**Evidence run (EM-executed to close eng-director finding 5):** `uv lock`, universal,
`requires-python >=3.12`, `environments` pinned to win32+linux+darwin, cu130 as
`[[tool.uv.index]] explicit = true` with the darwin-exclusion marker on `torch`/`torchvision`, over
the full 196-spec union — **RESOLVES at 254 packages**. `torch`/`torchvision` are the only
platform-divergent double-pins; a further ~17 `nvidia-*`/`triton` distributions are carried for
Linux only. The darwin marker is required and works; a lock without it either fails to resolve on
darwin or silently pulls the cu130 wheel where no darwin wheel exists.

### (b) The registry key — verified against live state at execution time

**Verification performed, not inherited from the plan's prose:**

1. Read `docs/plans/2026-08-16-one-engine-for-the-whole-box.md` at HEAD. Its Tasks block mints
   exactly two keys, both under `engine.*`: `engine.target` (box-wide, two values `main`/`candidate`,
   registry-resident, default per install class — claude-klabauter `candidate`, klabauter `main`) and
   `engine.working_repos` (retained as a pure locator; its exemption semantics are the subject of
   the item-2 exchange below, not a key-namespace question).
2. Read `cross-repo/inbox/2026-08-16-doe-claude-em-engine-targeting-contract-reply.md` and
   `2026-08-16-doe-claude-em-engine-targeting-item-2-we-were-wrong.md` for state only — both belong
   to the peer (one-engine) workstream and are not actioned, replied to, or edited here. Net state:
   DoE agreed item 1 (`engine.target`, no objection to primitive/name/location) outright, then
   reversed item 2 to accepted the same day (their PM ruled claude-klabauter also runs the published
   candidate). Both memos confirm the peer's minted namespace is `engine.*` and nothing else — no
   memo proposes or reserves any namespace this plan might want.
3. **Namespace disjointness confirmed.** `engine.*` is fully accounted for by `engine.target` and
   `engine.working_repos`. The fleet-environment key does not belong under `engine.*` — it answers
   "where is the shared Python environment," not "which published channel does this box run" or
   "does this repo participate in that channel." No collision exists; this section keeps it that
   way by minting elsewhere.

**Minted key: `fleet_env.root`.**

**Namespace: `fleet_env.*`** — a new tool-specific namespace, not `fleet.*`. `fleet.*` already
names a live namespace in this repo (`coordinator_core/ops/fleet/`, the op-registry surface behind
`cc_invoke.route("fleet.archive_paper_trail", …)`, `fleet.prune_closed_bugs`, `fleet.archive_queue_entry`).
That is a different registry system (op dispatch, not the machine-local settings registry) and
reusing its prefix would not be a technical collision, but it would be a naming collision a reader
trips over — grepping `fleet.` would return unrelated op names. `fleet_env.*` is unambiguous and
still names the tool (the fleet environment) precisely.

Minting `fleet_env.*` needs **zero coordinator-team sign-off** — DoE-claude
`coordinator/docs/wiki/machine-local-registry.md` §5a: *"Appending values under existing
namespaces, opening a new tool-specific namespace (`mything.*`), or hand-editing per-machine paths
in `registry.local.toml` does NOT require coordinator-team sign-off… A new namespace under
`registry.local.toml` needs zero registration."* No permission gate exists to seek.

**Residency: `registry.local.toml`** (per-machine, untracked) — **not** `registry.toml` (shared
baseline). Reasoning, from the wiki's own §2 discriminator table: a stable per-machine path (sibling
repo root, vendor SDK root) belongs in `registry.local.toml`; the fleet environment's on-disk
location is exactly that shape — the Klabauter root varies per machine (drive letter, Dev Drive
presence, or its absence per AC5), so the value itself is machine-specific, not a shared constant.

**Do NOT copy the peer's registry-residency constraint by analogy — verified, not assumed.** The
peer's own reply corrects this explicitly: *"you presented registry-residency as a constraint
neither plane can design around, because `coordinator_core/claude_klabauter_root.py` memoizes on
`_registry_mtime_pair`. That binds your plane only… There is no memo here for a non-resident fact to
fail to invalidate."* Reading `coordinator/bin/lib/machine_local_resolve.py` and
`coordinator/bin/lib/machine_local_impl_resolve.py` confirms neither module caches anything —
`machine_local_impl_path()` and `resolve_machine_local_bin()` both re-derive their answer on every
call, with no module-level memo. `fleet_env.root`'s own read path (C1's resolver, C5's fallback
ladder) is new code with no such cache either. Residency in `registry.local.toml` is chosen here
because it is the doctrinally correct location for this value's shape (per-machine, no live
source) — not because a memoization hazard forces it. If C1 or C5 introduces caching later, this
reasoning must be re-checked against that addition specifically.

**Amendment (C4): the key is installer-seeded, not purely operator-set.** The residency argument
above (per-machine, no live source) holds regardless of *who* writes the value — it was written
against an initial design where only an operator ever set this key by hand. C4
(`scripts/setup.py::_seed_fleet_env_root_from_klabauter`) changed that: on every install run, once
`register_claude_klabauter_root` has registered `repos.claude_klabauter` in the same pass, the installer
seeds `fleet_env.root = <klabauter-root>/.fleet-env` if the key currently reads absent. An operator
retains full control — a key that already carries a value (operator-set, or seeded by a prior run)
is never overwritten — but a fresh install with a discoverable klabauter checkout no longer requires
a hand-run `machine-local set` to land the environment at its contracted location. See § "Setting
the key on a machine," below, for the corrected operator-vs-installer split.

**The key returns a PATH.** Its value is the fleet environment's root directory. Two distinct
resolver contracts exist in this repo and C1 must pick deliberately:

- `cc_invoke.py::_machine_local_get(key)` — the **read-a-key** contract. Performs the registry read
  itself (`sys.executable` subprocess against `bin/_machine_local.py get <key>`) and returns the
  resolved string value, or `None` on a clean miss. An existing precedent already returns a
  filesystem path through this exact contract: `_machine_local_get("repos.claude_klabauter")`
  (`cc_invoke.py`, `_resolve_claude_klabauter_root`'s rung 2).
- `coordinator/bin/lib/machine_local_resolve.py::resolve_machine_local_bin(script_dir)` — a
  **different** contract: it resolves the `machine-local` CLI **executable's own location** (for a
  caller who needs an invokable binary path to `subprocess.run` directly), and does not read any
  registry key's contents at all. Its own module docstring states this in exactly those terms — "a
  different contract from `cc_invoke.py::_machine_local_get`, which performs the registry READ
  itself… and never hands out an invokable path."

**Verified finding, correcting this plan's own C1 body text:** C1's body states the key "returns a
PATH, which is the `machine_local_resolve` contract, not the read-a-key contract." On inspection
this is backwards. `machine_local_resolve.py` resolves the `machine-local` binary's path, which is
unrelated to what data type any given key's value holds — it has no read-a-key capability at all.
Because `fleet_env.root`'s value is itself a path, and an established precedent (`repos.claude_klabauter`)
already carries a path through the read-a-key contract, C1 should reuse
`cc_invoke.py`-style **read-a-key** reading (or the underlying `machine-local get fleet_env.root`
CLI call it wraps), not `machine_local_resolve.py`. This is not a blocking finding for C0 — C0 writes
no code — but it must not be inherited uncorrected into C1; C1's dispatch brief should be corrected
or C1's executor should independently verify before implementing.

### The environment's location — a contract clause, not an executor choice

**Basename: `.fleet-env`**, dot-prefixed, placed directly under the Klabauter root (resolved via
the registry — never a literal drive letter or Dev-Drive assumption anywhere in this plan's code).
C4 creates the environment at `<klabauter-root>/.fleet-env`; C11 adds the literal basename
`.fleet-env` to `dist/klabauter-toplevel/.gitignore`.

**Why dot-prefix + gitignore is sufficient (verified on disk, not assumed):**
`coordinator/bin/percolate-round.py::_reconcile_dest_discard` runs, in order:

```
git -C <dest> reset --hard HEAD
git -C <dest> clean -fd
```

No `-x` flag on the clean call. `git clean` without `-x` never removes files matched by
`.gitignore` (`-x` is what would extend cleaning to ignored files; `-fd` alone only removes
untracked-and-not-ignored files and directories). A gitignored, dot-prefixed environment therefore
survives `--reconcile-dest=discard` intact — this holds as of this read and satisfies AC13. If this
function's flags change before C4/C11 land, that is a BLOCKED-worthy finding for whichever chunk
discovers it, not something to re-verify silently.

### The day-one absent-key property (AC5b)

The key is absent on **every** fleet machine the instant it is minted — that is the normal state
for a rollout period, not an error state. Absent-or-unreadable resolution happens **at the read
site** (C1's resolver / C5's fallback ladder) and degrades to the documented fallback location,
**never** as a third stored registry value. There is no "not yet opted in" flag written anywhere;
absence itself is the only signal.

**A machine that never received the key is, by construction, indistinguishable from one that
deliberately opted out** — because "opted out" is not a state this design writes anywhere. This is
a deliberate, accepted consequence (mirrors the peer workstream's own `engine.target` disposition
and their explicitly-stated rationale: *"the store holds the value, the read site holds the
default, and 'unknown' is not encoded as a third stored value"*), not an oversight.

**How an operator tells the two apart:** by an inventory check outside the registry itself — e.g. a
doctor/probe pass that enumerates which fleet machines have `fleet_env.root` set versus which have
`repos.claude_klabauter` (or the equivalent Klabauter-presence marker) registered without it. The
registry alone cannot answer this; the distinguishing signal lives in whatever rollout tracking
(doctor probe, an operator checklist, or a future audit script) cross-references key-presence
against machine inventory. This document does not mandate which mechanism performs that
cross-reference — only that the registry's absent state is not itself that mechanism.

### The Python minor

**Pinned: 3.14** (`coordinator_core/install/fleet_env_lock.py::LOCK_PYTHON_MINOR`, five consumption
sites: this module's own `requires-python` emission and its `uv lock --python` argv,
`fleet_env.py`'s import of the constant, its `uv sync --python` argv, and its derivation of
`lib/python{minor}/site-packages` as a real filesystem path). PM ruling, 2026-08-17: flipped from
3.12 under a controlled install surface — *"this is really dumb"* on the prior pin. Measured: the
fleet's contracted health-probe set (`_FLEET_ENV_IMPORT_PROBES`) resolves to 90 packages at 3.12,
3.13, and 3.14 alike, and `torch` ships cp314 wheels (2.13.0), so nothing in the union blocks the
newer minor.

**Why the prior 3.12 pin is retired, not merely bumped.** It existed to keep the environment
*"buildable on any fleet machine whose system/uv-managed Python has not yet picked up 3.13"* — a
hedge against machines this design did not control. Under the controlled install surface (this
plan's C1–C11), the fleet machine's own interpreter is provisioned by the installer, not
discovered as-is, so that hedge no longer applies. Pinning the newest broadly-available minor
(3.14) costs nothing in resolution outcome and matches the machine interpreters this fleet
actually runs.

**Platform scope, decided alongside the flip (2026-08-17):** macOS arm64 is the supported mac
target; Intel macOS (x86_64) is out of support. The torch 2.13.0 cp314 wheel gap on macOS x86_64 is
therefore not a risk against any supported platform — dropped from scope entirely, not carried as a
known risk. Windows/Linux fallout from the flip is accepted risk, tracked separately (this plan's
Windows probe chunk); it does not gate this pin.

**Rollback, verified.** If the flip strands a platform, the pin reverts by: (1) set
`LOCK_PYTHON_MINOR = "3.12"` in `fleet_env_lock.py` (the sole edit — all five consumption sites
re-derive from it); (2) regenerate the lock at 3.12
(`python3 -m coordinator_core.install.fleet_env_lock --emit-lock`, requires a `python3.12`
resolvable by `uv`); (3) delete the stranded `.fleet-env` tree (or let
`coordinator_core.install.fleet_env.ensure_fleet_env()`'s health probe detect the now-mismatched
`lib/python3.14/site-packages` path and rebuild — the rename-swap rebuild never mutates the live
tree in place, so a failed rebuild attempt cannot corrupt a still-running environment). This restores
exactly the pre-flip state: the 3.12 lock content, the 3.12 `requires-python` floor, and the
`lib/python3.12/site-packages` derivation `fleet_env.py::_site_packages_dir` uses. Verified by
inspection of the rename-swap contract (`fleet_env._swap_in_new_env`) and by confirming
`LOCK_PYTHON_MINOR` is this module's only literal minor pin — no test currently asserts the constant's
value, so a rollback edit is not caught by CI; treat the regenerated lock's own health-probe pass as
the verification step.

## Resolving the key (C1)

The registry key is a value, not an invokable path — reach it through
`coordinator/bin/fleet-env.py`, never by hand-reading `registry.local.toml` or hardcoding a path.

```
python3 coordinator/bin/fleet-env.py get
```

Prints the resolved `fleet_env.root` path to stdout and exits 0. If the key is absent or
unreadable, prints a one-line remediation to stderr and exits 3 — it does not guess a fallback
location. Exit 2 means a usage error (bad/missing subcommand); exit 3 means the key was absent or
unreadable; only exit 0 carries a printed path. `fleet-env.py::resolve_fleet_env_root()` is the one sanctioned in-process read
site (plan AC1); no other module may read a hardcoded path to the environment. It delegates to
`coordinator/bin/lib/cc_invoke.py::_machine_local_get`, the read-a-key contract that performs the
registry read itself and returns the resolved value or `None` on a clean miss — not
`machine_local_resolve.py`, which resolves the `machine-local` CLI executable's own location and
reads no registry key at all (see that module's docstring).

**The day-one absent case.** Every fleet machine reads absent on `fleet_env.root` the instant the
key is minted — `fleet-env.py get` reports that identically to a machine that deliberately never
gets one; this is the normal rollout-window state, not an error. For the full property and how an
operator distinguishes "not rolled out yet" from "opted out," see § The day-one absent-key
property (AC5b), above.

**Setting the key on a machine — installer-seeded by default, operator-overridable.** As of C4,
`scripts/setup.py`'s install chain seeds this key itself
(`_seed_fleet_env_root_from_klabauter`, called from `install_fleet_shared_environment`,
immediately before `ensure_fleet_env()` runs and after `register_claude_klabauter_root` has registered
`repos.claude_klabauter` in the same pass): if `fleet_env.root` reads absent AND
`repos.claude_klabauter` is registered, the installer writes
`fleet_env.root = <klabauter-root>/.fleet-env` and never touches the key again on a machine where
it already carries a value. This is `fleet-env.py`'s own CLI reading a key it did not write — the
CLI itself remains read-only (`get` only, see above); the write comes from the install chain, not
from this script. An operator can still set (or override) the key directly, and always wins over
the seed — the installer only fills an absent key, it never re-asserts one that already has a
value:

```
machine-local set fleet_env.root <absolute-path-to-.fleet-env>
```

A machine with no discoverable klabauter checkout (`repos.claude_klabauter` unregistered) gets no
seed and falls through to `fleet-env.py`'s absent-key behaviour above, same as before C4.
`fleet-env.py` does not implement a fallback location for the absent case — that is
`coordinator_core/install/fleet_env_resolve.py`'s ladder (C5), a distinct module from this one and
from the C4 seed above: C5's ladder runs only when rung 1 (this key, seeded or not) is unusable,
never as a substitute for seeding it.

## Provisioning the environment (C4)

`coordinator_core/install/fleet_env.py::ensure_fleet_env()` creates/refreshes the environment at
the root C1 + C5 resolve to (`resolve_environment_root()`), installing exactly C3's committed lock
(`docs/install/fleet-env.lock`, via `uv sync --frozen --no-install-project` — never re-resolved).
It mirrors `coordinator_core/install/ensure_venv.py`'s proven shape (idempotent health-gated fast
path, a build-lock sidecar on the same shared `locked_write` primitive, and a rename-swap rebuild
that never mutates the live tree in place) rather than inventing a second one; see that module's
own docstring and `fleet_env.py`'s module docstring for the full rationale. The rename-swap
property matters more here than for the settings-home coordinator venv: this environment has the
whole fleet as readers, with no lock of their own, on a 50-70-session box.

**Health contract — the probe set, named so it is what consumers are promised, not an executor's
private choice:** `fleet_env.py::_FLEET_ENV_IMPORT_PROBES` = `yaml`, `pydantic`, `psutil`, `numpy`,
`torch`, `transformers`, `chromadb`, `huggingface_hub`. This is a deliberately small, representative
subset of the fleet union's direct requests, not its full ~250-package transitive closure — chosen
to cover the union's genuinely distinct consumption shapes (lightweight cross-repo utility via
`yaml`/`pydantic`/`psutil`/`numpy`; the GPU-heavy ML stack via `torch`/`transformers`; the vector
store via `chromadb`; the PM-ruled floor via `huggingface_hub`) without every provisioning run
paying to import all ~250 packages. An environment missing ANY of these imports is rebuilt, never
silently accepted (AC4). This set and `fleet_env.py`'s own constant must not drift apart
independently — this section documents the promise, the constant discharges it.

**Idempotency.** A second run against a healthy environment is a no-op: the health probe (above)
gates every mutation, and only an unhealthy or absent environment takes the build lock at all.
Concurrent callers on a 50-70-session box are serialised through the same fail-loud,
no-polling build-lock contract `ensure_venv` uses (`FleetEnvContention` on contention, not a hang).

**Binding-replay seam, for C6.** `fleet_env.BINDING_REPLAY_HOOK: Optional[Callable[[Path], None]]`
— `None` by default. C4's provisioning calls it (if set) unconditionally after every rebuild, via
`fleet_env._replay_sibling_bindings(env_root)`, never on the healthy fast path. C6 sets this hook to
a callable that replays every registered sibling `.pth` binding from its own declarative binding
registry — C4 does not know what a "binding" is and does not implement one; this is only the one
call site C6 wires into, so a rebuild reproduces every registered binding (AC4) instead of bindings
surviving by being copied across the swap.

**Interim binding instruction (usable now; superseded wholesale by C6).** Until C6's binding
registry lands, a sibling repo that needs to reach the fleet environment resolves its root exactly
as this document's own tooling does — call `coordinator_core.install.fleet_env.resolve_environment_root()`
(or, from a fresh process, run `python3 coordinator/bin/fleet-env.py get`, falling back through C5's
ladder if that prints nothing) — and then invoke that root's interpreter directly
(`<root>/Scripts/python.exe` on Windows, `<root>/bin/python` on POSIX) as the Python running its
own dependency-resolved code, e.g. `subprocess.run([str(env_python), "-m", "your_module"])`. There
is no `.pth` wiring in this interim path — a sibling repo's own code still imports from the fleet
environment only when it is the process actually running under that interpreter, not merely present
on `sys.path`. This paragraph is a stopgap that closes the two-chunk window before C6 lands; C6
replaces it wholesale with the full binding contract (a named registration call plus reproducible
`.pth` wiring), not an amendment to it.

## The sibling `.pth` binding contract (C6)

**Settled prior art, cited so it is never re-derived.**
`example-market-data-repo/scripts/setup.py::provision_venv` → `path_wire_example_retrieval_repo()` already writes a
repo-namespaced `.pth` basename (`example_market_data_repo_example_retrieval_repo_sibling.pth`) carrying an
ABSOLUTE path, located via `sysconfig.get_path('purelib')`. That module's own docstring warns: *"a
relative literal in a `.pth` resolves relative to site-packages, not this repo root, and silently
fails to import."* This chunk adopts that convention verbatim as the fleet-wide naming rule:
**`<repo>_<sibling>_sibling.pth`, absolute path only.** Under it, N repos writing into one shared
site-packages tree cannot collide on filename — filename collision was never this chunk's open
problem.

**The one documented call.** `coordinator_core.install.fleet_env.register_sibling_binding(repo,
sibling, sibling_root)` — `sibling_root` MUST be an absolute path (raises `FleetEnvError` otherwise,
rather than writing a binding that would silently misbehave, per the failure mode above). This:

1. Persists a `{repo, sibling, path}` entry into a versioned JSON binding registry
   (`fleet_env._binding_registry_path()` — `<settings-home>/machine-local/fleet-env-bindings.json`,
   machine-local like `registry.local.toml`, since the paths it names are absolute and specific to
   THIS machine, never portable to another). The registry is the reproducible source of truth — not
   the `.pth` file itself, which a rebuild can and does destroy.
2. If the fleet environment already exists, immediately writes the corresponding `.pth` file into
   its site-packages, so a caller does not have to wait for the next rebuild for the binding to take
   effect.

`deregister_sibling_binding(repo, sibling)` is the exact inverse (AC6b): removes the registry entry
and deletes the `.pth` file immediately if present.

**Rebuild durability (AC4's added clause).** A rebuild (C4's rename-swap onto a fresh tree) destroys
every `.pth` file written into the old tree — the health probe, which checks only C4's own
contractual imports, cannot see this. So the registry, not the tree, is what a rebuild reproduces
from: `fleet_env._replay_sibling_bindings` runs UNCONDITIONALLY after every rebuild (never on the
healthy fast path) and always consults the registry directly
(`_replay_registered_bindings`) — **this does not depend on any other module having imported this
one first, or having set a global.** `fleet_env.BINDING_REPLAY_HOOK` still exists as an additional
extension point (called after the registry replay, if set), but it is never the only path by which
registered bindings get replayed — the registry lives in `fleet_env.py` itself, so nothing external
needs to wire it in for replay to happen.

**Concurrency.** The registry file and the `.pth` writes it drives are protected by
`coordinator_core.locked_write.held_lock` — the same locking primitive this repo already uses
elsewhere (e.g. `ensure_venv`'s build lock), reused rather than a second file-locking mechanism
invented here. The registry mutation and the site-packages `.pth` write are two distinct,
never-nested lock acquisitions (the registry lock is released before any site-packages lock is
taken), respecting `held_lock`'s non-reentrancy contract.

**Removal and staleness (AC6b).** Deregistering a repo deletes its `.pth` immediately if the
environment exists; if the environment does not yet exist, the registry entry is simply gone by the
time the next (necessarily-a-rebuild, since absent) provisioning pass replays the registry, so no
stale `.pth` is ever written. A registered absolute path that no longer resolves (a moved or deleted
sibling) is a **stale binding**, detected during rebuild replay (printed as a `[fleet-env] WARNING`
to stderr, naming the repo/sibling/path) and independently via
`fleet_env.check_sibling_bindings(env_root)`, callable directly by an external doctor/probe pass at
any time — it flags every binding that is either `stale_path` (registered path no longer exists) or
`missing_pth` (path still exists but no `.pth` was ever replayed into the given environment, e.g.
registered while the environment was absent). Nothing is silently broken; everything flagged is
reported by name.

## Migration paths for the three other hard interpreter-path pins (AC11)

Naming the migration path is in scope here; performing it in any of these repos' own trees is not —
each repo's own maintainers make the change, driven by a cross-repo memo (dispatched by the EM, not
authored into their trees).

- **example-retrieval-repo's venv interpreter persisted into `~/.claude.json` as the MCP Python.** Migration:
  once the fleet environment is provisioned, example-retrieval-repo's install path points the MCP server
  registration at the fleet environment's interpreter (`<fleet-env-root>/Scripts/python.exe` on
  Windows, `<fleet-env-root>/bin/python` on POSIX, resolved via
  `coordinator_core.install.fleet_env.resolve_environment_root()` or
  `python3 coordinator/bin/fleet-env.py get`) instead of its own repo-local `.venv`'s interpreter,
  and calls `register_sibling_binding("example_retrieval_repo", "<own-package>", <own repo root>)` for any
  in-process import surface it still needs on `sys.path` (its editable installs — `example_retrieval_repo`,
  `example_retrieval_repo_ue_addon`, `coordinator_whoami` — become `.pth`-style bindings rather than `pip
  install -e` targets inside a repo-local venv).
- **example-retrieval-repo-ue-addon's three-tier host-venv resolution chain** (`--host-venv`,
  `EXAMPLE_RETRIEVAL_REPO_HOST_VENV_OVERRIDE`, `server.json::python_executable`, `.venv` derivation). Migration:
  the chain's terminal rung (today, deriving a `.venv` path) instead resolves the fleet environment
  via the same `resolve_environment_root()` / `fleet-env.py get` call; the three override tiers above
  it are unaffected (an explicit `--host-venv`/`EXAMPLE_RETRIEVAL_REPO_HOST_VENV_OVERRIDE`/`server.json` value
  still wins, exactly as today) — only what an unset chain derives to changes.
- **example-game-repo's sidecar launch hard-failing on an absent `.venv-sidecar`.** This is the one binding
  this design does NOT collapse: example-game-repo's own accepted DR (`DR-INSTALL-003`) documents a genuine,
  reproduced `huggingface_hub` version conflict between its main venv and the sidecar's model
  loaders, which the fleet's single resolved `huggingface_hub >=1.0` does not carry the `<1.0` side
  of (see `state/audits/2026-08-16-fleet-venv-survey.md` § blocker 2 and the PM ruling there — the
  fleet environment carries the modern hub; example-game-repo's own sidecar isolation problem is example-game-repo's to
  resolve, not a input this design accepts). Migration path named for completeness: example-game-repo's main
  `.venv` (non-sidecar) is a fleet-environment candidate under the same interpreter-resolution swap
  as example-retrieval-repo above; the sidecar's separate, incompatible-by-design environment stays a distinct
  installation outside this shared environment until example-game-repo's own future DR resolves the
  `huggingface_hub` split (survey: "a separate future DR (PM Option 1), NOT realized here").
