"""Hook-plane census: one fate per ``coordinator_core/hooks`` module.

Fate rule: ``block`` only for a ``deny_registry`` entry (``_REDIRECT`` names each); every other
hook is ``guide``. A hook's deny paths resolve through ``machine_profile.apply_guard_level``, which
logs an off-registry deny and never injects it; ``guard-em-prefire-survey`` emits its own
context-only nudge. Off the registry ``apply_guard_level`` turns a deny into one nudge line, so a
deny-only hook still guides, and each is imported by a dispatcher, registry or pin file; the
exceptions are ``_DELETED``, bodies removed under DR-475. ``DENY_ONLY_CANDIDATES`` is read by no code; it is the hand-read worklist for the hook delete pass.
``WIRED`` records each module's direct DoE ``hooks.json`` event (C6 memo input);
a module absent from it is reached through a dispatcher or a ``hooks/scripts`` wrapper.
"""

from __future__ import annotations

from coordinator_core.guard_census import BLOCK, DELETED, GUIDE, Row

WIRED = {
    "assert-em-role": "SessionStart",
    "block-group-em-agent-cap-grant": "PreToolUse",
    "block-worktree-tool": "PreToolUse",
    "context-pressure-precompact": "PreCompact",
    "flag-em-poll-in-flight": "PreToolUse",
    "guard-agent-route-admission": "PreToolUse",
    "guard-brief-prescribed-method": "PreToolUse",
    "guard-config-change-hookstack-selfdefence": "ConfigChange",
    "guard-editor-python-direct": "PreToolUse",
    "guard-manufactured-blocker": "Stop",
    "guard-turn-intent": "Stop",
    "nudge-autonomous-askuserquestion": "PreToolUse",
    "nudge-cross-repo-cwd-boundary": "CwdChanged",
    "nudge-named-agent-report-delivery": "PreToolUse",
    "plan-persistence-check": "PostToolUse",
    "postuse-advisory-dispatch": "PostToolUse",
    "postuse-stop-family-dispatch": "PostToolUse",
    "postuse-subagent-compaction-warning": "PostToolUse",
    "postusefailure-cross-repo-memo-remediate": "PostToolUseFailure",
    "preuse-agent-dispatch": "PreToolUse",
    "preuse-bash-dispatch": "PreToolUse",
    "preuse-search-dispatch": "PreToolUse",
    "preuse-sendmessage-dispatch": "PreToolUse",
    "preuse-skill-dispatch": "PreToolUse",
    "preuse-write-dispatch": "PreToolUse",
    "runtime-tripwire-em-check": "PostToolUse+UserPromptSubmit",
    "sessionend-archive-session": "SessionEnd",
    "sessionstart-async-dispatch": "SessionStart",
    "sessionstart-dispatch": "SessionStart",
    "stop-dispatch": "Stop",
    "subagent-zero-tool-use-detect": "SubagentStop",
    "taskstop-launch-recorder": "PostToolUse",
    "taskstop-reaper": "PreToolUse",
    "track-dispatched-agents": "SubagentStart",
    "watchdog-undischarged-next-move": "PostToolUse",
}

# Deny-only bodies whose only value is a deny that cannot issue off the registry.
DENY_ONLY_CANDIDATES = (
    "block-dispatch-suite-invocation",
    "block-unenumerated-agent-type",
    "block-ungranted-opus-subagent",
    "block-workflow-foreign-emission",
    "block-workflow-unmodeled-agent",
    "block-worktree-tool",
    "enforce-agent-dispatch-mode",
    "enforce-agent-model-pin",
    "hand-dispatch-deny",
    "sizing-scout-deny",
)

_HELPERS = (
    "bin-impl-drift",
    "cockpit-session",
    "foreground-dispatch-strip",
    "forwarder-resolve",
    "git-common-dir",
    "guard-runner",
    "guard-runner-contract",
    "message-envelope",
    "named-dispatch-strip",
    "next-move-ledger",
    "plan-path-bridge",
    "posture",
    "sentinel-write-guard",
    "session-hub",
    "skill-invocation",
    "stop-family-runner",
    "stop-family-runner-contract",
    "touch-record",
    "turn-intent",
    "worktree-isolation-strip",
)

_TEST_MODULES = (
    "test-agent-postuse-dispatch",
    "test-auto-push",
    "test-day-branch-assert-designation",
    "test-em-report-altitude",
    "test-flag-em-poll-in-flight",
    "test-guard-config-change-hookstack-selfdefence",
    "test-nudge-cross-repo-cwd-boundary",
    "test-nudge-harness-directive-dispatch",
    "test-nudge-unrouted-sizing",
    "test-platform-localize",
    "test-postuse-advisory-dispatch",
    "test-postusefailure-cross-repo-memo-remediate",
    "test-session-start-write-plugin-root-breadcrumb",
    "test-subagent-arrival-check",
    "test-subagent-fabrication-check",
    "test-subagent-sidecar-fill-check",
    "test-subagent-zero-tool-use",
    "test-subagent-zero-tool-use-resolve",
    "test-subagent-zero-tool-use-surface",
    "test-suggest-sonnet-research",
    "test-ue-knowledge-distrust",
)

