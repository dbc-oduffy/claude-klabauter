"""dispatch.ask_stage brief blocks: the PM-intent, relayed-ask, revisions and EM-diagnosis sections, the sizing
and test-scope readers that feed them, and the plan scaffold-marker scan. Lowest module of the ask_stage split:
imports nothing from its siblings, so `AskStageError` and the `_write`/`_rel` helpers live here.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops._brief_premise import check_brief_claims, render_contradicted
from coordinator_core.ops.dispatch_emit import requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR, RELAYED_ASK_HEADING
from coordinator_core.ops.dispatch_emit.pathspec import NoTestTargetError, NoWritesDeclaredError, terminal_test_scope
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing
from coordinator_core.ops.sizing_intake_contract import read_em_brief

PM_INTENT_HEADING = "## PM intent (verbatim)"


_HARNESS_RELAY_MARKERS = (
    "[Workflow harness",
    "was relayed from the driving session",
    "<system-reminder>",
)


def _is_harness_relay(text: str) -> bool:
    """True when `text` is a harness-composed resume relay, not the PM's words."""
    head = text.lstrip()
    return any(head.startswith(m) for m in _HARNESS_RELAY_MARKERS) or _HARNESS_RELAY_MARKERS[1] in head[:400]


def pm_intent_block(sizing: Optional[Mapping], ask_text: Optional[str]) -> str:
    """The brief's head: the PM's own words (sizing `pm_verbatims`, else a PM-verbatim `intent`,
    plus the ask as submitted), the exit criterion, and the precedence rule. Never the EM's
    paraphrase: an `em-elaborated` intent is left out. Empty when no PM words are on record."""
    sizing = sizing or {}
    quotes = requirements_ledger.pm_words(sizing)
    if ask_text and ask_text.strip() and not _is_harness_relay(ask_text) and ask_text.strip() not in quotes:
        quotes.append(ask_text.strip())
    if not quotes:
        return ""
    lines = [PM_INTENT_HEADING, ""]
    for q in quotes:
        lines.extend("> " + ln if ln else ">" for ln in q.splitlines())
        lines.append("")
    criterion = sizing.get("exit_criterion")
    statement = criterion.get("statement") if isinstance(criterion, Mapping) else None
    if isinstance(statement, str) and statement.strip():
        lines.extend([f"Exit criterion: {statement.strip()}", ""])
    lines.append(
        "These words are the outcome you are held to; design the change that makes each of them "
        "literally true. Where the brief below, a plan, a memo or in-repo doctrine reads narrower "
        "than these words or against them, these words win: keep the ask and name the conflict in "
        "your report."
    )
    return "\n".join(lines) + "\n"


def relayed_ask_block(relayed_text: Optional[str]) -> str:
    """The brief's relayed-ask section, below the PM-intent block and never inside it: an EM's
    relay of the ask, stated as not the PM's words. Empty when nothing was relayed."""
    if not relayed_text or not relayed_text.strip():
        return ""
    lines = [RELAYED_ASK_HEADING, ""]
    for ln in relayed_text.strip().splitlines():
        lines.append("> " + ln if ln else ">")
    lines.extend([
        "",
        "An agent relayed this ask; it is not the PM's own wording. Serve it, and where the PM's "
        "verbatim words above (or in the sizing) read differently, the PM's words outrank it.",
    ])
    return "\n".join(lines) + "\n"


REVISIONS_HEADING = "## Revisions sent while the run was in flight"


def revisions_block(root: Path, run_id: str) -> str:
    """The brief's section of every register row a `workflow.revise` added to the run, in arrival
    order: the ledger judges each, so an executor not told of one can only fail it. Empty when none."""
    try:
        ledger = requirements_ledger.read_ledger(root, requirements_ledger.run_dir(root, run_id))
    except requirements_ledger.LedgerRefused:
        return ""
    items = [i for i in ledger.items if i.origin == requirements_ledger.ORIGIN_REVISION] if ledger else []
    if not items:
        return ""
    lines = [REVISIONS_HEADING, ""]
    for item in items:
        tag = ", ".join(p for p in (item.at, f"from {item.sender}" if item.sender else "") if p)
        lines.append(f"{item.id} ({tag}):" if tag else f"{item.id}:")
        lines.extend("> " + ln if ln else ">" for ln in item.text.strip().splitlines())
        lines.append("")
    lines.append(
        "These are requirements of this run, relayed by an EM or peer, not the PM's words. Deliver "
        "each one your row serves; a later revision governs over an earlier item it changes."
    )
    return "\n".join(lines) + "\n"


def em_brief_block(sizing: Optional[Mapping], root: Optional[Path] = None) -> str:
    """The brief's EM-diagnosis section (the sizing's `em_analysis.em_brief`), marked as not the
    PM's words and placed below the PM-intent and relayed-ask blocks. Empty when none is recorded.
    With ``root``, the brief's tree-contradicted claims follow as CONTRADICTED lines."""
    brief = read_em_brief(sizing) if isinstance(sizing, Mapping) else None
    if not brief:
        return ""
    block = f"## EM diagnosis (not the PM's words)\n\n{brief}\n"
    if root is not None:
        flagged = render_contradicted(check_brief_claims(brief, root))
        if flagged:
            block += f"\n{flagged}\n"
    return block


