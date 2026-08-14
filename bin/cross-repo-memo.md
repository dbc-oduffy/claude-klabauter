# cross-repo-memo — send verb breadcrumb

> You are looking for the cross-repo memo **send** verb.

The executable is `cross-repo-memo`, on PATH as `cross-repo-memo`, with a `.cmd`
sibling for native Windows shells. Per DR-210 (the claude-klabauter-native-tooling-ownership
strangler) and `docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md`,
the coordinator executable surface — including this CLI — has migrated out of
DoE-claude and now lives in THIS repo, at `coordinator/bin/cross-repo-memo.py`. It is
no longer a DoE-owned file; resolve it repo-relatively when you're already in this
tree, or via PATH otherwise (PATH setup is machine-local, not this doc's concern).

## One-shot form

```sh
cross-repo-memo --to <receiver-em> --topic <slug> --title "<one-line summary>" \
  [--body-file <path>] [--summary "<tl;dr>"] [--kind ask|consult|fyi] \
  [--self-receipt --decision accepted|declined|partial|superseded] \
  [--supersedes <path>] [--in-reply-to <inbound-memo-basename>]
# body from stdin when --body-file is omitted or '-'
```

Flag reference (verified against `cross-repo-memo --help`):

- `--to RECEIVER_EM_ID` — receiver identifier. `claude-central-em` (aliases `central-em`,
  `central`) always resolves to `~/.claude`; sibling repos resolve via the machine-local
  registry (`repos.<name>`) by convention `<receiver>-em`. Required unless `--list-receivers`.
- `--topic SLUG` — short slug used in the memo filename. Required unless `--list-receivers`.
- `--title ONE_LINE` — one-line memo title. Required unless `--list-receivers`.
- `--body-file PATH` — path to a file containing the memo body; reads stdin if omitted.
- `--self-receipt` — dispatcher IS the receiver; writes terminal `status=actioned`. Requires
  `--decision`.
- `--decision {accepted,declined,partial,superseded}` — required when `--self-receipt` is set.
- `--supersedes PATH` — path to a prior memo this one supersedes.
- `--in-reply-to MEMO` — OPTIONAL. Basename (or path — normalized to basename) of the
  inbound memo this send replies to. Must name a memo present in THIS repo's own
  `cross-repo/inbox/` or `cross-repo/archive/` (searched recursively) — checked before
  anything is written to the receiver, fail-loud on a typo/unresolvable value. Written
  as `in_reply_to:` in frontmatter and consumed by the pickup skill's reply-closure
  check (`compute_reply_closure`); a prose citation of the inbound filename in the body
  still works too — this is a structured alternative, not a replacement. Also available
  on `draft` (`cross-repo-memo draft <topic> --to <em> --title "..." --in-reply-to <memo>`),
  where it is staged into the outbox frontmatter and survives through `send`.
- `--summary TEXT` — one-line tl;dr (≤120 chars); derived from the body's first non-empty
  line if omitted.
- `--kind {ask,consult,fyi}` — sender-declared memo shape. Omitted means no `kind:` line
  (readers apply an `ask` default). `ack` is not a valid kind.
- `--list-receivers` — print every valid `--to` target on this machine and exit.

## Draft-then-send form (preferred for anything longer than a few lines)

```sh
cross-repo-memo draft <topic> --to <receiver-em> --title "<one-line summary>"
# -> stages a draft at state/memo-outbox/<topic>.md; edit the body in place
cross-repo-memo send <topic>
```

**The draft is scaffolding, and `send` consumes it.** On a successful send the local
`state/memo-outbox/<topic>.md` is **deleted** — the sent copy lives in the receiver's
`cross-repo/inbox/`, and this repo's durable record of the exchange is the archived
inbound reply plus whatever the commit message captures. Nothing is meant to accumulate
in the outbox.

**Corollary worth knowing before you go looking:** an absent outbox draft is the *normal*
post-send state, NOT evidence that a send failed. A memo whose draft is missing and whose
receiver-side copy exists was sent successfully. Verify delivery by checking the
receiver's `cross-repo/inbox/` (or their `cross-repo/archive/`, if they have already
actioned it) — never by looking for a surviving local draft.

<!-- Review: this corollary is here because its absence cost a real escalation — the
     2026-07-17 native-schema plan's Decision brief escalated "C10a's memo may never have
     been sent" to the PM as a human-only item, reasoning from the missing outbox draft.
     The memo had in fact landed and been actioned in the receiver's tree. -->

## Example — memo to central EM

```sh
echo "claude-klabauter/rag boundary: state/ reconciliation item — see docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md" \
  | cross-repo-memo --to claude-central-em --topic claude-klabauter-rag-boundary-note \
      --title "claude-klabauter/rag boundary: state/ reconciliation item" --kind fyi
```

Sent memos land in the receiver's `cross-repo/inbox/` (`delivery_mode: receiver-repo`)
and are **committed in the receiver repo on landing** — a committed-but-unpushed fact,
with all receiver hooks neutralized for that one commit via `core.hooksPath` (no foreign
trailer, no auto-push), per DR-214 Amendment A1. Propagation is the receiver's own next
push; the receiver actions the memo in place from there. (The delivery-commit is a
deliberate, sanctioned exception to "don't touch others' repos"; the commit mechanism is
DoE-owned in the CLI, so the observable contract is simply: delivery is committed, not
left dirty.)

Run `cross-repo-memo --list-receivers` for valid `--to` targets on this machine.
Run `cross-repo-memo --help` for the full flag reference.

See also: `cross-repo/README.md` for both inbound and outbound channel docs.
