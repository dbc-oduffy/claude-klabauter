"""Bash-guard census: one fate per roster guard.

Fate rule: ``block`` only for a ``deny_registry`` entry (``GUARD_DENY_CLASS`` or
``SUBAGENT_DENY_CLASS``), each naming its elegant redirect in ``_REDIRECT``. A guard whose only
value is the deny it can no longer issue is ``deleted`` (``_DELETED``: module, tests, dispatch
wiring and pins removed; bash guards are chain-dispatched, so none held a per-guard hooks.json
entry). Every other guard is ``guide``: its deny paths resolve through
``machine_profile.apply_guard_level``, which logs an off-registry deny as one advisory line and
never refuses. Block rows are derived from the registry so a registry shrink (C8) cannot strand a
stale block row.
"""

from __future__ import annotations

from coordinator_core import deny_registry
from coordinator_core.guard_census import BLOCK, DELETED, GUIDE, Row, enumerate_bash_guards

_REDIRECT = {
    "block-push-pr-non-owned-remote": "push to an owned remote, or ask the PM to publish",
    "block-perforce-submit": "the p4 review flow; the PM submits",
    "p4-verb-fence": "the read-only p4 verbs",
    "destructive-git-orphan": "a non-destructive ref operation",
    "destructive-rm": "the trash-then-prune op",
    "destructive-git-clean": "git clean -n, then the trash-then-prune op",
    "destructive-git-revert": "git revert of named paths, or restore from a ref",
    "block-stash-destruction": "git stash show; keep the stash",
    "block-worktree-creation": "disjoint file scope on the shared tree",
    "block-whole-filesystem-scan": "a scoped search under the repo",
    "guard-disk-fill": "write under the repo or scratch on a drive with headroom",
    "runaway-find": "a find rooted under the repo with -maxdepth, or example-retrieval-repo",
    "guard-heavy-command-admission": "the engine's heavy-run admission",
    "block-external-workspace-vcs-write": "the workspace's own owner",
    "block-editor-kill-by-name": "close the editor by pid",
    "blanket-git-add": "ceremony.commit_v2 with literal paths",
    "check-test-suite-invocation": "a scoped pytest run of named test files, or the EM's scheduled suite leg",
    "guard-subagent-heavy-ue-launch": "return the launch to the EM as a slot leg",
    "block-subagent-destructive-action": "the trash-then-prune op",
}

#: Deny-only bodies executed away. The sentinel and grant guards policed a door now owned by the
#: PM allowlist; the resave, handoff and fleet-delegation guards only walled an act.
_DELETED = (
    "block-disarm-marker-sentinel-creation",
    "block-fleet-delegation-creation",
    "block-hand-authored-handoff-creation",
    "block-subagent-findings-reject",
    "block-subagent-grant-acquisition",
    "block-subagent-guard-grant",
    "block-subagent-stash-creation",
    "block-unreal-engine-resave",
)

#: Deny-only bodies still on the roster, each with what blocks its deletion. The delete pass
#: finishes them once the named dependent is cleared.
DENY_ONLY_CANDIDATES = {
    "block-approval-sentinel-creation": "C8-pinned; shares _sentinel_creation_guard",
    "block-em-post-fire-product-write": "C8-pinned",
    "block-noncanonical-branch-creation": "coordinator_core/conservatism/tests/test_declared_directions_hold.py imports it",
    "block-reviewer-bash-outside-allowlist": "ops, git.commit and session identity import its helpers",
    "block-subagent-commit": "dispatch_checks, _dc_commit and em_only_act import its helpers",
    "block-subagent-plan-body-bash-write": "coordinator_core/tests/test_raw_writes_have_a_disposition.py and coordinator/tests/test_run_report_retirement_grep_clean.py name the file",
    "block-topic-branch": "block_push_pr_non_owned_remote imports its helpers",
    "block-worktree-sentinel-creation": "coordinator_core/conservatism/tests/test_declared_directions_hold.py imports it",
}

_REGISTERED = {**deny_registry.GUARD_DENY_CLASS, **deny_registry.SUBAGENT_DENY_CLASS}

ROWS = [
    Row(name, BLOCK, _REDIRECT[name]) if name in _REGISTERED else Row(name, GUIDE)
    for name in enumerate_bash_guards()
] + [Row(name, DELETED) for name in _DELETED]