def _run_sizing_rel(sizing_rel: Optional[str], plan_text: str) -> Optional[str]:
    if sizing_rel:
        return sizing_rel
    split = split_frontmatter(plan_text)
    return read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None


def _run_sizing(root: Path, rel: Optional[str]) -> Optional[dict]:
    if not rel:
        return None
    try:
        return load_sizing(root, rel)
    except (SizingFireRefused, OSError, ValueError):
        return None


DELIVERY_NAMED_NOTE = re.compile(r"DELIVERY[^/]*\.md", re.IGNORECASE)
_DOC_TARGET_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#.*)$")


def doc_writes(run_id: str, sizing: Optional[Mapping]) -> tuple[str, ...]:
    """The docs stage's declared write set: the run's delivery note, then every path the sizing
    names in `doc_consumers` and `ask_conflicts` (a `:<line>` or `#<anchor>` suffix stripped),
    plus any `DELIVERY*.md` the sizing's `writes` names (the delivery note the ask asked for, at
    its named path). A sizing or plan path is never one -- those stay process records."""
    sizing = sizing or {}
    raw = [p for p in sizing.get("doc_consumers") or [] if isinstance(p, str)]
    raw += [
        c.get("path") for c in sizing.get("ask_conflicts") or []
        if isinstance(c, Mapping) and isinstance(c.get("path"), str)
    ]
    raw += [
        w for w in sizing.get("writes") or []
        if isinstance(w, str) and DELIVERY_NAMED_NOTE.match(PurePosixPath(w.strip()).name)
    ]
    from coordinator_core.session.record_homes import home_dir

    process = (PurePosixPath(home_dir(".", "sizings")).as_posix() + "/", "docs/plans/")
    out = [f"{DELIVERY_NOTE_DIR}/{run_id}.md"]
    for entry in raw:
        rel = PurePosixPath(_DOC_TARGET_SUFFIX.sub("", entry.strip()).replace("\\", "/")).as_posix()
        if (
            rel and rel != "." and not PurePosixPath(rel).is_absolute() and ".." not in PurePosixPath(rel).parts
            and not rel.startswith(process) and rel not in out
        ):
            out.append(rel)
    return tuple(out)


def _reviewable(declared: Sequence[str]) -> tuple[str, ...]:
    """The declared paths a reviewer can read a product diff for. Bookkeeping and scratch writes
    (audits, state, plans) freeze no reviewable diff, so a run declaring only those has an empty
    list and the composed script skips the review wave instead of halting at review prep."""
    from coordinator_core.ops.review_stamp import PRODUCT_FILE_EXCLUDED_PREFIXES

    skip = (*PRODUCT_FILE_EXCLUDED_PREFIXES, "scratch/")
    return tuple(p for p in declared if not p.replace("\\", "/").removeprefix("./").startswith(skip))


def _test_scope(waves, root: Path) -> tuple[tuple[str, ...], Optional[dict]]:
    """(test targets, None), or ((), the typed no-target record) when the locator maps no
    testable write to a runnable target -- e.g. a .ts-only spine in a repo whose tests it cannot
    see. Never a crash and never a silent pass: the run reads its tests as degraded."""
    try:
        return tuple(terminal_test_scope(waves, repo_root=root)), None
    except NoTestTargetError as exc:
        return (), {
            "reason": str(exc),
            "unmapped_paths": list(exc.unmapped_paths),
            "testable_omissions": list(exc.testable_omissions),
        }
    except NoWritesDeclaredError as exc:
        return (), {"reason": str(exc), "unmapped_paths": [], "testable_omissions": []}


def _wave_test_scopes(waves, root: Path) -> dict[str, tuple[str, ...]]:
    """Cumulative declared test scope per non-last wave, keyed by the wave number rows carry.
    A wave whose cumulative scope is empty has no key: nothing to wait on before its checkpoint."""
    out: dict[str, tuple[str, ...]] = {}
    for i in range(len(waves) - 1):
        scope = _test_scope(waves[: i + 1], root)[0]
        if scope:
            out[str(i)] = scope
    return out

# Mirrors DoE's coordinator/lib/plan_scaffold_markers.py; claude-klabauter never imports DoE's tree.
_SCAFFOLD_MARKERS = (
    ("PLACEHOLDER", re.compile(r"\bPLACEHOLDER\b")),
    ("<REPLACE:", re.compile(r"<REPLACE:")),
    ("path/to/file", re.compile(r"path/to/file")),
)


def scaffold_markers(text: str) -> dict[str, int]:
    """Marker -> first 1-based line carrying it; `#` YAML comment lines are template docs, skipped."""
    found: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            continue
        for name, rx in _SCAFFOLD_MARKERS:
            if name not in found and rx.search(line):
                found[name] = n
    return found


class AskStageError(ValueError):
    """The stage inputs name no stageable plan or sizing, or escape the run dir.

    `owed_em_ids` / `plan_path` are set when every row is EM-owned; the op reply carries them as fields."""

    def __init__(
        self,
        message: str,
        *,
        owed_em_ids: Optional[list] = None,
        plan_path: Optional[str] = None,
        gated_rows: Optional[list] = None,
    ):
        super().__init__(message)
        self.owed_em_ids = list(owed_em_ids) if owed_em_ids else None
        self.gated_rows = list(gated_rows) if gated_rows else None
        self.plan_path = plan_path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
