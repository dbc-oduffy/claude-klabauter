# Privacy Policy

**claude-klabauter** — a control-plane engine by [Dónal
O'Duffy](https://github.com/dbc-oduffy)

Last updated: 2026-08-02

## What this software does

`coordinator_core` computes and mutates coordination state — handoffs, plans, reviews,
session records, changelogs — as files inside your own repositories. It runs as a local
subprocess on your machine, invoked by you, by a git hook, or by an agent session you
started.

## Data collection

This software does **not** collect, transmit, or store any user data. It has no analytics,
no telemetry, no crash reporting, no update check, and no external reporting of any kind.
It opens no network connections of its own.

## Where your data goes

Everywhere the engine writes is a path under a repository or settings directory on your own
disk:

- **Work-state artifacts** — written into your project's own working tree.
- **Machine-local settings** — a settings directory in your home directory, holding the
  per-machine paths the engine resolves against. This never leaves the machine.
- **Nothing else.** There is no hosted component to send anything to.

The one place data leaves your machine is the one you drive: if you commit and push, the
artifacts go wherever you pushed them. That is `git`, under your control, not this engine.

## Third-party services

None. The engine introduces no third-party service relationship.

If you invoke it from a Claude Code session, that session's relationship with Anthropic is
one you already have and one this software neither extends nor alters — the engine is a
local subprocess and is not an API client.

## Source code

This repository is **source-available** (Apache 2.0 with a Commons Clause rider — see
`LICENSE`). Every claim above is auditable: read the code at
[github.com/dbc-oduffy/claude-klabauter](https://github.com/dbc-oduffy/claude-klabauter).

## Contact

Questions about this policy: open an issue on the [GitHub
repository](https://github.com/dbc-oduffy/claude-klabauter/issues).
