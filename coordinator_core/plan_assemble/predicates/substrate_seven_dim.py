"""
coordinator_core.plan_assemble.predicates.substrate_seven_dim — Layer 0
Branch B (a): problem-set presence, `scope_mode` surfacing, and four of the
seven-dimension (`:90`) rows, into `gates.substrate.*`.

Purpose: `/plan`'s seven-dimension checklist asks whether a plan duplicates
prior art, fabricates a citation, skips official docs, or cites no reference
implementation — four facts that already have a producer somewhere on disk
(a `prior-art-checker`/`docs-checker` dispatch sidecar, `doc_content_verify`'s
citation scan). This module reads and surfaces those facts; it never
reimplements the checks that produce them. It also carries `:72` (problem-set
presence), `:73` (the trusted `scope_mode` leg only), `:94` (the M-band
premise-gate evidence row, read off `coordinator_core.sizing_assemble.
_PREMISE_DETENT_TSHIRTS` rather than a hardcoded tuple), and `:96`/`:100`
(the trampoline verdict-citation and DEC-4 signal rows).

`:90(1)`/`:90(4)` CONSUME the deterministic plan-sidecar paths
`<machinery_root>/plan-sidecars/<plan-stem>.prior-art-check.md` and
`<machinery_root>/plan-sidecars/<plan-stem>.docs-check.md` (resolved via
`coordinator_core.session.machinery_paths.plan_sidecars_dir`) — the
plan-derivable `report_sidecar` home `coordinator_core.subagent_sandbox.
provision_report._PLAN_DERIVABLE_LENS` resolves `coordinator:prior-art-checker`
and `coordinator:docs-checker` dispatches to (spec § 2.7). This module never
dispatches either checker and never re-derives their verdict — file
presence at that deterministic path IS the signal.

`:90(2)` calls `coordinator_core.ops.doc_content_verify.verify_doc` directly
over the plan body, supplying `repo_exists` and `doc_relative_checker` (not
`sibling_checkers`/`read_target_text`, which are out of scope for a
fabrication check over plan-body text — see `seven_dim_no_fabrication`'s own
docstring): a plan with zero `Finding(reason="absent")` citations has
`no_fabrication = True`. This module composes over that function; it does
not fork or re-derive its extraction/resolution logic.

Negative-spec:
  - Does NOT reimplement prior-art checking or docs checking — `:90(1)` and
    `:90(4)` read sidecar presence only, never the dispatch content itself
    beyond the one `verdict`/`status` field named per row.
  - Does NOT fork `doc_content_verify`'s extraction/exclusion/resolution
    ladder. `:90(2)` calls `verify_doc` directly against the plan body text.
  - Does NOT hardcode the premise-detent tuple. `:94` imports
    `coordinator_core.sizing_assemble._PREMISE_DETENT_TSHIRTS` — the
    constant that actually governs whether the `premise_unproven`/
    `premise_not_applicable` detent fires — and reads it at call time; it
    never widens it and never substitutes `_LARGE_TSHIRTS` (a distinct
    constant gating the unrelated shape-route condition
    `resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear`). The contract
    row's original framing assumed the detent never fired for "M"; that
    premise went stale once `_PREMISE_DETENT_TSHIRTS` shipped with "M"
    included. The evidence is that constant's own declaration and the
    rationale in `coordinator_core/sizing_assemble/__init__.py`'s module
    docstring — no memo is cited here because none exists on this point;
    an earlier draft of this docstring named one that was never written.
    This module deliberately diverges from the contract row's stale
    framing rather than reproducing it — reading `_LARGE_TSHIRTS` here
    would have been a false "M band uncovered" signal for every M-sized
    plan.
  - Does NOT compute `:73`'s inferred-PM-intent arm (the doubt-check triad's
    U-classified leg). Only the trusted frontmatter `scope_mode:` read is
    in this module's scope.
  - Does NOT decide `:90(2)`'s absent-vs-cross-repo distinction differently
    than `doc_content_verify` already does — `resolves-cross-repo` is not a
    fabrication, exactly as that module's own negative-spec states.
  - Does NOT touch `residue.py` — envelope wiring is C13's exclusive write.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C3
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.doc_content_verify import _doc_relative_checker, verify_doc
from coordinator_core.session.machinery_paths import plan_sidecars_dir as _plan_sidecars_dir
from coordinator_core import sizing_assemble as _sizing_assemble
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined

_PROBLEM_SET_DIR = "docs/problems"
_SPIKE_VERDICT_DIR = "docs/research/spike-verdicts"


def _plan_sidecar_path(context: PredicateContext, lens: str) -> Path | None:
    """Resolve `<machinery_root>/plan-sidecars/<plan-stem>.<lens>.md` (spec §
    2.7 / `provision_report.py`'s `_PLAN_DERIVABLE_LENS`) for `context.plan_path`,
    via `machinery_paths.plan_sidecars_dir` — the same accessor
    `provision_report` writes through and `harvest_exit_interviews` reads
    through, so a future relocation is one accessor edit, not a respelled
    literal here too.

    Mirrors `provision_report._resolve_plan_sidecar_stem` — the plan's own
    filename stem, not a re-derivation of any other identifier. Returns
    `None` if `context.plan_path` is absent (no plan supplied)."""
    if context.plan_path is None:
        return None
    stem = context.plan_path.stem
    return Path(_plan_sidecars_dir(str(context.repo_root))) / f"{stem}.{lens}.md"


def problem_set(context: PredicateContext) -> dict[str, Any]:
    if context.plan_frontmatter is None:
        return {"present": undetermined("no plan supplied (--plan not given)"), "path": None}

    raw = context.plan_frontmatter.get("problem_set")
    if not raw:
        return {"present": False, "path": None}

    path = None
    if isinstance(raw, str) and raw != "inline":
        candidate = context.repo_root / _PROBLEM_SET_DIR / raw
        if candidate.exists():
            path = candidate.relative_to(context.repo_root).as_posix()

    return {"present": True, "path": path}


def scope_mode(context: PredicateContext) -> dict[str, Any]:
    if context.plan_frontmatter is None:
        return {"value": undetermined("no plan supplied (--plan not given)")}
    value = context.plan_frontmatter.get("scope_mode")
    if value is None:
        return {"value": undetermined("plan frontmatter carries no scope_mode key")}
    return {"value": value}


def seven_dim_no_duplicate(context: PredicateContext) -> dict[str, Any]:
    """`:90(1)` — `gates.substrate.seven_dim.no_duplicate`. CONSUMES the
    `prior-art-checker` dispatch sidecar's presence at its deterministic
    plan-sidecar path; never reimplements prior-art checking."""
    sidecar_path = _plan_sidecar_path(context, "prior-art-check")
    if sidecar_path is None:
        return undetermined("no plan supplied (--plan not given); no plan-sidecar stem to derive")
    if not sidecar_path.exists():
        return undetermined(
            f"no prior-art-checker dispatch sidecar found at "
            f"{sidecar_path.relative_to(context.repo_root).as_posix()}"
        )
    return True


def seven_dim_no_fabrication(context: PredicateContext) -> dict[str, Any]:
    if context.plan_path is None or context.plan_body is None:
        return undetermined("no plan supplied (--plan not given), or plan carried no body")

    doc_rel_path = _safe_relative(context.plan_path, context.repo_root)

    def _repo_exists(token: str) -> bool:
        try:
            return (context.repo_root / token).exists()
        except OSError:
            return False

    findings = verify_doc(
        doc_rel_path,
        context.plan_body,
        repo_exists=_repo_exists,
        doc_relative_checker=_doc_relative_checker(context.repo_root, doc_rel_path),
    )
    absent = [f for f in findings if f.reason == "absent"]
    return {
        "no_fabrication": len(absent) == 0,
        "absent_citations": [
            {"line": f.line, "token": f.token} for f in absent
        ],
    }


def seven_dim_official_docs_read(context: PredicateContext) -> dict[str, Any]:
    """`:90(4)` — `gates.substrate.seven_dim.official_docs_read`. CONSUMES
    the `docs-checker` dispatch sidecar's presence at its deterministic
    plan-sidecar path; never reimplements docs checking."""
    sidecar_path = _plan_sidecar_path(context, "docs-check")
    if sidecar_path is None:
        return undetermined("no plan supplied (--plan not given); no plan-sidecar stem to derive")
    if not sidecar_path.exists():
        return undetermined(
            f"no docs-checker dispatch sidecar found at "
            f"{sidecar_path.relative_to(context.repo_root).as_posix()}"
        )
    return True


def seven_dim_reference_impl_seen(context: PredicateContext) -> dict[str, Any]:
    if context.plan_body is None:
        return undetermined("no plan supplied (--plan not given), or plan carried no body")
    return _CITATION_TOKEN_RE.search(context.plan_body) is not None


def premise_gate(context: PredicateContext) -> dict[str, Any]:
    """`:94` — `gates.substrate.premise_gate.m_band_uncovered`. Compares the
    sizing object's `estimate.tshirt` against `coordinator_core.
    sizing_assemble._PREMISE_DETENT_TSHIRTS`, READ (never hardcoded, never
    widened) from that module — the constant that actually governs whether
    `sizing_assemble`'s `premise_unproven`/`premise_not_applicable` detent
    fires. `_PREMISE_DETENT_TSHIRTS` is `("M", "L", "XL", "XXL")`: the
    detent DOES fire for "M". `m_band_uncovered` is therefore `False` for
    every tshirt in that tuple, including "M" — an M-sized plan is covered
    by the detent, not a gap.

    The contract row this function implements was originally framed against
    `_LARGE_TSHIRTS` (`("L", "XL", "XXL")`, no "M"), on the premise that the
    premise-detent "never fires" for M. That premise went stale when
    `_PREMISE_DETENT_TSHIRTS` was introduced as a sibling constant carrying
    "M" — see its own declaration and the rationale in
    `coordinator_core/sizing_assemble/__init__.py`'s module docstring, which
    is the evidence for this divergence. The row is therefore VACUOUS as
    specified: the gap it exists to flag is closed, so `m_band_uncovered` is
    `False` for every t-shirt, not just for the ones inside the detent set.

    Negative-spec — two wrong shapes, both of which read as plausible:
      - Reading `_LARGE_TSHIRTS` here reintroduces the stale premise as a
        false "M band uncovered" on every M-sized plan.
      - Computing `tshirt not in _PREMISE_DETENT_TSHIRTS` answers a
        DIFFERENT question ("is this size outside the detent set"), and
        reports `True` for an S- or XS-sized plan — a field named
        `m_band_uncovered` claiming a gap in the M band for a plan that is
        not M. The evidence for what the detent covers is `tshirt`, which
        this function emits alongside; the boolean answers only its own
        question.

    `_PREMISE_DETENT_TSHIRTS` and `_LARGE_TSHIRTS` are deliberately distinct
    constants — `_LARGE_TSHIRTS` gates the unrelated shape-route condition
    `resized_tshirt in _LARGE_TSHIRTS and jtbd_unclear`, so widening it to
    include "M" would silently reroute an M-sized `jtbd_unclear` sizing from
    `plan` to `shape`. Never merge or widen either one; this function only
    reads `_PREMISE_DETENT_TSHIRTS` live, to keep the vacuity claim above
    honest if that constant ever changes."""
    if context.sizing_frontmatter is None:
        return {
            "m_band_uncovered": undetermined(
                "no sizing object supplied (--sizing-object not given)"
            ),
            "tshirt": None,
        }
    estimate = context.sizing_frontmatter.get("estimate")
    tshirt = estimate.get("tshirt") if isinstance(estimate, dict) else None
    if tshirt is None:
        return {
            "m_band_uncovered": undetermined("sizing object carries no estimate.tshirt"),
            "tshirt": None,
        }
    return {
        "m_band_uncovered": "M" not in _sizing_assemble._PREMISE_DETENT_TSHIRTS,
        "tshirt": tshirt,
    }


def trampoline_verdict(context: PredicateContext) -> dict[str, Any]:
    if context.sizing_frontmatter is None:
        return {
            "verdict_cited": undetermined(
                "no sizing object supplied (--sizing-object not given)"
            ),
            "verdict_path": None,
            "verdict": None,
        }
    premise = context.sizing_frontmatter.get("premise")
    spike_verdict = premise.get("spike_verdict") if isinstance(premise, dict) else None
    if not spike_verdict:
        return {"verdict_cited": False, "verdict_path": None, "verdict": None}

    record_path = context.repo_root / spike_verdict
    if not record_path.exists():
        return {
            "verdict_cited": False,
            "verdict_path": spike_verdict,
            "verdict": None,
        }

    verdict_value = None
    try:
        text = record_path.read_text(encoding="utf-8")
        parsed = parse_frontmatter(text)
        frontmatter = parsed.get("frontmatter") or {}
        verdict_value = frontmatter.get("verdict")
    except OSError:
        pass

    return {"verdict_cited": True, "verdict_path": spike_verdict, "verdict": verdict_value}


def trampoline_dec4_signal(context: PredicateContext) -> Any:
    if "trampoline" not in context.caller_flags:
        return undetermined("caller_flags carries no 'trampoline' key (DEC-4 signal not supplied)")
    return bool(context.caller_flags["trampoline"])


_CITATION_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+:\d+")


def _safe_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def compute(context: PredicateContext) -> dict[str, Any]:
    return {
        "problem_set": problem_set(context),
        "scope_mode": scope_mode(context),
        "seven_dim": {
            "no_duplicate": seven_dim_no_duplicate(context),
            "no_fabrication": seven_dim_no_fabrication(context),
            "official_docs_read": seven_dim_official_docs_read(context),
            "reference_impl_seen": seven_dim_reference_impl_seen(context),
        },
        "premise_gate": premise_gate(context),
        "trampoline": {
            "verdict_cited": trampoline_verdict(context)["verdict_cited"],
            "verdict_path": trampoline_verdict(context)["verdict_path"],
            "dec4_signal": trampoline_dec4_signal(context),
        },
    }


__all__ = [
    "problem_set",
    "scope_mode",
    "seven_dim_no_duplicate",
    "seven_dim_no_fabrication",
    "seven_dim_official_docs_read",
    "seven_dim_reference_impl_seen",
    "premise_gate",
    "trampoline_verdict",
    "trampoline_dec4_signal",
    "compute",
]
