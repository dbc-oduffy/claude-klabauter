# Install-chain walk: root to unknown leaf

> This document describes mechanisms that **already exist** and are live in the shipped
> chain-walker and install-spinoff machinery. It is not a design proposal, not a roadmap, and not
> a "we should build this" pitch. If you finish reading and come away thinking the baton
> rendezvous described below still needs building, this document has failed at its one job.

The install chain answers two different questions, and conflating them is the single most common
mistake a reader of this contract makes:

1. **"What does repo X depend on, and how do I install those deps?"** — answered by the
   **recursive walker** (§ The recursive walker, upstream direction).
2. **"What does the walker do when it reaches a repo whose downstream branches it cannot name —
   an unknown branch, an unknown leaf?"** — answered by the **install-spinoff baton rendezvous**
   (§ The install-spinoff baton rendezvous, downstream direction). This already exists.

These are two distinct layers, moving in opposite directions through the same dependency graph.
The recursive walker only ever looks *upstream* (a repo asking "what do I need before I can run?").
The baton rendezvous only ever looks *downstream* (an already-installed root discovering what
else the operator wants installed, without any of those repos being named in its own source).
Treat them as separate mechanisms throughout — a repo can participate in one, the other, or both.

## 1. The recursive walker (upstream direction)

The recursive walker is how a single repo locates and installs its own direct dependencies. It is
data-driven, not name-driven: nothing in the walker's own code names a particular upstream. Each
repo declares its own `direct_deps` in `docs/install/agent-install-manifest.json`
(`agent-install-contract.md § Schema reference`), and the walker reads that manifest **at walk
time** — never from a cached or centrally-maintained registry.

For each `direct_deps` entry, the walker dispatches one subagent per upstream. That subagent
constructs and runs the upstream's standalone setup script with this exact composition
(`agent-install-contract.md § Walker composition (v2)`):

```
<upstream standalone_setup_script> \
  <consumer_install_args...> \      # from the CONSUMER's DirectDep entry
  --i-am-agent \
  <upstream override_flags.skip_dep_check> \
  <upstream override_flags.accept_hallucination_risk>
```

`consumer_install_args` (mode/version selection) come from the *consumer's* own manifest entry
for that upstream; `override_flags.*` are read from the *upstream's own* manifest at dispatch
time, never literal-pinned by the consumer — this is the authority-boundary split the contract
enforces so a flag rename on one side never drifts against a hardcoded copy on the other
(`agent-install-contract.md § Authority boundary`).

**The trap this document exists partly to name plainly:** `setup_skill` in a repo's manifest is
the slash command a **human** types (e.g. `/coordinator:setup`) — informational metadata, nothing
more. The chain-walker **never reads `setup_skill`**. Dispatched subagents do not expand slash
commands in their prompts; the actual dispatch primitive the walker invokes is
`standalone_setup_script` (`agent-install-contract.md § Skill chain-walker`). A reader who assumes
the walker "runs the setup skill" has the wrong mental model of the dispatch mechanism.

**Cycle safety.** Because each recursive subagent dispatch runs as an independent process with no
access to its parent's in-memory state, cycle/diamond-DAG detection is a disk-resident visited-set
file, not anything held in memory:

```
<settings-home>/<repo-id>/chain-walk-<session-id>.json
```

A dep already present in the file's `visited` array is skipped ("already walking `<id>`,
skipping"); otherwise it is appended before the subagent is dispatched
(`agent-install-contract.md § Visited-set protocol`). This is a property of *this repo's own
walk*, scoped to one session — it has nothing to do with the baton rendezvous below.

In `claude-klabauter`'s own concrete implementation, this upstream side is
`coordinator_core.ops.setup_chain_walker` — a native Python port of the DAG-root's chain-walker
shell script. Its `dep_probe_all` function probes every `direct_deps` entry (sibling-dir presence
plus the declared `functional_probe`); `consent_gate` is the last barrier before an install
proceeds with a hard dep missing; `run_prereq_gate` unifies the machine-environment prerequisite
probe set with the manifest-dep probe set behind one PASS/FAIL/WARN table; and `main` is the CLI
entrypoint that wires argument parsing, the agent-direct short-circuit, phase dispatch, and the
consent gate together. `coordinator-claude` itself is the terminal node of the walker's upstream
direction — "chain step 5 of 5" in that module's own banner means *root of the OSS
plugin-adoption chain*, not "step five of five installs remaining."

