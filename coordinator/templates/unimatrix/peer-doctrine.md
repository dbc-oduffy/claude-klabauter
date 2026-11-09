## Fleet peers

You are a colleague in a multi-agent fleet: Claude sessions, Codex and agy work side by side.

- A message that starts with `[<sender>]` comes from another agent, not from the PM.
- The PM is the human at your keyboard. Plain answers in this window go to the PM.
- Answer an agent by sending to that agent. Do not print the answer for the PM to read:

  `{SEND} --to <sender> "<your reply>"`

- List the peers you can message with `{SEND} --list`. Start a message to any of them the same way.
- Make every reply self-contained: the reader has not seen your window.

Codex sub-agents are reached through their parent root session. They cannot accept
independent Codex input, so the router excludes discovered child threads from its
sendable peers. Send to the verified parent name from `--list`, identify the child
by its task path or thread ID, and ask the parent to forward the message using its
collaboration tools. A child working on another repository can still share its
parent's working directory; the directory alone does not establish task ownership.

The router does not automatically create a child inbox or short-name alias such as
`codex-rag-c8`. `--as` identifies the sender; it does not register a destination.
An unregistered short name fails with `no session named`. Legacy remote routers
may still advertise child threads, but those rows do not make the children directly
messageable. Verify the parent instead of guessing an alias or assuming a forwarder
exists.

For an explicitly owned temporary reply endpoint, the inbox client can use
`--reply-inbox`, which requests the relay's `reply-inbox <name>` mode. The receiving
host must support that mode. It advertises the endpoint to directly connected
boxes while the owner's SSH connection stays open; disconnecting removes it.
Ordinary `inbox` registrations remain local-only, and downstream mirrors are never
re-exported. This captures messages for the owning client; it does not enable
native input to a Codex child thread.

Use the reachable name shown by `--list`, not a remote `/tmp` socket path. If the
temporary endpoint is absent, use its verified parent route instead. Inbox names
accept 1–64 ASCII letters, digits, underscores, hyphens and interior single dots;
paths, whitespace, `@`, edge dots and consecutive dots are rejected.

For example, route a child response through its verified parent:

    {SEND} --to <verified-parent-name> "For child <task-path-or-thread-id>: <response>"

Example. You receive:

    [peer-a] Which test file covers the roster? Reply with the path.

You run:

    {SEND} --to peer-a "coordinator/bin/tests/test_unimatrix_mirror_roster.py"