_DISPATCHED = (
    "agent-completion-log",
    "agent-postuse-dispatch",
    "allow-emitted-workflow-fire",
    "auto-push",
    "block-dispatch-suite-invocation",
    "block-unenumerated-agent-type",
    "block-ungranted-opus-subagent",
    "block-workflow-foreign-emission",
    "block-workflow-unmodeled-agent",
    "cater-subagent-start",
    "check-claude-md-size",
    "claude-md-ledger",
    "compact-summary-constraints",
    "coordinator-reminder",
    "day-branch-assert",
    "derive-global-doctrine-live-copy",
    "derive-setup-copies",
    "dispatch-escape",
    "doc-data-extensions",
    "doctrine-changelog-prose",
    "doctrine-surface-netting",
    "em-report-altitude",
    "enforce-agent-dispatch-mode",
    "enforce-agent-model-pin",
    "fanin-registries",
    "foreign-path-filter",
    "group-em-autofire",
    "group-em-park-spool",
    "guard-doctrine-changelog-prose",
    "guard-doctrine-surface-ratio",
    "guard-doctrine-surface-ratio-precommit",
    "guard-handoff-summary-cap-on-write",
    "guard-hook-generation-self-probe",
    "guard-host-subagent-bash-ban",
    "guard-host-subagent-bash-spawn-shapes",
    "guard-named-dispatch-tool-restriction",
    "guard-phantom-staged-deletion-precommit",
    "guard-posix-invocation-doctrine-write",
    "guard-python-syntax-on-write",
    "guard-repo-setup-claude-home-refusal",
    "guard-terminal-review",
    "guard-test-tree-git-fixture-spawn",
    "hand-dispatch-deny",
    "handoff-segment-inject",
    "mise-autofire",
    "nudge-em-code-dispatch",
    "nudge-em-turn-budget",
    "nudge-foreground-agent-dispatch",
    "nudge-hand-written-plan",
    "nudge-harness-directive-dispatch",
    "nudge-initiative-goals-ladder",
    "nudge-multiwave-workflow",
    "nudge-plan-test-surface-tier",
    "nudge-unauthorized-handoff",
    "nudge-unrouted-sizing",
    "nudge-workflow-authoring-trampoline",
    "observe-config-change",
    "observe-post-compact",
    "offer-exploration-tier-dispatch",
    "oss-operative-strings",
    "phantom-staged-deletion",
    "pickup-autofire",
    "platform-localize",
    "posix-invocation-detect",
    "project-orientation",
    "example-retrieval-repo-detect",
    "receiver-state-sensor",
    "repin-cloud-engine-root",
    "runtime-tripwire-stop-watcher",
    "scratch-size-probe",
    "session-start-announce-job-mode",
    "session-start-cloud-focus",
    "session-start-guard-plane-check",
    "session-start-register-published-engine",
    "session-start-repair-prepare-commit-msg-hook",
    "session-start-watch-presence",
    "session-start-write-plugin-root-breadcrumb",
    "sessionend-auto-commit",
    "sessionstart-bin-drift-refresh",
    "sessionstart-ensure-http-forwarder",
    "sizing-scout-deny",
    "strip-worktree-isolation",
    "subagent-arrival-check",
    "subagent-arrival-record",
    "subagent-fabrication-check",
    "subagent-review-mark",
    "subagent-sidecar-fill-check",
    "subagent-zero-tool-use",
    "subagent-zero-tool-use-resolve",
    "subagent-zero-tool-use-surface",
    "submit-bug-blitz",
    "submit-engine-ask",
    "suggest-sonnet-research",
    "sweep-boot",
    "track-touched-files",
    "ue-knowledge-distrust",
)

#: Deny-only bodies executed under DR-475 (commit 900eac166a): the dispatch gate refused recoverable work.
_DELETED = (
    "general-purpose-code-handoff-deny",
    "unsized-writer-dispatch-deny",
)

_REDIRECT = {
    "block-worktree-tool": "disjoint file scope on the shared tree",
    "block-workflow-foreign-emission": "emit the script from this session, then fire it",
    "block-dispatch-suite-invocation": "name the touched test files or node ids in the brief",
}


def _row(n: str, note: str) -> Row:
    return Row(n, BLOCK, _REDIRECT[n]) if n in _REDIRECT else Row(n, GUIDE, note=note)


ROWS = (
    [_row(n, f"DoE hooks.json {event}; deny paths resolve through apply_guard_level") for n, event in WIRED.items()]
    + [Row("guard-em-prefire-survey", GUIDE, note="emits a context-only nudge, never a deny; DoE wires it through preuse-bash-dispatch and preuse-search-dispatch")]
    + [_row(n, "no direct hooks.json entry; reached through a dispatcher or wrapper") for n in _DISPATCHED]
    + [Row(n, GUIDE, note="support module, no verdict of its own") for n in _HELPERS]
    + [Row(n, GUIDE, note="test module sitting in the hooks package; enumerated as a module, no verdict") for n in _TEST_MODULES]
    + [Row(n, DELETED) for n in _DELETED]
)
