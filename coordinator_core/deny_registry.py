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
    "guard-heavy-command-admission": BOX_DEGRADING,
    "block-external-workspace-vcs-write": BOX_DEGRADING,
    "block-editor-kill-by-name": BOX_DEGRADING,
    "blanket-git-add": DISRUPTIVE,
    "check-test-suite-invocation": BOX_DEGRADING,
}

#: Guards whose deny is registered for a dispatched caller only; the EM's call is a log.
SUBAGENT_DENY_CLASS: Dict[str, str] = {
    "guard-subagent-heavy-ue-launch": BOX_DEGRADING,
    "block-subagent-destructive-action": BOX_DEGRADING,
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
