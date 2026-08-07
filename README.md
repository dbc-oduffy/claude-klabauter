# claude-klabauter

**claude-klabauter is the control-plane engine for a fleet of repositories whose engineering
work is executed by AI agents.** It exists to answer one question: can agentic software
engineering be run at scale — token-efficiently, to a consistent standard, and leaving a record
that a human can read and a machine can query — without the whole thing resting on a model
remembering its way there?

The lever is moving coordination out of prose and into computed artifacts. A rule written in
prose has to be noticed, retained, and applied; it costs tokens every session, degrades over long
ones, and leaves nothing queryable behind. The same rule written as an operation fires whether or
not anyone remembered it, costs nothing to re-read, and emits a fact. About half of this system's
governing prose has been deleted on that basis, and the results got better rather than worse.

<!-- mirror-parity:divergent-start -->
The name is the *Klabautermann*, the kobold of North Sea and Baltic sailors' folklore, who lives
below decks, patches the hull and keeps the ship sound without ever being seen doing it. That is
the intended relationship: good infrastructure is the kind nobody has to think about while it is
working.
<!-- mirror-parity:divergent-end -->

---

## claude-klabauter and coordinator-claude

Claude-klabauter is one of three parts of my setup, and it deliberately lacks opinions.

- **coordinator-claude** — the doctrine and the agent-facing surface: the rules,
  the skills and slash commands, the session ceremonies, and the meaning of every artifact shape.
  This is what an agent experiences. [Also OSS](https://github.com/dbc-oduffy/coordinator-claude).
- **claude-klabauter** (this repo) — the engine underneath it. It produces and mutates the
  authoritative on-disk work-state, triggers sessions, and computes the answers the doctrine would
  otherwise have to ask someone to remember. It owns mechanism, not meaning: it is not a successor
  to coordinator-claude and it does not own doctrine.
- **bonus retrieval layer** — the indexing and query capability: chunker, embedder, query surface. It 
  reads claude-klabauter's artifacts; claude-klabauter never writes into its store. Add one if you've
  got one, cos all this emitting of data would love a good home. Mine isn't OSS ready yet.

Claude-klabauter is a **hard prerequisite** of coordinator-claude — most mutating coordinator operations
fail without it present. The reverse edge is soft: the engine runs standalone.

The split is not tidiness. A harness config directory is a guarded surface — an agent
accumulating resident executing code inside its own instruction directory is exactly the shape
that trips vendor safety guards, and it did, repeatedly. Moving the executing half into a
separate, installable, versioned Python package stops that class of alarm at the source. It also
means an engine change ships without a doctrine change and vice versa, instead of requiring a
coordinated multi-repo commit for either.

---

## The problems it solves

**1. Prose doctrine is squishy, and it is not free.**
An instruction reaches an agent by one of four routes: harness mandate (the config files every
spawned agent reads on boot), invocation (skills, slash commands), injection (hooks), or the
prompt itself. Only the last is under a human's control at the moment it matters — the rest are
prose the model must notice and choose to apply, and every one of them is read on boot whether or
not it is used. A more robust governing architecture therefore becomes a more expensive one to
run, and the usual reflex — more agent types, more skills, one per situation — raises both the
token bill and the amount a human has to remember is available.

The governing test here is: *for every rule, what artifact discharges it?* "The operator
remembers" is not an answer, and neither is a checklist that emits a list of commands for someone
to run by hand — that has relocated the transcription, not discharged it. So rules become ops
that compute the result and apply it: coverage gates, review-trail scans, schema-drift gates,
plan task-spine mutation, changelog and release-tag ops. The standing design rule is *ergonomics
over enforcement* — make the correct path the cheap one rather than walling off the wrong one,
with hard blocks reserved for irreversible harm.

**2. Judgment that can't be automated can still be governed.**
Not everything reduces to a script; some calls need the model's reasoning. Those get deterministic
doors instead of deterministic answers. A fresh ask is *sized* before it is acted on, and the size
picks the room — dispatch it if it's cheap, shape it, plan it, or route it into a roadmap if it's
large. Plans pass review before execution, execution is delegated, and results are reviewed again
on the way out. The model still decides; it just cannot skip the stage, because the stage is a
gate in the artifact rather than a sentence in a document.

**3. Work should leave its own record, without anyone stopping to write one.**
Agentic work is fast and structurally messy — incoherent commits, no durable trace outside the
code, and native planning artifacts that are ephemeral by design. Claude-klabauter attaches the record to
the act instead of asking for it afterwards:

- every plain `git commit` gets a ~20ms injection of human-legible, machine-parseable context —
  which session, which plan, and *why* the work was done — so a meaningful unit of work is
  identifiable after the fact;
- invoking the plan skill mints a schema-conformant plan skeleton within ~5ms, before anyone is
  asked to produce one;
- plans, handoffs and roadmaps carry durable IDs that flow into one another, and blocking is
  computed rather than tracked — verified completion of plan X flips plan Y out of blocked by
  itself.

The same machinery is what makes dropped work visible. In-flight work is a durable, typed record
("handoffs", or batons) with real lineage: a baton knows its predecessor, its children, and its
terminal state. Ops walk that graph and answer questions no human is going to reliably ask — *did
this baton's parent ship without it? has this plan been reviewed but never picked up? is this memo
actionable, aging, and owned by nobody?* Roughly 14 `handoff.*` ops plus the reconciliation and
deferral detectors exist for exactly this. A session that hits its context limit mid-task loses
nothing important, not because the model retained it, but because the record was already on disk.

Because those records are structured rather than narrated, the byproduct is visibility: the same
artifacts that govern the work also answer questions about it — what is in flight, what shipped,
what stalled — without a status report being written.

**4. The same coordination logic was being reimplemented per repo, in shell.**
The starting inventory was 813 resident executing artifacts scattered across a harness config
directory (615 scripts, 116 hooks, 82 libs) — untested, unversioned, and impossible to change in
one place. Claude-klabauter consolidates that into one installable Python engine.

**5. Shell on the hot path is not portable.**
Agent tooling is optimised for POSIX and trained to reach for bash, which on Windows means a cold
`bash.exe` per invocation on the commit path — process storms severe enough to make scaled agentic
work unusable — or a silent no-op where no bash exists at all, which is a coordination system that
has quietly stopped coordinating. Windows is first-class here: new automation is naked Python
(3.11+), the degrading command shapes are blocked outright, and the block names the performant
Python alternative at the point of refusal rather than just saying no. The residual shell-outs are
a closed, enumerated list (third-party installers run as published, git-hook shims that git itself
execs, and probes that interrogate the shell about itself).

---

## What it actually is

A Python package, `coordinator_core`, exposing **~166 registered operations** over a JSON-RPC 2.0
op registry. Execution is **command-type and spawn-per-call** — there is no resident daemon
(one was built, measured, and retired; ~59ms cold start proved invisible at commit, ceremony, and
session cadence). Each op is held to a per-invocation end-to-end budget, benchmarked against a
real subprocess rather than in-process.

Op families, by weight:

| Family | What it covers |
|---|---|
| `handoff.*`, `baton.*` | In-flight work lifecycle, lineage walks, reconciliation on ship |
| `fleet.*`, `session.*` | Cross-repo state, session ledger, attribution |
| `ceremony.*`, `workday.*` | Session-ceremony machinery (start, complete, weekly close) |
| `memo.*` | Cross-repo memo channel — the fleet's inter-repo message bus |
| `plan.*`, `goal.*`, `roadmap.*` | Plan task-spines, goal records, initiative DAG assembly |
| `coverage.*`, `review*.*`, `ci.*` | Review-coverage gates, review trails, security/audit sweeps |
| `cartography.*` | Structural symbol/edge mapping, fallback when no semantic index exists |
| `percolate.*`, `release.*` | Publishing a working tree to a downstream repo; tags and releases |

Invocation is `python3 -m coordinator_core.invoke <op> '<json>'`. Consumers call it as a
subprocess; nothing needs to be running first.

---

## The path a piece of work takes

The core pathway mirrors how a product manager works with an engineering team, and every stage
below is a claude-klabauter artifact that coordinator-claude's doctrine reads and advances:

1. **An ask arrives** and is sized before anything else happens.
2. **Sizing picks the room** — dispatch it directly, converge on its shape first, turn it into a
   plan, or, if it is large enough, seed a roadmap of plans.
3. **A plan is reviewed** before it may be executed, and execution is a named gate rather than a
   continuation.
4. **Delegates execute** against the reviewed plan, in parallel where the scopes are disjoint.
5. **Results are reviewed again**, and the plan, its handoff lineage, and any roadmap it belongs
   to are reconciled from what actually landed.

---

## Install

**Prerequisite:** Python 3.11+. **The install command lives in
[`INSTALL.md`](INSTALL.md)** — read it rather than substituting a plain `pip install`,
which skips the dependency check and registration step the real installer performs and
will leave you with an engine that imports but is not actually set up.

`coordinator_core` is **not** stdlib-only — it declares real dependencies (`pydantic`, `psutil`,
`jsonschema`, `PyYAML`, `referencing`, `typing_extensions`) in `pyproject.toml`, which the
installer resolves. `INSTALL.md`'s own **Verify** section covers running the engine's test
suite once installed; the suite is marker-tiered and **the tiers are not equal** — see
[`docs/reference/test-tiers.md`](docs/reference/test-tiers.md) before treating a raw
pass/fail count as a verdict.

From a Claude Code session with coordinator-claude present, its setup flow walks the whole
install chain and delegates to `INSTALL.md`'s installer. This repo itself ships no plugin or
skills surface — discovery-resolved surfaces belong in coordinator-claude.

**A plain `pip install .` is not wrong, only narrower than it looks:** it does genuinely
work, as an ordinary library-dependency install for a downstream project that only needs
`coordinator_core` importable or its one console script
(`coordinator-cockpit-emit-schema`, see `pyproject.toml`). It is not, and was never meant
to be, how you set up the control-plane engine itself — that always goes through
`INSTALL.md`.

---

## Status

The engine transfer is landed and in production use across the originating fleet. The query and
read layer is fully native — no engine code path requires a Node runtime to do its own work.

Current focus:

1. **Engine of record** — every engine-tool change lands in one repo, with zero migratable
   executing shell left resident in consumers.
2. **Release to consumers** — a cut, consumable version and an install chain that lands for real
   users, not just the authoring machine.
3. **Windows-native and performant** — pinned test commands, a green baseline suite, and per-op
   invocation budgets measured and held on win32.
4. **Finish the bash kill** — no bash on any commit, session, or ceremony path.

---

## Licence

Apache-2.0 with a Commons Clause rider — **source-available, not OSI-approved.** The rider bars
resale of the software itself; it does not restrict internal use, modification, or redistribution
of derivative works. See [`LICENSE`](LICENSE) for the authoritative text; treat it as
authoritative over this summary if the two ever appear to disagree.

Using the engine *at work*, on commercial work, is covered by the standard grant and needs no
permission. If your organisation runs a blanket policy against non-OSI licences,
[`COMMERCIAL.md`](COMMERCIAL.md) describes a **free written internal-use grant, issued on
request** — a signed document your legal team can file, at no cost and with no sales
conversation attached.

---

## Reading further

- [`docs/reference/`](docs/reference/) — reference material: boundary/data-plane shape, shell-out
  carve-outs, test tiers, and related conventions.
- [`coordinator_core/DIRECTORY.md`](coordinator_core/DIRECTORY.md) — module-by-module map.
- [`AGENTS.md`](AGENTS.md) — entry point for an AI agent asked to install this.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how patches land, including how this repository is
  published and what that means for a merged PR.
- [`LICENSE`](LICENSE), [`NOTICE.md`](NOTICE.md), [`COMMERCIAL.md`](COMMERCIAL.md) — licensing.
- [`SECURITY.md`](SECURITY.md), [`PRIVACY.md`](PRIVACY.md),
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — policy.
