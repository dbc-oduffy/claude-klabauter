# Contributing to claude-klabauter

Thanks for your interest. This project is small, opinionated, and genuinely happy to receive
patches.

## Read this first: how code gets into this repository

This repository is **published from an upstream working tree, not edited in place.** A
publish run syncs files one direction — upstream to here — and a file that differs is
overwritten.

The practical consequence: **a PR merged into this repository will be reverted by the next
publish unless the change is also integrated upstream.** That is a property of the pipeline,
not a comment on your patch. So the flow is:

1. You open a PR here.
2. The maintainer reviews it here, in the open, on your diff.
3. On acceptance, the change is integrated upstream and comes back out on the next publish.
4. Your PR is closed as merged-upstream, with the commit that carries it named in the thread.

Your work is not lost and your authorship is not erased — but the commit SHA that lands is
not the one you pushed. If that bothers you, say so in the PR and it can be handled
differently. Better to know before you spend an evening on it.

## Patches and hotwires — send them back, even rough

Install and setup lean on agents on purpose. A script-only install turned into whack-a-mole:
it worked on the author's machine and broke in small, machine-specific ways on everyone
else's. Handing the install to an agent that can read errors and adapt routes around that —
but it means *your* machine is where the remaining rough edges get found.

So: **if something does not work, patch it. Hotwire whatever you need to get running locally
— you have our blessing.** Then send the fix back. Three ways, in rough order of preference:

1. **Open a PR** with your patch.
2. **Open an issue** describing what broke and what you changed.
3. **Leave a note** — a paragraph pasted into an issue is plenty.

**Do not polish it, and do not worry about whether the code is "good."** We mean that
literally. The valuable part is the *what, how, and why*: what you were trying to fix, how
you worked around it, and why the original failed on your setup. A throwaway hack carries
all three — far more than a one-line bug report ever could — and a proper fix can be
generalised from it. A rough patch you actually sent beats a clean one you did not.

If an agent did your install, it is well-placed to write this up: it has the error in
context and made the fix. Ask it to draft the PR or issue for you.

## What we are looking for

- **Portability fixes** — especially Windows. A `bash`-spawning code path on the commit,
  session, or ceremony path is treated as a defect here, not a wart.
- **New operations** — coordination rules that currently live in prose and could compute
  their own answer instead.
- **Bug fixes** — in ops, guards, resolution, or the install chain.
- **Tests** — particularly for paths that are currently asserted only indirectly.
- **Documentation** — clarifications, examples, worked walkthroughs.

## Development setup

**This is a contributor/test-harness setup, not the product install** — if you are
installing this repo to *use* it rather than patch it, use [`INSTALL.md`](INSTALL.md)'s
`scripts/setup.py` instead; it runs the dependency check and registration step this
editable install deliberately skips.

**Prerequisite:** Python 3.11+. No Node runtime is required, for anything.

```
python3 -m pip install -e '.[test]'
```

The `-e` (editable) install is what makes local edits to `coordinator_core` take effect
without reinstalling, and `[test]` pulls in the test-only dependencies below — both are
things a contributor needs and an end-user install has no reason to carry.

Run an operation directly to check the install:

```
python3 -m coordinator_core.invoke <op> '<json>'
```

**Windows:** disable the App Execution Alias stubs first — Settings › Apps › App execution
aliases › turn off `python` / `python3`. Otherwise `python3` resolves to a Store shim that
does not run your interpreter.

## Tests

```
python3 -m pytest coordinator_core
```

The suite is split by pytest marker, and **the tiers are not equal**: `cadence`,
`pending_fix`, and `designed_red` each mean something specific about whether a failure is a
real signal. `designed_red` in particular is red on purpose and must never be treated as a
gate. Read [`docs/reference/test-tiers.md`](docs/reference/test-tiers.md) before you change
either tier or interpret a red run.

**On parallelism:** the suite runs under `pytest-xdist`, but **bare `-n auto` is unbounded
and has killed a machine.** Cap the worker count for your own box —
`min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB)` is the rule of thumb used here.

## Conventions

These are the ones a first PR is most likely to trip over.

- **New automation is naked Python (3.11+), not shell.** No new `.sh`, no shell wrapping
  Python, interpreter resolution via shebang. The remaining shell-outs are a closed,
  enumerated list — see
  [`docs/reference/shell-out-carve-outs.md`](docs/reference/shell-out-carve-outs.md). A site
  is sanctioned only if that document *names* it; satisfying a carve-out's rationale is not
  membership in it.
- **Windows is first-class**, not a port. Assume no `bash`, no POSIX-only binaries, and
  backslash separators.
- **No inline what-comments.** Do not explain what the code does or narrate the change.
  Purpose docstrings, spec backlinks, and negative-space blocks ("this deliberately does
  NOT…") are wanted — they are the difference between a reader trusting the code and
  re-deriving it.
- **Cite by enclosing function, not line number.** Hand-copied line numbers are stale on
  arrival.
- **No hardcoded absolute paths.** A path with a real username in it names one machine and
  is wrong on every other.

## Pull request policy

`main` is protected. All changes land via PR.

- **Maintainer approval required.** Every PR needs an approving review from @dbc-example-operator.
  Approvals are dismissed when new commits are pushed.
- **CI must pass.** Validation runs automatically on every PR
  (`python3 .github/scripts/run-all-checks.py` runs the same checks locally).
- **No force pushes, no branch deletion, conversations must be resolved.**

For substantial changes, open an issue first to discuss direction. Drive-by typo fixes and
obvious bugs do not need this; new operations, behavioural changes, or restructures do. It
saves both sides from a PR that gets closed for heading somewhere the project is not going.

## Licensing of contributions

Contributions are accepted under the same terms as the project: Apache 2.0 with the Commons
Clause rider (see `LICENSE`). By submitting a PR you confirm you have the right to license
your contribution on those terms. There is no CLA.

## Code of conduct

See [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Questions?

Open an issue with the `question` label.
