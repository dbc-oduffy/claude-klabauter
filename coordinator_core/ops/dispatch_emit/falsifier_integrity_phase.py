"""Blinded falsifier-integrity review inputs and agent specs for an emitted inventory script.

Invariants: a review prompt carries only the six permitted reviewer fields
(criterion, how, baseline_output, expected_when_true, baseline_ref,
can_report_red_report) and never a plan path or plan-context text; labels are
ordinals, not plan stems; nothing here spawns a subprocess.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit.predispatch import CACHED_SCHEMA, AgentSpec
from coordinator_core.ops.dispatch_emit.spine_read import load_frontmatter_doc
from coordinator_core.ops.review_mint.execute_review import resolve_operative_criterion

REVIEW_PHASE_TITLE = "Falsifier integrity"
REVIEWER_AGENT_TYPE = "coordinator:falsifier-integrity-reviewer"


def reviewer_agent_model() -> str:
    """The reviewer's model from ``emit``'s agent table; imported lazily because
    ``emit`` imports this module at load."""
    from coordinator_core.ops.dispatch_emit.emit import resolve_agent_model

    return resolve_agent_model(REVIEWER_AGENT_TYPE)


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
    falsifier_sha: str = ""
    plan_sha: str = ""
    cache_path: Optional[str] = None
    cached: Optional[dict] = None


CACHE_DIR_NAME = ".can-report-red"
_CACHEABLE_VERDICTS = ("SOUND", "BROKEN")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reviewer_rubric_path() -> Optional[Path]:
    """The reviewer agent file the harness loads, under the content root; None when absent."""
    from coordinator_core.content_root import read_content_root

    root = read_content_root()
    if not root:
        return None
    name = REVIEWER_AGENT_TYPE.split(":", 1)[-1] + ".md"
    for sub in ("agents", "coordinator/agents"):
        candidate = Path(root) / sub / name
        if candidate.is_file():
            return candidate
    return None


def _reviewer_rubric_identity() -> str:
    """sha256 of the rubric text with CRLF folded to LF; ``""`` when unresolved or unreadable.
    Read per call, never memoised: a rubric edit under a warm engine must miss."""
    path = _reviewer_rubric_path()
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return _sha(text.replace("\r\n", "\n"))


def _falsifier_sha(
    fields: dict,
    report_json: Optional[str],
    instrument_source: Optional[str],
    rubric_identity: str,
) -> str:
    """Covers every reviewer input, the instrument report, the instrument source (the reviewer
    can read it and the report omits it), the reviewer model and the reviewer rubric, so a
    changed instrument, model tier or rubric misses instead of replaying a stale verdict."""
    return _sha(
        json.dumps(
            [fields, report_json, instrument_source, reviewer_agent_model(), rubric_identity],
            sort_keys=True,
        )
    )


def _read_cached(repo_root: Path, cache_path: str, falsifier_sha: str, plan_sha: str) -> Optional[dict]:
    """The stored ``{verdict, tells}`` when both shas match; any unreadable or malformed
    file is a miss. In-process read, no spawn."""
    try:
        doc = json.loads((repo_root / cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    tells = doc.get("tells")
    if (
        doc.get("falsifier_sha") != falsifier_sha
        or doc.get("plan_sha") != plan_sha
        or doc.get("verdict") not in _CACHEABLE_VERDICTS
        or not isinstance(tells, list)
        or not all(isinstance(t, str) for t in tells)
    ):
        return None
    return {"verdict": doc["verdict"], "tells": tells}


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


class OracleFalsifierArgMismatch(ValueError):
    """The acceptance oracle and ``falsifier.how`` invoke one instrument with different arguments."""


_ACCEPTANCE_HEADING = re.compile(r"^##\s+Acceptance Criteria\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s", re.MULTILINE)
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _invocation_args(command: str, instrument: str) -> Optional[frozenset[str]]:
    """Arguments following ``instrument`` in ``command`` (basename match); None when absent."""
    tokens = command.replace("\\", "/").split()
    for i, tok in enumerate(tokens):
        if tok.strip("\"'") == instrument or tok.strip("\"'").endswith("/" + instrument):
            return frozenset(tokens[i + 1 :])
    return None


def oracle_falsifier_arg_mismatch(plan_text: str) -> Optional[str]:
    """A refusal message when a backticked command in ``## Acceptance Criteria`` runs an
    instrument that ``falsifier.how`` also runs, under a different argument set; else None.
    A command the falsifier never names is not compared."""
    fals = _falsifier(plan_text)
    head = _ACCEPTANCE_HEADING.search(plan_text)
    if fals is None or head is None:
        return None
    body = plan_text[head.end() :]
    nxt = _NEXT_HEADING.search(body)
    section = body[: nxt.start()] if nxt else body
    how = fals["how"]
    for token in dict.fromkeys(_PY_TOKEN.findall(how)):
        rel = token.replace("\\", "/")
        falsifier_args = _invocation_args(how, rel)
        if falsifier_args is None:
            continue
        for command in _BACKTICKED.findall(section):
            oracle_args = _invocation_args(command, rel)
            if oracle_args is not None and oracle_args != falsifier_args:
                return (
                    f"acceptance oracle and falsifier.how run {rel} with different arguments: "
                    f"oracle {sorted(oracle_args)} vs falsifier {sorted(falsifier_args)}"
                )
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
    rubric_identity = _reviewer_rubric_identity()
    for plan_path in plan_paths:
        text = _read_plan(plan_path, repo_root)
        fals = _falsifier(text)
        if fals is None:
            continue
        mismatch = oracle_falsifier_arg_mismatch(text)
        if mismatch is not None:
            raise OracleFalsifierArgMismatch(f"{plan_path}: {mismatch}")
        instrument =_instrument_path(fals["how"], repo_root)
        report_path: Optional[str] = None
        report_json: Optional[str] = None
        source: Optional[str] = None
        if instrument is not None:
            if verdict is None:
                verdict = _load_verdict_reaches_exit()
            source = (repo_root / instrument).read_text(encoding="utf-8")
            # Indented, not one line: a dispatched reader reads this file with a line-capped Read.
            report_json = json.dumps(verdict(source, instrument), indent=2, sort_keys=True) + "\n"
            report_path = (report_dir / f"{Path(plan_path).stem}.json").as_posix()
        criterion = getattr(resolve_operative_criterion(text, repo_root), "statement", "")
        baseline_ref = _baseline_ref(text)
        falsifier_sha = _falsifier_sha(
            {
                "criterion": criterion,
                "how": fals["how"],
                "baseline_output": fals.get("baseline_output"),
                "expected_when_true": fals["expected_when_true"],
                "baseline_ref": baseline_ref,
            },
            report_json,
            source,
            rubric_identity,
        )
        plan_sha = _sha(text)
        cache_path: Optional[str] = None
        cached: Optional[dict] = None
        if rubric_identity:
            cache_path = (report_dir.parent / CACHE_DIR_NAME / f"{Path(plan_path).stem}.verdict.json").as_posix()
            cached = _read_cached(repo_root, cache_path, falsifier_sha, plan_sha)
        if cached is not None:
            # A hit needs no can-report-red report: nothing will read it.
            report_path = report_json = None
        out.append(
            ReviewInput(
                plan_path=plan_path,
                criterion=criterion,
                how=fals["how"],
                baseline_output=fals.get("baseline_output"),
                expected_when_true=fals["expected_when_true"],
                baseline_ref=baseline_ref,
                report_path=report_path,
                report_json=report_json,
                falsifier_sha=falsifier_sha,
                plan_sha=plan_sha,
                cache_path=cache_path,
                cached=cached,
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
    return [_spec(n, inp) for n, inp in enumerate(inputs, start=1)]


def _spec(n: int, inp: ReviewInput) -> AgentSpec:
    common = dict(
        key=inp.plan_path,
        label=f"falsifier-integrity:{n}",
        phase=REVIEW_PHASE_TITLE,
        agent_type=REVIEWER_AGENT_TYPE,
        model=reviewer_agent_model(),
    )
    if inp.cached is not None:
        # Same shape the reviewer's structured output folds from (`tells[].status`), so a
        # hit flows through the script's fold exactly as a fresh verdict does.
        result = {
            "verdict": inp.cached["verdict"],
            "tells": [{"tell": t, "status": "FIRED"} for t in inp.cached["tells"]],
        }
        return AgentSpec(**common, prompt=json.dumps(result), schema=CACHED_SCHEMA)
    cacheable = inp.cache_path is not None and inp.falsifier_sha and inp.plan_sha
    return AgentSpec(
        **common,
        prompt=_prompt(inp),
        schema="falsifier_integrity_result",
        cache_path=inp.cache_path if cacheable else None,
        cache_key=(inp.falsifier_sha, inp.plan_sha) if cacheable else None,
    )
