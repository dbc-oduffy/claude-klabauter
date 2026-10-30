"""deny_registry.py -- the closed list of guards and op refusals allowed to refuse work.

A guard or op never denies recoverable work. Only a registered entry carrying an
irreversible class may deny; ``machine_profile.apply_guard_level`` clamps every other
guard to a log-only advisory at every level and profile. The ratchet
(``tests/test_deny_registry_ratchet.py``) goes red when a deny site or op refusal is
neither registered nor pending a named fix.

Pure data plus stdlib scanners: imported on the hook hot path, so no spawn and no
heavy import.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Union

#: What may deny. ``box-degrading``: disk fill, runaway processes, whole-filesystem
#: scans, destroying data or history. ``irreversible-external``: push/PR to a non-owned
#: remote, p4 submit/shelve. ``malformed-request`` is op-only: a call the op cannot parse.
BOX_DEGRADING = "box-degrading"
IRREVERSIBLE_EXTERNAL = "irreversible-external"
MALFORMED_REQUEST = "malformed-request"
#: ``disruptive``: a guard-only class for an act that damages peers' in-flight work on the
#: shared tree (a blanket commit sweeping their edits). Not an op refusal class.
DISRUPTIVE = "disruptive"

IRREVERSIBLE_CLASSES = frozenset({BOX_DEGRADING, IRREVERSIBLE_EXTERNAL, MALFORMED_REQUEST})
GUARD_CLASSES = frozenset({BOX_DEGRADING, IRREVERSIBLE_EXTERNAL, DISRUPTIVE})

#: Guards that may deny, each with its class. Keys are kebab-case guard names. A guard
#: registered here denies only its degrading shape; narrowing a shape is the guard's own fix.
GUARD_DENY_CLASS: Dict[str, str] = {
    "block-push-pr-non-owned-remote": IRREVERSIBLE_EXTERNAL,
    "block-perforce-submit": IRREVERSIBLE_EXTERNAL,
    "p4-verb-fence": IRREVERSIBLE_EXTERNAL,
    "destructive-git-orphan": BOX_DEGRADING,
    "destructive-rm": BOX_DEGRADING,
    "destructive-git-clean": BOX_DEGRADING,
    "destructive-git-revert": BOX_DEGRADING,
    "block-stash-destruction": BOX_DEGRADING,
    "block-worktree-creation": BOX_DEGRADING,
    "block-whole-filesystem-scan": BOX_DEGRADING,
    "guard-disk-fill": BOX_DEGRADING,
    "guard-heavy-command-admission": BOX_DEGRADING,
    "block-external-workspace-vcs-write": BOX_DEGRADING,
    "block-editor-kill-by-name": BOX_DEGRADING,
    "blanket-git-add": DISRUPTIVE,
    "check-test-suite-invocation": BOX_DEGRADING,
    "block-dispatch-suite-invocation": BOX_DEGRADING,
    "block-worktree-tool": BOX_DEGRADING,
    "block-workflow-foreign-emission": DISRUPTIVE,
    "runaway-find": BOX_DEGRADING,
}

#: Guards whose deny is registered for a dispatched caller only; the EM's call is a log.
SUBAGENT_DENY_CLASS: Dict[str, str] = {
    "guard-subagent-heavy-ue-launch": BOX_DEGRADING,
    "block-subagent-destructive-action": BOX_DEGRADING,
    #: PM ruling: "subagents should be hard-blocked from changes to CLAUDE.md too".
    #: Always-loaded doctrine is fleet-wide; DISRUPTIVE is the admitted class (a write that
    #: damages shared state peers load).
    "guard-doctrine-surface-edits": DISRUPTIVE,
}

#: Guards ruled advisory after a predicate read against the five deny classes (disk fill,
#: runaway processes, data or history destruction, irreversible external acts, blanket
#: commits): none guards one, so ``apply_guard_level`` logs their would-be deny. One line each.
#: ``tests/test_guard_classification_ratchet.py`` fails on a guard in none of the four maps.
ADVISORY_BY_RULING: Dict[str, str] = {
    "block-derived-global-doctrine-write": "a derived copy; the next derive rewrites it",
    "block-drive-root-new-dir-write": "recoverable clutter at a drive root",
    "block-drive-root-new-dir-bash-write": "recoverable clutter at a drive root",
    "block-home-root-write": "recoverable clutter at the home root",
    "block-home-root-bash-write": "recoverable clutter at the home root",
    "block-em-post-fire-product-write": "an EM write into a live run's paths; recoverable by revert",
    "block-group-em-agent-cap-grant": "prose in a message; nothing is destroyed",
    "block-unenumerated-agent-type": "dispatch roster hygiene; a bad type is a failed dispatch",
    "block-ungranted-opus-subagent": "PM ruling: substitute Sonnet, never reject",
    "check-claude-md-size": "a size budget on a text file; recoverable",
    "enforce-agent-dispatch-mode": "dispatch parameter rewrite",
    "enforce-agent-model-pin": "model pin mismatch; correctable by rewrite",
    "guard-agent-route-admission": "routing mismatch; the dispatch can be redone",
    "guard-brief-prescribed-method": "brief lint; prose only",
    "guard-doctrine-changelog-prose": "doctrine prose style; recoverable",
    "guard-doctrine-surface-bash-write": "doctrine-surface edit through Bash; recoverable by revert",
    "guard-doctrine-surface-ratio": "doctrine weight ratio; recoverable",
    "guard-editor-python-direct": "routing nudge; editor Python is not destructive here",
    "guard-em-prefire-survey": "an EM's read sprawl; no side effect",
    "guard-manufactured-blocker": "Stop-hook prose check; no side effect",
    "guard-named-dispatch-tool-restriction": "dispatch tool strip; correctable by rewrite",
    "guard-python-syntax-on-write": "an unparseable file on disk; recoverable by rewrite",
    "guard-repo-setup-claude-home-refusal": "repo-setup config overwrite under ~/.claude; recoverable from the repo",
    "guard-terminal-review": "Stop-hook review gate; no side effect",
    "guard-turn-intent": "Stop-hook turn record; no side effect",
    "hand-dispatch-planner": "a hand dispatch of a workflow-owned planner; the run can be redone",
    "nudge-em-code-dispatch": "routing nudge for an EM's inline code write",
    "nudge-foreground-agent-dispatch": "foreground to background rewrite",
    "submit-bug-blitz": "submission-shape check; the submit can be redone",
    "submit-engine-ask": "submission-shape check; the submit can be redone",
    "watchdog-undischarged-next-move": "watchdog nudge; no side effect",
    "background-publish": "shell hygiene or routing rule; recoverable",
    "block-approval-sentinel-creation": "sentinel marker file; recreatable",
    "block-completion-monolith-write": "work-state artifact write rule; recoverable by revert",
    "block-confined-agent-write": "dispatch or agent-scope rule; recoverable by redo",
    "block-consumed-handoff-edit": "work-state artifact write rule; recoverable by revert",
    "block-cross-repo-agent-write": "dispatch or agent-scope rule; recoverable by redo",
    "block-cutover-phase-hand-edit": "work-state artifact write rule; recoverable by revert",
    "block-dev-repo-sentinel-removal": "sentinel marker file; recreatable",
    "block-dev-repo-sentinel-removal-advisory": "advisory twin; a sentinel is recreatable",
    "block-dev-repo-sentinel-write": "sentinel marker file; recreatable",
    "block-dev-side-mirror-wiki": "work-state artifact write rule; recoverable by revert",
    "block-disarm-marker-sentinel-write": "sentinel marker file; recreatable",
    "block-duplicate-decision-record-id": "work-state artifact write rule; recoverable by revert",
    "block-em-strict-dispatch-code-write": "dispatch or agent-scope rule; recoverable by redo",
    "block-fleet-delegation-write": "dispatch or agent-scope rule; recoverable by redo",
    "block-foreign-family-sidecar-write": "work-state artifact write rule; recoverable by revert",
    "block-goals-log-hand-write": "work-state artifact write rule; recoverable by revert",
    "block-hand-authored-handoff-creation": "work-state artifact write rule; recoverable by revert",
    "block-hand-authored-sidecar-creation": "work-state artifact write rule; recoverable by revert",
    "block-hand-set-test-verdict": "work-state artifact write rule; recoverable by revert",
    "block-home-dir-memo-delivery": "work-state artifact write rule; recoverable by revert",
    "block-illegal-filename": "shell hygiene or routing rule; recoverable",
    "block-memo-status-hand-edit": "work-state artifact write rule; recoverable by revert",
    "block-noncanonical-branch-creation": "git hygiene rule; recoverable",
    "block-oss-mirror-memo-delivery": "work-state artifact write rule; recoverable by revert",
    "block-priority-ledger-edit": "work-state artifact write rule; recoverable by revert",
    "block-reviewer-bash-outside-allowlist": "dispatch or agent-scope rule; recoverable by redo",
    "block-sizing-object-schema-violation": "work-state artifact write rule; recoverable by revert",
    "block-subagent-archive-write": "work-state artifact write rule; recoverable by revert",
    "block-subagent-commit": "a subagent commit is recoverable by reset; the Commit Gate is doctrine",
    "block-subagent-grant-record-write": "work-state artifact write rule; recoverable by revert",
    "block-subagent-guard-grant-write": "work-state artifact write rule; recoverable by revert",
    "block-subagent-plan-body-bash-write": "work-state artifact write rule; recoverable by revert",
    "block-subagent-plan-body-write": "work-state artifact write rule; recoverable by revert",
    "block-topic-branch": "git hygiene rule; recoverable",
    "block-unauthorized-claude-md-write": "work-state artifact write rule; recoverable by revert",
    "block-worktree-sentinel-creation": "sentinel marker file; recreatable",
    "block-worktree-sentinel-write": "sentinel marker file; recreatable",
    "bump-foreign-repo-write": "out-of-repo write bump; recoverable",
    "bump-out-of-repo-tool-write": "out-of-repo write bump; recoverable",
    "bump-outside-repo-write": "out-of-repo write bump; recoverable",
    "cat-heredoc-write-advise": "nudge; no side effect",
    "check-raw-pid-liveness": "shell hygiene or routing rule; recoverable",
    "deploy-dirty-tree": "dirty-tree deploy check; a deploy is redoable",
    "destructive-git-revert-advisory": "advisory twin of the registered destructive-git-revert",
    "find-exec-rewrite": "command rewrite or offer; the command still runs",
    "git-commit-safe-commit-advise": "nudge; no side effect",
    "git-no-optional-locks": "command rewrite or offer; the command still runs",
    "grep-via-bash-guard": "shell hygiene or routing rule; recoverable",
    "grep-via-bash-rewrite": "command rewrite or offer; the command still runs",
    "guard-class-relay": "shell hygiene or routing rule; recoverable",
    "guard-concrete-path-citations": "shell hygiene or routing rule; recoverable",
    "guard-host-subagent-bash-ban": "dispatch or agent-scope rule; recoverable by redo",
    "guard-host-subagent-bash-spawn-shapes": "dispatch or agent-scope rule; recoverable by redo",
    "guard-memory-store-cap": "work-state artifact write rule; recoverable by revert",
    "guard-post-executor-verification": "shell hygiene or routing rule; recoverable",
    "guard-settings-json-write": "work-state artifact write rule; recoverable by revert",
    "head-tail-plumbing-rewrite": "command rewrite or offer; the command still runs",
    "headless-claude-plugin-dir": "shell hygiene or routing rule; recoverable",
    "heredoc-repo-write-advise": "nudge; no side effect",
    "inprocess-search": "shell hygiene or routing rule; recoverable",
    "move-from-inside-cwd": "shell hygiene or routing rule; recoverable",
    "multiprobe-banner": "shell hygiene or routing rule; recoverable",
    "multiprobe-banner-rewrite": "command rewrite or offer; the command still runs",
    "no-verify": "hook bypass flag; the commit is recoverable and the hook's own verdict stands",
    "nudge-authoring-leak": "nudge; no side effect",
    "nudge-baton-body-bar": "nudge; no side effect",
    "nudge-dangling-sizing-citation": "nudge; no side effect",
    "nudge-handoff-ac-shape": "nudge; no side effect",
    "nudge-handoff-author-lint": "nudge; no side effect",
    "nudge-improvement-queue-write": "nudge; no side effect",
    "nudge-outbox-draft-frontmatter-shape": "nudge; no side effect",
    "nudge-peer-notice-unread": "nudge; no side effect",
    "nudge-plan-sidecar-family-split": "nudge; no side effect",
    "nudge-private-git-fact-resolver": "nudge; no side effect",
    "nudge-prose-queue-append": "nudge; no side effect",
    "nudge-prose-queue-creation": "nudge; no side effect",
    "nudge-sentinel-retained-review-sidecar": "nudge; no side effect",
    "nudge-session-display-name-as-identifier": "nudge; no side effect",
    "nudge-shell-shaped-spawn": "nudge; no side effect",
    "nudge-tasks-state-folder-split": "nudge; no side effect",
    "nudge-terminal-artifact-edit": "nudge; no side effect",
    "nudge-unattributed-process-time-figure": "nudge; no side effect",
    "nudge-unmarked-spawning-test": "nudge; no side effect",
    "nudge-windows-subprocess-popup": "nudge; no side effect",
    "offer-git-c": "command rewrite or offer; the command still runs",
    "offer-invoke-params-stdin": "command rewrite or offer; the command still runs",
    "p4-checkout-before-edit": "shell hygiene or routing rule; recoverable",
    "piped-pytest-exit-advisory": "nudge; no side effect",
    "plumbing-and-loops": "shell hygiene or routing rule; recoverable",
    "powershell-via-bash-guard": "shell hygiene or routing rule; recoverable",
    "reap-stale-git-lock": "lock reaper; removing a stale lock is recoverable",
    "sed-range-read-advise": "nudge; no side effect",
    "stale-write": "shell hygiene or routing rule; recoverable",
    "stash-apply-verification-advisory": "nudge; no side effect",
    "trailing-background-ampersand": "shell hygiene or routing rule; recoverable",
    "validate-commit": "commit-shape check; a commit is recoverable",
    "validate-frontmatter-schema-advisory": "nudge; no side effect",
    "validate-frontmatter-schema-deny": "frontmatter shape check; recoverable by rewrite",
    "wiki-changelog-prose-advisory": "nudge; no side effect",
    "xs-lane-shell-write": "shell hygiene or routing rule; recoverable",
}

PendingFix = Dict[str, str]
OpClass = Union[str, PendingFix]

_OPS = "coordinator_core.ops."
_PIPELINE_PARSE = (
    "dispatch_emit.cli",
    "dispatch_emit.op",
    "dispatch_emit.pipeline_compose",
    "dispatch_emit.pipeline_inputs",
    "dispatch_emit.pipeline_manifest",
    "dispatch_emit.research_emit",
)


def _pending(owner: str) -> PendingFix:
    return {"pending_fix": owner}


#: Op refusals (``raise *Refus*`` under ``coordinator_core/ops``) keyed ``"module:Exception"``.
#: A class is a refusal the op may keep; ``{"pending_fix": <row or run id>}`` names who
#: converts the refusal to a drop, a spill or an admit. Shrink-only.
OP_REFUSAL_CLASS: Dict[str, OpClass] = {
    **{f"{_OPS}{m}:PipelineEmitRefused": MALFORMED_REQUEST for m in _PIPELINE_PARSE},
    _OPS + "research_reground:PipelineEmitRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_compose:AskComposeRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_compose:AskPlanBlitzRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_plan_blitz:AskPlanBlitzRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_roadmap:AskPlanBlitzRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_roadmap:AskComposeRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_fanout:_Refused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.requirements_ledger:LedgerRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.measured_rev:MeasuredRevRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.reverify_delivery:ReverifyRefused": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.sibling_hold:SupersedeRefused": MALFORMED_REQUEST,
    _OPS + "review_mint.supersede:SupersedeRefused": MALFORMED_REQUEST,
    _OPS + "verdict_record_op:TestVerdictRefused": MALFORMED_REQUEST,
    _OPS + "tracker.push_suggestion:PushSuggestionRefused": MALFORMED_REQUEST,
    _OPS + "diagnostics_probes:DiagnosticsRefusal": MALFORMED_REQUEST,
    _OPS + "dispatch_emit.ask_brief:EmBriefRefused": _pending("C5"),
    _OPS + "dispatch_emit.co_declared_settle:OwnHunkRefused": _pending("C11"),
    _OPS + "dispatch_emit.own_hunk_stage:OwnHunkRefused": _pending("C11"),
    _OPS + "dispatch_emit.ask_gate:SizingFireRefused": _pending("C8"),
    _OPS + "dispatch_emit.op:SizingFireRefused": _pending("C8"),
    _OPS + "dispatch_emit.sizing_fire:SizingFireRefused": _pending("C8"),
    _OPS + "dispatch_emit.close_without_script:CloseRefusal": _pending("C8"),
    _OPS + "dispatch_emit.emit:FidelityBarRefusalError": _pending("C8"),
    _OPS + "dispatch_emit.pipeline_compose:_ScriptCapRefused": _pending("C8"),
    _OPS + "_research_sizing:ResearchSizingRefused": _pending("C8"),
    _OPS + "dod_floor_ratchet:RatchetRefused": _pending("C8"),
    _OPS + "grind_ops:VerifyRefusalError": _pending("C8"),
    _OPS + "queue_promote:_OssMirrorWriteRefused": _pending("C8"),
    _OPS + "review_stamp:MintRefusal": _pending("C8"),
}

_RAISE_REFUSAL = re.compile(r"^\s*raise\s+(?:\w+\.)*(\w*Refus\w*)", re.M)
_PENDING_OWNER = re.compile(r"^(C\d+|[A-Za-z0-9][A-Za-z0-9_-]{3,})$")


def guard_deny_class(name: str, *, subagent: bool = False) -> str | None:
    """The registered class for guard ``name`` (kebab or snake), else ``None``."""
    kebab = name.replace("_", "-")
    cls = GUARD_DENY_CLASS.get(kebab)
    if cls is None and subagent:
        cls = SUBAGENT_DENY_CLASS.get(kebab)
    return cls


def op_refusal_sites(ops_root: Path, package: str = "coordinator_core.ops") -> Dict[str, int]:
    """Every ``raise *Refus*`` in non-test modules under ``ops_root``, keyed ``module:Exception``."""
    found: Dict[str, int] = {}
    for path in sorted(ops_root.rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        rel = path.relative_to(ops_root).with_suffix("")
        module = ".".join((package, *rel.parts))
        for match in _RAISE_REFUSAL.finditer(path.read_text(encoding="utf-8")):
            key = f"{module}:{match.group(1)}"
            found[key] = found.get(key, 0) + 1
    return found


def registry_problems(
    guard_classes: Dict[str, str] | None = None,
    op_classes: Dict[str, OpClass] | None = None,
    refusal_sites: Iterable[str] = (),
) -> List[Tuple[str, str]]:
    """``(site, reason)`` for every registry entry or op refusal that breaks the rule.

    Empty means the registry is consistent: every class is in the closed list, every
    pending fix names an owner, and every op refusal site is registered.
    """
    guards = GUARD_DENY_CLASS if guard_classes is None else guard_classes
    ops = OP_REFUSAL_CLASS if op_classes is None else op_classes
    problems: List[Tuple[str, str]] = []
    for name, cls in guards.items():
        if cls not in GUARD_CLASSES:
            problems.append((name, f"guard class {cls!r} is not an irreversible guard class"))
    for key, cls in ops.items():
        if isinstance(cls, dict):
            owner = cls.get("pending_fix", "")
            if set(cls) != {"pending_fix"} or not _PENDING_OWNER.match(str(owner)):
                problems.append((key, f"pending_fix needs a row or run id, got {cls!r}"))
        elif cls not in IRREVERSIBLE_CLASSES:
            problems.append((key, f"op class {cls!r} is not in IRREVERSIBLE_CLASSES"))
    for site in refusal_sites:
        if site not in ops:
            problems.append((site, "op refusal is neither classed nor pending a named fix"))
    return problems
