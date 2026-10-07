"""Blinded falsifier-integrity review inputs and agent specs for an emitted inventory script.

Invariants: a review prompt carries only the six permitted reviewer fields
(criterion, how, baseline_output, expected_when_true, baseline_ref,
can_report_red_report) and never a plan path or plan-context text; labels are
ordinals, not plan stems; nothing here spawns a subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit.predispatch import AgentSpec
from coordinator_core.ops.dispatch_emit.spine_read import load_frontmatter_doc
from coordinator_core.ops.review_mint.execute_review import resolve_operative_criterion

REVIEW_PHASE_TITLE = "Falsifier integrity"
REVIEWER_AGENT_TYPE = "coordinator:falsifier-integrity-reviewer"
REVIEWER_AGENT_MODEL = "sonnet"

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_INSTRUMENT_REL = "coordinator/bin/instrument-can-report-red.py"
_PY_TOKEN = re.compile(r"[\w./\\-]+\.(?:py|sh)\b")


@dataclass(frozen=True)
class ReviewInput:
    plan_path: str
    criterion: str
    how: str
    baseline_output: Optional[str]
    expected_when_true: str
    baseline_ref: Optional[str]
    report_path: Optional[str]
    report_json: Optional[str]


def _read_plan(plan_path: str, repo_root: Path) -> str:
    return (repo_root / plan_path).read_text(encoding="utf-8")


def _falsifier(plan_text: str) -> Optional[dict]:
    from coordinator_core.ops.dispatch_emit import emit

    fals = emit._prime_exit_criterion_falsifier(plan_text)
    return fals if isinstance(fals, dict) else None


def _baseline_ref(plan_text: str) -> Optional[str]:
    """``falsifier.baseline_ref``, which ``emit``'s falsifier reader drops."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        doc = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return None
    block = doc.get("prime_exit_criterion") if isinstance(doc, dict) else None
    fals = block.get("falsifier") if isinstance(block, dict) else None
    ref = fals.get("baseline_ref") if isinstance(fals, dict) else None
    return str(ref).strip() or None if ref is not None else None


def plans_with_falsifier(plan_paths: Iterable[str], *, repo_root: Path) -> frozenset[str]:
    """Plan paths whose frontmatter carries ``prime_exit_criterion.falsifier``."""
    return frozenset(
        p for p in plan_paths if _falsifier(_read_plan(p, repo_root)) is not None
    )


def _instrument_path(how: str, repo_root: Path) -> Optional[str]:
    """Repo-relative POSIX path of the first existing ``.py`` or ``.sh`` file named in ``how``."""
    root = repo_root.resolve()
    for token in _PY_TOKEN.findall(how):
        rel = token.replace("\\", "/")
        if rel.startswith("/"):
            continue
        candidate = (root / rel).resolve()
        if candidate.is_file() and root in candidate.parents:
            return candidate.relative_to(root).as_posix()
    return None


def _load_verdict_reaches_exit():
    path = _ENGINE_ROOT / _INSTRUMENT_REL
    spec = importlib.util.spec_from_file_location("_instrument_can_report_red", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verdict_reaches_exit


def review_inputs(
    plan_paths: Iterable[str], *, repo_root: Path, report_dir: Path
) -> list[ReviewInput]:
    """One ReviewInput per falsifier-carrying plan; the caller writes ``report_json``."""
    from coordinator_core.ops.dispatch_emit import emit

    out: list[ReviewInput] = []
    verdict = None
    for plan_path in plan_paths:
        text = _read_plan(plan_path, repo_root)
        fals = _falsifier(text)
        if fals is None:
            continue
        instrument = _instrument_path(fals["how"], repo_root)
        report_path: Optional[str] = None
        report_json: Optional[str] = None
        if instrument is not None:
            if verdict is None:
                verdict = _load_verdict_reaches_exit()
            source = (repo_root / instrument).read_text(encoding="utf-8")
            # Indented, not one line: a dispatched reader reads this file with a line-capped Read.
            report_json = json.dumps(verdict(source, instrument), indent=2, sort_keys=True) + "\n"
            report_path = (report_dir / f"{Path(plan_path).stem}.json").as_posix()
        out.append(
            ReviewInput(
                plan_path=plan_path,
                criterion=getattr(resolve_operative_criterion(text, repo_root), "statement", ""),
                how=fals["how"],
                baseline_output=fals.get("baseline_output"),
                expected_when_true=fals["expected_when_true"],
                baseline_ref=_baseline_ref(text),
                report_path=report_path,
                report_json=report_json,
            )
        )
    return out


def _prompt(inp: ReviewInput) -> str:
    lines = [
        f"criterion: {inp.criterion}",
        f"how: {inp.how}",
        f"baseline_output: {inp.baseline_output if inp.baseline_output is not None else '(none)'}",
        f"expected_when_true: {inp.expected_when_true}",
        f"baseline_ref: {inp.baseline_ref if inp.baseline_ref is not None else '(none)'}",
    ]
    if inp.report_path is None:
        lines.append("can_report_red_report: no code to walk")
    else:
        lines.append(f"can_report_red_report: {inp.report_path}")
    return "\n".join(lines)


def review_specs(inputs: Sequence[ReviewInput]) -> list[AgentSpec]:
    """One blinded reviewer AgentSpec per input, labelled ``falsifier-integrity:<ordinal>``."""
    return [
        AgentSpec(
            key=inp.plan_path,
            label=f"falsifier-integrity:{n}",
            phase=REVIEW_PHASE_TITLE,
            agent_type=REVIEWER_AGENT_TYPE,
            model=REVIEWER_AGENT_MODEL,
            prompt=_prompt(inp),
            schema="falsifier_integrity_result",
        )
        for n, inp in enumerate(inputs, start=1)
    ]
