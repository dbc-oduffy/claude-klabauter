# claude-klabauter — Agent Entry Point

> **You are an AI agent reading this file.** Someone pointed you at this repository. This
> page tells you what it is and how to install it correctly. Read it before you improvise.

## What this is

`coordinator_core` — a control-plane engine that turns coordination rules into operations
that compute their answer. It is a Python package exposing a JSON-RPC 2.0 operation registry,
invoked as a subprocess. **There is no daemon and nothing to start.** Every call spawns,
answers, and exits.

It is also the engine that a coordinator-claude install depends on. If you were sent here by
that install chain, this is the prerequisite step, not the whole job — finish here, then
return to the chain.

## Release channels

We ship two branches, and we're forthright about both rather than mentioning only one:

- **`main`** — the release channel. **Install from `main`, unless whoever dispatched you
  explicitly named `candidate`.**
- **`candidate`** — public, and our nightly. We (the makers) run `candidate` ourselves, for
  maximum dogfooding before changes reach `main`. It's open for anyone to track, but most users
  should stay on `main`.

If you cloned this repository without specifying a branch, you are already on `main` — do
nothing further about channel selection.

## Install

**Prerequisite:** Python 3.11+. No Node runtime is required, for any part of this.

**Run the installer in [`INSTALL.md`](INSTALL.md)** — `python3 scripts/setup.py
--i-am-agent` (Windows: `python scripts\setup.py --i-am-agent`) — not a plain `pip
install`. A plain install skips the dependency check and the registration step the
installer performs; it will import cleanly and still not be an installed engine. Read
`INSTALL.md` in full before running anything.

This package declares real dependencies (`pydantic`, `psutil`, `jsonschema`, `PyYAML`,
`referencing`, `typing_extensions`) that the installer resolves; it is **not**
stdlib-only.

**On Windows, before anything else:** disable the App Execution Alias stubs — Settings ›
Apps › App execution aliases › turn off `python` / `python3`. Left on, `python3` resolves to
a Microsoft Store shim, and every diagnostic you run afterwards will mislead you.

## Verify

Do not report success without running these. Import and pytest passing is **necessary, not
sufficient** — both pass identically whether or not the install below actually wired the
control plane. Run the doctor probe first and read its exit code; it is the only one of
these checks keyed on observable control-plane behaviour rather than package plumbing.

```
python3 bin/claude-klabauter-doctor-probe.py --step-zero
python3 -c "import coordinator_core; print(coordinator_core.__name__)"
python3 -m pip install '.[test]'
python3 -m pytest coordinator_core
```

The doctor probe's own **exit code** is the pass/fail signal — exit 0 is a pass, any
non-zero exit means the install is not actually wired, whatever the installer itself
printed; read each non-"pass" line's own `remediation` field rather than guessing at a fix.
Check it however your shell reports the last exit code (POSIX: `echo $?`; PowerShell/cmd:
`echo %errorlevel%` / `$LASTEXITCODE`) — this is the same check `scripts/setup.py` runs
post-install, except here you read its exit code directly instead of trusting the
installer's own summary line.

Read [`docs/reference/test-tiers.md`](docs/reference/test-tiers.md) before you interpret the
pytest result. The tiers are not equal: tests marked `designed_red` are red **by design** —
their output is a worklist, never a gate — and `pending_fix` marks known-broken path
assumptions. A raw pass/fail count is not a verdict here.

**Do not run the suite with a bare `-n auto`.** It is unbounded and has taken a machine down.
Cap workers to roughly `min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB)`.

## Use

```
python3 -m coordinator_core.invoke <op> '<json>'
```

Operations are grouped into families — work-state and handoff lifecycle, session and fleet
records, ceremony machinery, plans and goals, coverage and review gates, publishing and
releases. `README.md` has the family table; `coordinator_core/DIRECTORY.md` is the
module-by-module map. Read one of those rather than guessing an operation name.

## What you must NOT do

- **Do not run a plain `pip install .` as the install step.** It imports cleanly and
  proves nothing — it skips the dependency check and the registration step
  `scripts/setup.py` performs. Reporting success after one is a false pass.
- **Do not start a server or look for one.** There is no resident process. If you find
  yourself debugging a connection, you have misread the architecture.
- **Do not skip the verification pass.** An install that imports but has unresolved
  dependencies fails later, inside a commit hook, where the error is far harder to read.
- **Do not add a `bash` wrapper** around any of this to make it fit a workflow. Windows is
  first-class here; a shell-spawning wrapper on a hot path is a defect, and
  [`docs/reference/shell-out-carve-outs.md`](docs/reference/shell-out-carve-outs.md)
  enumerates the closed list of places shell-out is sanctioned at all.
- **Do not treat a red `designed_red` test as a failed install.** See above.

## Licence

Apache 2.0 with a Commons Clause rider — **source-available, not OSI-approved.** If the
person you are working for needs a licence answer, read `LICENSE` and `COMMERCIAL.md` and
quote them; do not paraphrase the terms from memory.

## Why this file exists

`AGENTS.md` is a cross-tool convention filename that agents look for unprompted on landing in
a repository. It means you can find the right entry point even when nobody pasted you a
one-liner.

Follow the steps above. Then report back to the person who sent you.
