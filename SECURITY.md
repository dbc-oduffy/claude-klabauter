# Security Policy

## What this repository contains

`coordinator_core` is executable Python that runs on a developer machine or in CI. It is
not a network service: there is no resident daemon, no listening socket, and no server to
attack. Every operation is a spawn-per-call subprocess invoked locally.

That said, it is not inert configuration either. It reads and writes files, shells out to
`git`, resolves paths from environment and machine-local settings, and is invoked from
commit hooks and agent sessions. Treat it as ordinary local-execution software.

## Supported versions

The most recent release is supported. There is no long-term-support branch.

## Reporting a vulnerability

Report privately first, please — do not open a public issue for an exploitable finding.

Use GitHub's [private vulnerability
reporting](https://github.com/dbc-oduffy/claude-klabauter/security/advisories/new) on this
repository. If that is unavailable to you, open an issue containing only a request for a
private channel, with no technical detail.

Useful things to include: the version or commit, the operation invoked, what an attacker
controls, and the impact. A proof of concept helps but is not required.

Expect an acknowledgement within a few days. This is a small project maintained by one
person, so please do not expect enterprise response times; a fix will be prioritised over
process.

## In scope

- Path traversal or arbitrary file write through op parameters.
- Command injection through any `git` or subprocess call site.
- Secret or credential leakage into artifacts, logs, or published output.
- Privilege or trust-boundary escapes in the guard, hook, and sandbox surfaces.

## Out of scope

- Findings that require an attacker who already has arbitrary code execution as the same
  local user. At that point the attacker does not need this engine.
- Denial of service through deliberately malformed local input.
- Issues in the third-party dependencies listed in `NOTICE.md` — report those upstream.
- Issues in Claude Code itself — report those to
  [Anthropic](https://github.com/anthropics/claude-code/issues).

## Disclosure

Coordinated. Once a fix is available, credit is offered to the reporter unless anonymity is
preferred.
