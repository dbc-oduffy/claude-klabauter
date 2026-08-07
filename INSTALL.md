# Install

> This document describes an install path that already works today. It is not a design
> proposal.

## What claude-klabauter is

Claude-klabauter is the **control-plane engine** for a fleet of repositories whose engineering
work is executed by AI agents — the `coordinator_core` Python package: a JSON-RPC 2.0 operation
registry, command-type and spawn-per-call, with no resident daemon. It is **not a plugin and not
usable standalone.** It produces and mutates the authoritative on-disk work-state and computes
the answers doctrine would otherwise have to ask someone to remember, but it owns mechanism, not
meaning.

**coordinator-claude is a hard prerequisite, stated plainly: most mutating coordinator
operations fail without this engine present, and this engine has nothing to control without
coordinator-claude's doctrine, skills, and artifact-shape contract.** coordinator-claude is
public at <https://github.com/dbc-oduffy/coordinator-claude>. Installing that repo and
installing this one is one joint installation, not two independent ones — from a Claude Code
session with coordinator-claude present, its setup flow walks the whole install chain and
delegates here for this leg.

## Are you an agent or a human?

**If you are an AI agent** dispatched to install this repository: run the non-interactive path
below (`--i-am-agent`), verify with the commands under **Verify**, and report back to whoever
sent you. Do not improvise beyond what this file and `AGENTS.md` state.

**If you are a human**, the interactive path (no `--i-am-agent`) will prompt you where a
decision is needed. Either path installs the same engine.

## Install

**Prerequisite:** Python 3.11+.

```
python3 scripts/setup.py --i-am-agent      # agent path, non-interactive
python3 scripts/setup.py                   # human path, interactive prompts where needed
python3 scripts/setup.py --check           # deterministic check-only, no side effects
```

Windows: `python scripts\setup.py` with the same flags.

**On Windows, before anything else:** disable the App Execution Alias stubs — Settings › Apps ›
App execution aliases › turn off `python` / `python3`. Left on, `python3` resolves to a
Microsoft Store shim and every diagnostic afterwards will mislead you.

Without coordinator-claude present, the installer's dependency check fails loud rather than
warning and continuing — this engine has nothing to control on its own. The documented
degraded path is the explicit override pair `--skip-dep-check --accept-missing-deps-risk`
(both together; passing only one is an error).

**Cloning coordinator-claude is not the same as installing it, and the order matters.** Its
repository ships `bin/machine-local` as a forwarder; the real resolver is only deposited once
coordinator-claude's own `/coordinator:setup` has run, which includes a restart. Until then the
forwarder exits 127, reporting `resolver not installed` and directing you to
`run /coordinator:setup (Phase 3)`.

So on a fresh machine, complete coordinator-claude's install first — clone, run
`/coordinator:setup`, restart, and confirm `machine-local` resolves — and only then run this
repo's installer. Starting here instead produces that 127 and a remediation instruction that
cannot succeed until the prerequisite side is finished. This is deliberate on their side: a
resolver that fails loudly is preferable to one that returns nothing and lets every consumer
silently fall through to a last-resort guess.

Windows note, accurate as of 2026-08-05 and expected to lapse: coordinator-claude's *published*
snapshot predates their Windows de-bash work, so the forwarder a fresh clone gets today is a bash
script with no `.cmd` counterpart and will not resolve at all on a bash-less host rather than
failing with the message above. Their current source resolves this — the forwarder is Python and
probes the `.cmd` sibling first on Windows — and a completed `/coordinator:setup` installs a
working Windows path regardless. If you are testing on Windows before their next release, expect
the prerequisite leg rather than this installer to be where you get stuck, and report it as such.

## Verify

"Installed" and "verified working" are not the same claim. Run:

```
python3 -c "import coordinator_core; print(coordinator_core.__name__)"
python3 -m pip install '.[test]'
python3 -m pytest coordinator_core
```

The suite is marker-tiered and the tiers are not equal — see
[`README.md`](README.md#install) and
[`docs/reference/test-tiers.md`](docs/reference/test-tiers.md) for the surviving-test-count
figure and how to read the result; a raw pass/fail count on its own is not a verdict.

## Licence

**Source-available, not open source.** Apache-2.0 with a Commons Clause rider — the rider bars
resale of the software itself, not internal use, modification, or redistribution of derivative
works. See [`LICENSE`](LICENSE) and [`COMMERCIAL.md`](COMMERCIAL.md); the licence file is
authoritative over this summary.

## Where this fits in the chain

This repo is one leg of a larger install chain — coordinator-claude at the root, this engine as
its hard dependency, and further downstream repos joining beyond it, some of them unknown to
either. See [`docs/reference/install-chain-walk.md`](docs/reference/install-chain-walk.md) for
the full walk and the mechanisms that make an unnamed downstream leg installable at all.