## 2. The install-spinoff baton rendezvous (downstream direction)

The recursive walker above only ever looks upstream. It has no mechanism for a root repo to
discover an arbitrary downstream branch or leaf it was never told about by name — and that is
deliberate: **coordinator hardcodes no downstream leg by name.** That knowledge "lives in each
downstream repo and arrives only as the spinoff that repo seeds"
(`agent-install-contract.md § Install-spinoff layer`).

This is the answer to "what happens when the chain reaches an unknown branch, an unknown leaf?"
— and the mechanism already exists, is already live, and requires no new baton type, no new
folder, and no new frontmatter `kind`.

**The mechanism.** A downstream repo's own installer drops a `kind: spinoff` baton — carrying an
`install_chain_order:` field — into the standard handoff rendezvous folder:

```
$(coordinator-settings-home)/state/handoffs/
```

This is the *same* folder `/spinoff` and `coordinator:roadmap-planning` already write to; nothing
new was invented for install legs (`agent-install-contract.md § The rendezvous`). Seeding is a
cheap file drop (`cp`/`sed`, never the Write tool — a bare Write into `state/handoffs/` without an
active authoring skill trips the unauthorized-handoff nudge), and it is idempotent
(overwrite-on-reseed). It can run for every leg the operator queued *before* the coordinator
reboot, so the durable coordinator session sees the whole requested chain at once.

Post-reboot, coordinator's Step 0 greps that same folder for `install_chain_order:` legs, builds
a lightweight install-chain spine from whatever it finds, and drives each leg to conclusion via
`/pickup` — agnostic to which repos are present, asserting no fixed set
(`agent-install-contract.md § Install-spinoff layer → The two roles`).

A **second, additive discovery sweep** finds orient legs — post-install orientation batons — by
**shape alone**, never by name:

> `kind: spinoff` **AND** no `install_chain_order:` **AND** (filename matches `orient-*.md` **OR**
> the `summary:`/`title` frontmatter matches a word-boundary "orientation").

(`agent-install-contract.md § Orient-leg discovery`.) The `kind: spinoff` gate plus the
word-boundary match (not a bare substring) keep an unrelated handoff that merely *mentions*
"orientation" from being falsely swept into the install walk.

**Both sweeps are repo-name-agnostic by design.** Quoting the contract directly: "Coordinator
hardcodes no downstream leg by name … that knowledge lives in each downstream repo and arrives
only as the spinoff that repo seeds." A repo joining the chain does not require any change to
coordinator's own source — it only requires seeding its own baton, correctly shaped, into the
rendezvous folder before the coordinator reboot.

