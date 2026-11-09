# Non-Claude session identity and PM ledger

The same engine session store serves Claude, Codex and other launchers. Codex supplies
`CODEX_THREAD_ID`; agy supplies `ANTIGRAVITY_CONVERSATION_ID`; other launchers supply
`COORDINATOR_AGENT_VENDOR`, `COORDINATOR_AGENT_SESSION` and optionally
`COORDINATOR_AGENT_MODEL`. Set those variables on the engine caller, including warm
calls. An explicit `COORDINATOR_SESSION_ID` takes precedence. Native IDs beat inherited
Claude identity variables. UUID native IDs remain unchanged; opaque IDs map to stable,
vendor-namespaced UUIDs through `session.core.canonical_session_env_value`. Provenance
keeps the original native ID.

A harness without Claude's SessionStart hook must initialize its session once. Through
the normal warm JSON-RPC door, call `session_baton.mint` with the canonical `session_id`,
`initialize: true`, the absolute repository `cwd`, and optionally `prompt`, `title` and
`intent`. This uses the sole session directory constructor and fails visibly if the
metadata could not be created. No Claude rollout or transcript is required. Omitting
`initialize` preserves the existing requirement that a session already exists.

Append each PM instruction verbatim using `baton.pm_turn_append` with `prompt`. Read
it using `baton.pm_turns` with `all: true`, a `turn`, or inclusive `start`/`end`.
Both infer the native caller's canonical identity, or accept explicit `session_id`.
The private ledger lives at `<git-common-dir>/coordinator-sessions/<session_id>/pm_turns.jsonl`;
baton and session metadata share that directory. PM-ask verification compares ledger
turns, so pass the exact complete PM instruction, including whitespace.

Use the existing engine pickup, handoff and memo operations with this same identity;
no vendor-specific transcript lookup should be needed for attribution. Cross-repository
memos continue through the cross-repo-memo lifecycle. The workflow and engine-ask
submission steps are documented in [the Codex route](codex-engine-ask-route.md).

Agent trailer emission is currently held by the frozen producer contract §5.2. Both
Example-retrieval-repo and example-cockpit-repo must explicitly ACK reader readiness; their checked-in
references belong in `agent_provenance.WRITER_READINESS_ACKS`. An environment variable
cannot bypass this hold. The 2026-11-07 lifecycle notifications are
`native-agent-trailer-readiness` (rag) and `native-agent-trailer-readiness-cockpit` (cockpit).
Neither ACK was present when this change was prepared. Session identity and PM-ledger
operations work during the hold, and forged Agent trailers are still removed.

After both readiness references are recorded, `ceremony.commit_v2` stamps `Agent-Vendor`, `Agent-Model`, `Agent-Tier` and
`Agent-Session-Id` from caller context, replacing message-authored Agent trailers.
An unknown model stays unknown; Terra has no verified tier assignment. Claude gets no
Agent trailers. A warm request cannot inherit the daemon spawner's provenance.
An explicit commit `session_id` is also carried into the contract ledger, even when
there is no ambient session environment. These changes add no runtime process spawns
and use the same Python implementation on Windows and POSIX.
