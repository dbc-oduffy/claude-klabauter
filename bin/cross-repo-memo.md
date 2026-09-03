# cross-repo-memo — send verb breadcrumb

> You are looking for the cross-repo memo **send** verb.

The executable is `cross-repo-memo`, on PATH, with a `.cmd` sibling for native
Windows shells. Its source sits in this repo at `coordinator/bin/cross-repo-memo.py`,
and klabauter carries the published twin downstream of it.

Do not read that location as ownership. DR-210 assigns the CLI shell, the memo
contract and the receiver-resolution loader to DoE-claude, and gives claude-klabauter only
the authoritative work-state-mutation ops, which then strangle the verbs one at a
time underneath a stable command name. `send` is the verb that has reached its
native op; it REFUSES outright when the klabauter engine is unreachable, with no
direct-write fallback. DR-210 names "claude-klabauter owns cross-repo-memo" as the premise it
was written to retire, so do not cite it for the opposite.

## Draft, then send

The one-shot flag form (a whole memo in flags, and its stdin variant) was RETIRED.
`argparse` rejects it — it is not a deprecated-but-working path. Two steps:

```sh
cross-repo-memo draft <topic> --to <receiver-em> --title "<one-line>" --kind ask|consult|fyi|proposal
# edit the printed outbox path, then:
cross-repo-memo send <topic>
```

Verbs: `draft`, `compose` (reprint the outbox path; `--open` execs `$EDITOR`),
`list` (outbox drafts with age, >24h marked stale), `discard` (drop an unsent
draft), `reconcile` (move already-delivered entries to `sent/` so `list` counts
work rather than files), and `send`.

`--help` is the authority on flags and this doc deliberately does not mirror it.
Two that are easy to get wrong:

- `--summary TEXT` — one-line tl;dr, capped at 120 chars; derived from the body's
  first non-empty line if omitted. The cap is enforced by the frontmatter
  validator's cross-field rule, not by the CLI.
- `--in-reply-to MEMO` — OPTIONAL. Basename of the inbound memo this send replies
  to. Must name a memo present in this repo's own `state/cross-repo/inbox/` or
  `state/cross-repo/archive/` (searched recursively), checked before anything is
  written to the receiver, fail-loud on an unresolvable value. Written as
  `in_reply_to:` in frontmatter and consumed by the pickup skill's reply-closure
  check (`compute_reply_closure`). A prose citation of the inbound filename in the
  body still works — this is a structured alternative, not a replacement. Also
  accepted on `draft`, where it is staged into the outbox frontmatter and survives
  through `send`.

`--help`'s legacy flag group is headed "LEGACY FLAG FORM (discovery-only now)" and
keeps only `--check-addressee` and `--list-receivers`. Everything else that once
sat there is gone, not deprecated-but-working.

**Never write memo bodies to `%TEMP%` or `tasks/` paths — the CLI owns the buffer.**

**The draft is scaffolding, and `send` moves it aside.** On a successful send the
local `state/memo-outbox/<topic>.md` is stamped and parked at
`state/memo-outbox/sent/<topic>.md`. It is not deleted — the only `os.remove` in the
CLI belongs to `discard`. The stamped copy moves because the stale-draft nudge scans
the outbox directory non-recursively with no status filter, so a stamped file left in
place would be flagged as a stale draft forever, and `sent/` is a subdirectory that
scan never descends into. Nothing is meant to accumulate at the top of the outbox.

**Corollary worth knowing before you go looking:** the outbox tells you nothing about
whether a send succeeded, in either direction. A missing draft is not proof it failed,
and a file under `sent/` is not proof it is still waiting. Verify delivery by checking
the receiver's inbox, or their archive if they have already actioned it — never by
reading the sender-side outbox.

<!-- Review: this corollary is here because its absence cost a real escalation — the
     2026-07-17 native-schema plan's Decision brief escalated "C10a's memo may never have
     been sent" to the PM as a human-only item, reasoning from the missing outbox draft.
     The memo had in fact landed and been actioned in the receiver's tree.
     Stated as absence-means-sent until 2026-09-03, which was wrong in the other
     direction: `send` parks the stamped draft under sent/ rather than deleting it, so
     a reader looking for the promised absence finds a file and has no reading for it.
     Reported by claude-klabauter-45. -->

## Where a sent memo lands

The inbox root is resolved **per receiver**, not as a fixed subpath:
`<receiver-repo>/state/cross-repo/inbox/` when that root exists on disk, else the
legacy `<receiver-repo>/cross-repo/inbox/`. Repos are migrating to the `state/`
root one at a time, so both answers are live and neither is the fleet-wide truth.
This repo finished its move: claude-klabauter's own channel — inbox, outbox and archive —
is under `state/cross-repo/`, and the legacy root is retired rather than kept as a
second home.

Delivered memos are **committed in the receiver repo on landing** — a
committed-but-unpushed fact, with all receiver hooks neutralized for that one commit
via `core.hooksPath` (no foreign trailer, no auto-push), per DR-214 Amendment A1.
Propagation is the receiver's own next push; the receiver actions the memo in place
from there. (The delivery-commit is a deliberate, sanctioned exception to "don't touch
others' repos"; the commit mechanism lives in the CLI, so the observable contract is
simply: delivery is committed, not left dirty.)

## Example — memo to the DoE EM

```sh
cross-repo-memo --list-receivers          # resolve the current id; never type one from memory
cross-repo-memo draft claude-klabauter-rag-boundary-note --to doe-claude-em --kind fyi --title "claude-klabauter/rag boundary: state/ reconciliation item"
# write the body into the printed outbox path, then:
cross-repo-memo send claude-klabauter-rag-boundary-note
```

Stale drafts (>24h) surface at `/workstream-start` and `/workday-start`.

## Inbound

This tool manages OUTBOUND drafts only. To close a memo already sitting in your own
`state/cross-repo/inbox/`, use `archive-stamp-cli resolve-memo` instead. See
`state/cross-repo/inbox/README.md` for the inbound side of the channel.