**Two seeding models, both valid.** A generic downstream leaf **self-seeds**: its own installer
writes its own baton. `claude-klabauter` is the one repo that instead uses **coordinator-seed** (its
baton is written by coordinator's own onboarding flow, from a shipped template) — a continuity
choice for this specific repo, not a structural requirement any other repo must follow. Self-seed
is the documented default for a new node joining the chain.

## 3. Walking the canonical chain, concretely

The full worked chain, root to an unnamed leaf:

```
Coordinator-claude  (root, terminal — "chain step 5 of 5" means root of the OSS
                      plugin-adoption chain, NOT "step five of five")
       |
       v  hard dep
   claude-klabauter    (the engine)
       |
       v
   example-retrieval-repo
       |
       v
   a private downstream leaf   (the chain's public members cannot name this repo)
```

`coordinator-claude` is the walker's terminal upstream node — it has exactly one hard direct dep
of its own (`claude-klabauter`, the engine) and, once that dep resolves, the recursive walker (§1)
has nowhere further upstream to go.

The centrepiece worked example is the fourth node: **a private downstream leaf that neither
`coordinator-claude` nor the public klabauter mirror knows about, or could name in their own
source.** This is exactly the case the baton rendezvous (§2) exists to solve. That leaf's own
installer seeds a `kind: spinoff` baton carrying `install_chain_order:` into
`$(coordinator-settings-home)/state/handoffs/` — nothing upstream needs to be told its name in
advance, because Step 0's discovery sweep finds it by shape, not by identity. The leaf joins the
chain the same way any repo does: by seeding its own baton, not by any upstream repo adding a
line of code that names it.

## 4. Severity semantics

Two severity taxonomies coexist and are **orthogonal** — do not conflate them.

**Manifest-dep severity** (`DirectDep.severity`, `hard | soft | optional`):

- `hard` — a missing hard dep drives the consent gate (`consent_gate` in
  `setup_chain_walker.py`). In a non-interactive/non-TTY context with no override flag pair, this
  exits **90**. In an interactive TTY context, the operator must double-confirm; declining either
  confirmation exits **91**.
- `soft` — warn loudly, offer to walk-and-install the dep, proceed if declined. This is a
  warn-and-continue path — a missing soft dep does not by itself produce a non-zero exit.
- `optional` — offer once, proceed silently if declined. No warning on decline.

**ENV-PREREQ-PROBE severity** (machine-level prerequisites — `hard | semi-hard | advisory`) is a
**separate, orthogonal contract** for a different surface (git/python/uv/gh/node/pwsh/ue/etc.),
with its own post-consumer demotion: on the post-consumer chain-walk path,
`run_prereq_gate("post-consumer", …)` demotes `gh`/`node`/`git`/`clone_auth` to `advisory`
(`_DEMOTE_TO_ADVISORY_POST_CONSUMER` in `setup_chain_walker.py`) while `python` stays the sole
hard gate. The `--preflight` path runs `run_prereq_gate("strict", …)` with no demotion. A dep
declared `hard` under the manifest-dep taxonomy does **not** imply its ENV-PREREQ probe (if any)
carries `hard` severity — these are different fields on different contracts
(`agent-install-contract.md § Severity semantics`).

**The override flag pair.** `--skip-dep-check` and `--accept-missing-deps-risk` (or the
upstream's own equivalently-spelled pair) must be supplied **together**. `consent_gate` in
`setup_chain_walker.py` checks both directions explicitly: `skip_dep_check` without
`accept_missing_deps_risk`, or vice versa, exits **93** (override-flag-pair-incomplete) before any
dep probing even runs. Passing only one flag is always a walker-invocation bug, never a valid
partial-acceptance state.

## Attestation — the walk, actually run

Run date: 2026-08-03. Git SHA: `b73dd2fc16509f99c1bb736ffebd89d029db4c2e`
(this checkout's `git rev-parse HEAD` — the citable anchor for this attestation).

This section records four separate legs, each actually executed against live substrate — not
read, not inferred from source. Raw run logs for legs (a) and (b) live outside this document
(the source repo's own state directory, gitignored by design and not part of the published
mirror); this section is the citable summary. Leg (c) is captured as a repeatable automated test
alongside this document's own source tree; leg (d) is the one-time drift check this plan's
anti-scope requires. **Failures below are recorded as findings, not retro-edited away** — this
plan's anti-scope names "documented, therefore proven" as the one outcome this section must not
produce.

**(a) `python3 scripts/setup.py --check` — deterministic exit contract.** PASS. Clean-state run
exits 0. One finding, not a blocker: `--check` returns before reaching the hard-dependency check
at all (`main()`'s `if args.check:` branch returns immediately), so its exit code is unaffected by
whether the upstream engine dependency is resolvable — confirmed live by pointing the resolution
env override at an empty directory and observing the identical exit 0. Read against
`agent-install-contract.md § entry_point_contract`'s flag-table description of `--check` as a
"read-only dep probe," the current behaviour is closer to a pure liveness check (the script is
present and runs) than a dependency probe of any kind. This is a documentation/behaviour gap
worth closing, not confirmed break-class, and is not fixed by this attestation.

**(b) The consent gate — hard-dependency enforcement.** PASS, all four scenarios. With the
upstream engine dependency made unresolvable via an environment override (no clone was moved,
renamed, or deleted to construct this state): a full run exits **90** (hard-dependency missing).
Passing only one of the two required override flags exits **93** (override-flag-pair-incomplete)
before any dependency probing runs. Passing both flags together, `--skip-dep-check
--accept-missing-deps-risk`, restores exit **0** via the documented degrade-gracefully path. A
fourth scenario — the dependency resolvable *only* through the environment-variable override
rung of the location ladder, with no directory-adjacent clone available to fall back to — also
exits **0** from both call sites that consume that resolution (the dependency-check gate and the
registration path), confirming the location ladder is honoured consistently rather than by one
call site only.

**(c) The leaf proof — install-spinoff baton, seed to sweep to pickup.** PASS, as a **driven
synthetic leaf**, not a real private downstream repo's own leg. A throwaway repository was
constructed in a temporary directory with a short (~20-line) `standalone_setup_script` that seeds
its **own** `kind: spinoff` baton (carrying `install_chain_order:`) into the rendezvous location —
invoked as a real subprocess, the same way a genuine downstream installer would run itself, not
hand-placed by a test harness. The real Step-0 sweep block (extracted verbatim from the shipping
onboarding template, not reimplemented) was then run against the same sandbox and correctly
discovered the seeded baton, carrying the seeded `install_chain_order` value through unchanged.
This proves seed, sweep, and pickup end to end, against the actual production sweep logic. It is
landed as a repeatable automated test alongside this reference document's own source, not only as
this prose block — re-run at need rather than trusted from a one-time narration.

**State this plainly, because blurring it is the specific failure mode this leg guards against:**
this run proves the *mechanism* — that an unnamed downstream repo dropping its own baton is
discovered and driven by the existing sweep with no code change anywhere in the chain. It is
**not** evidence that any particular real private downstream repo's own installer currently
conforms to this contract; that is a separate, per-repo claim this attestation does not make.

**(d) Entry-doc drift check.** Two INSTALL.md/README.md pairs were compared for disagreement on
prereqs, the install command, and the test-count figure:

- `INSTALL.md` vs `README.md` (this repo's own root pair) — **no disagreement found.** The install
  command matches (`python3 scripts/setup.py`, human path); the hard bidirectional dependency on
  the upstream engine's own doctrine repo is stated in both, from each file's own vantage point;
  neither cites a raw test-count figure.
- The published OSS mirror's `INSTALL.md`/`README.md` pair — **disagreement found, on the install
  command.** `INSTALL.md` documents the standalone installer script's own agent/human/check-only
  contract (`--i-am-agent`, plain, `--check`), matching the mechanism the rest of this chain
  actually dispatches (§ The recursive walker). The paired `README.md` instead documents
  `python3 -m pip install .` as the install step, with no mention of the installer script or the
  upstream dependency's hard-fail behaviour. `INSTALL.md` is this plan's own deliverable and was
  authored directly against the dispatch contract this document describes, so it is treated as the
  correct side; the `README.md` side is stale and belongs to separate, pre-existing authorship
  outside this plan's write scope. **Not edited here** — surfaced as a finding for that content's
  own owner to reconcile. The test-count figure is not cited by either file in this pair (the
  mirror's `INSTALL.md` deliberately omits the raw count and defers to the paired `README.md` and
  the test-tiers reference instead), so no test-count disagreement exists to report.

### Verdict

| Leg | Result |
|---|---|
| (a) deterministic exit contract | PASS (one non-blocking documentation-gap finding) |
| (b) consent gate, all four scenarios | PASS |
| (c) leaf proof | PASS — driven synthetic leaf; not evidence any specific real private leg conforms |
| (d) entry-doc drift check | one disagreement found (mirror `README.md` install command), fixed on neither side by this attestation, reported to that content's owner |

No exit-code mismatch was found anywhere in this walk. The chain mechanisms this document
describes are confirmed live, by execution, on the SHA recorded above — not merely documented.
