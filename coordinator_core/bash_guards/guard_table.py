"""Static guard table: which Bash-chain guards a command can trigger, readable before any guard module is imported.

`dispatch._build_guard_chain(prune=True)` consults `trigger_wanted` so a command imports only the guards
whose triggers it holds plus every ALWAYS guard. A row's triggers are the guard's own proven necessary
substrings (`CRASH_TRIGGERS`, pinned wider than each guard's denials by test_crash_trigger_is_wider_than_its_guard.py);
a guard with no proven necessary substring is ALWAYS. `test_guard_table_drift.py` fails when a row diverges
from its guard module's own MATCHERS or from `CRASH_TRIGGERS`.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, NamedTuple, Optional, Tuple

#: A row whose triggers are ALWAYS is never pruned.
ALWAYS = None

#: Per-guard necessary preconditions, lowercase: every command the guard could deny holds at least one of them
#: in some `dispatch._crash_probe_variants` normalization. Absent guard: no proven trigger. Also scopes a
#: crashed fail-closed guard's deny (`dispatch._crash_deny_is_out_of_class`).
CRASH_TRIGGERS: Dict[str, Tuple[str, ...]] = {
    "no-verify": ("git",),
    "destructive-git-orphan": ("git",),
    "destructive-git-clean": ("git",),
    "destructive-git-revert": ("git",),
    "blanket-git-add": ("git",),
    "stale-temp-index-base": ("git",),
    "destructive-rm": ("rm",),
    "runaway-find": ("find",),
    "stale-write": (">", "tee"),
    "block-worktree-creation": ("worktree",),
    "p4-verb-fence": ("p4", "git", "attrib", "chmod", "encodedcommand"),
    "block-approval-sentinel-creation": ("doctrine-edit-approved", "doctrine_edit_gate", "guard_level", "machine_local_coordinator_", "xargs", "sh", "python"),
    "block-worktree-sentinel-creation": ("override-worktree-guard", "xargs", "sh", "python"),
    "block-stash-destruction": ("stash",),
    "block-perforce-submit": ("p4", "submit", "shelve"),
    "block-external-workspace-vcs-write": ("git",),
    "block-push-pr-non-owned-remote": ("push", "pr"),
    "block-empty-source-push": ("push",),
    "block-held-push": ("push",),
    "block-whole-filesystem-scan": ("find", "rg", "grep", "egrep", "fgrep", "fd", "fdfind"),
    "guard-uncapped-pytest-plugin": ("pytest", "py.test"),
    "block-editor-kill-by-name": ("taskkill", "stop-process", "spps", "pkill", "killall", "kill"),
    "deny-kill-by-port-or-tree": ("kill", "stop-process", "spps", "fuser", ".sh", ".bash", ".ps1", ".py"),
    "guard-repo-setup-claude-home-refusal": ("repo-setup-args-and-register", "scaffold_structure"),
}


class GuardRow(NamedTuple):
    """One guard: its `bash_guards` module, the tool names its own MATCHERS declares, and its triggers (None = ALWAYS)."""

    module: str
    matchers: Tuple[str, ...]
    triggers: Optional[Tuple[str, ...]]


_CMD = ("Bash", "PowerShell")

#: Every guard module `dispatch._any_declared_matchers` reads, plus the pruned `guard-uncapped-pytest-plugin`.
#: The 16 `_CRASH_DENY_EXEMPT` guards and every caller-identity guard are ALWAYS.
ROWS: Dict[str, GuardRow] = {
    "block-subagent-plan-body-bash-write": GuardRow("block_subagent_plan_body_bash_write", _CMD, ALWAYS),
    "block-reviewer-bash-outside-allowlist": GuardRow("block_reviewer_bash_outside_allowlist", _CMD, ALWAYS),
    "block-subagent-destructive-action": GuardRow("block_subagent_destructive_action", _CMD, ALWAYS),
    "block-illegal-filename": GuardRow("block_illegal_filename", _CMD, ALWAYS),
    "check-test-suite-invocation": GuardRow("check_test_suite_invocation", _CMD, ALWAYS),
    "block-subagent-commit": GuardRow("block_subagent_commit", _CMD, ALWAYS),
    "check-raw-pid-liveness": GuardRow("check_raw_pid_liveness", _CMD, ALWAYS),
    "block-worktree-creation": GuardRow("block_worktree_creation", _CMD, CRASH_TRIGGERS["block-worktree-creation"]),
    "block-approval-sentinel-creation": GuardRow("block_approval_sentinel_creation", _CMD, CRASH_TRIGGERS["block-approval-sentinel-creation"]),
    "block-worktree-sentinel-creation": GuardRow("block_worktree_sentinel_creation", _CMD, CRASH_TRIGGERS["block-worktree-sentinel-creation"]),
    "block-topic-branch": GuardRow("block_topic_branch", _CMD, ALWAYS),
    "block-perforce-submit": GuardRow("block_perforce_submit", _CMD, CRASH_TRIGGERS["block-perforce-submit"]),
    "block-external-workspace-vcs-write": GuardRow("block_external_workspace_vcs_write", _CMD, CRASH_TRIGGERS["block-external-workspace-vcs-write"]),
    "block-push-pr-non-owned-remote": GuardRow("block_push_pr_non_owned_remote", _CMD, CRASH_TRIGGERS["block-push-pr-non-owned-remote"]),
    "block-empty-source-push": GuardRow("block_empty_source_push", _CMD, CRASH_TRIGGERS["block-empty-source-push"]),
    "block-held-push": GuardRow("block_held_push", _CMD, CRASH_TRIGGERS["block-held-push"]),
    "guard-deploy-dirty-tree": GuardRow("guard_deploy_dirty_tree", _CMD, ALWAYS),
    "block-editor-kill-by-name": GuardRow("block_editor_kill_by_name", _CMD, CRASH_TRIGGERS["block-editor-kill-by-name"]),
    "deny-kill-by-port-or-tree": GuardRow("block_kill_by_port_or_tree", _CMD, CRASH_TRIGGERS["deny-kill-by-port-or-tree"]),
    "block-whole-filesystem-scan": GuardRow("block_whole_filesystem_scan", _CMD, CRASH_TRIGGERS["block-whole-filesystem-scan"]),
    "guard-uncapped-pytest-plugin": GuardRow("guard_uncapped_pytest_plugin", _CMD, CRASH_TRIGGERS["guard-uncapped-pytest-plugin"]),
    "block-stash-destruction": GuardRow("block_stash_destruction", _CMD, CRASH_TRIGGERS["block-stash-destruction"]),
    "block-noncanonical-branch-creation": GuardRow("block_noncanonical_branch_creation", _CMD, ALWAYS),
    "inprocess-search": GuardRow("guard_inprocess_search", _CMD, ALWAYS),
    "grep-via-bash-guard": GuardRow("guard_grep_via_bash", _CMD, ALWAYS),
    "multiprobe-banner": GuardRow("guard_multiprobe_banner", _CMD, ALWAYS),
    "plumbing-and-loops": GuardRow("guard_plumbing_and_loops", _CMD, ALWAYS),
    "guard-repo-setup-claude-home-refusal": GuardRow("guard_repo_setup_claude_home_refusal", _CMD, CRASH_TRIGGERS["guard-repo-setup-claude-home-refusal"]),
}

#: Master-gate set of `dispatch._any_declared_matchers`: "Bash" plus every row's own declared tool names.
DECLARED_MATCHERS: FrozenSet[str] = frozenset(("Bash",)).union(*(row.matchers for row in ROWS.values()))


def trigger_wanted(name: str, variants: Tuple[str, ...]) -> bool:
    """True unless `name` has a row with triggers and none of them occurs in any lowercase probe variant of the command.

    An unknown guard or an ALWAYS row is wanted. `variants` come from `dispatch._crash_probe_variants`.
    """
    row = ROWS.get(name)
    if row is None or row.triggers is ALWAYS:
        return True
    return any(token in variant for variant in variants for token in row.triggers)
