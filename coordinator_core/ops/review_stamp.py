"""
coordinator_core.ops.review_stamp — mint/check the plan's `review_stamp` record.

Purpose: DoE-claude docs/plans/2026-09-27-review-inside-execute-plan.md, row MK1.
Binds a plan to the ONE terminal commit that carries its execute-review wave's
verdicts, so `implemented` can never be reached with no code review recorded
against the reviewed tree.

  - `mint(plan_path, repo_root, build_test_path=...)`: resolves the terminal
    commit carrying an `Inline-Review: applies <stem>` trailer whose
    integration sidecar's `plan_id` matches this plan, reads the prep,
    integration and delivery sidecars it names, and writes `review_stamp:`
    into the plan frontmatter. Refuses (raises `MintRefusal`) on a FAIL
    delivery verdict, a missing/non-pass build/test record, any unresolved
    finding, any confinement violation, any foreign claim, or zero product
    files. Writes nothing else in the plan.
  - `mint(..., repair=True)` (CLI: `mint --repair`): EXPLICIT, never-default
    repair path for a run that landed after the review-integrator
    retirement (commit 3d44bfb122) but before step b' shipped
    `review_mint.wave_bookkeeping` -- its integration sidecar predates the
    fields `mint` reads (no `prep_sidecar`), or no sidecar resolves at all
    (a pre-fix `Inline-Review: applies None` trailer). Terminal-commit
    resolution is UNCHANGED (`_resolve_terminal_commit`, including its
    None-trailer/receipt-session fallback); repair only widens what happens
    once resolution comes up short:
      1. still tries the normal walk first;
      2. if it resolves a sidecar that lacks `prep_sidecar`, or resolves
         nothing at all, repair locates the run's session_id (the resolved
         sidecar's `lead_session_id`, else the plan's own
         `<stem>.workflow.mjs.emitted.json` receipt `session_id`), then its
         `.coordinator-local/subagent-share/<session_id>/` dir;
      3. within that dir it heuristically discovers a prep-shaped sidecar
         (carries `product_files` + `whole_diff_sidecars`) and this run's
         wave sidecars (reviewer-agent_type sidecars, excluding the prep
         sidecar, the resolved integration sidecar, and any `*.blocks.md`
         companion file), then calls
         `review_mint.wave_bookkeeping.bookkeep_wave` over them to build a
         fresh bookkeeping record in the same shape `mint` already reads;
      4. mints against that record exactly as it would a normal one -- the
         same refusal predicates apply unchanged (FAIL delivery, non-pass
         build/test, unresolved findings, confinement violations, foreign
         claims, zero product files), plus a repair-specific refusal when
         no prep sidecar or zero wave sidecars are discoverable.
    A run whose sidecar already carries `prep_sidecar` is unaffected by
    `--repair` -- it takes the normal path even with the flag set.
  - `check(plan_path, repo_root, supersession=...)`: returns `None` when the
    existing stamp is still valid, or a one-line refusal string. Tree
    equality and ancestry are always checked; the supersession check (a
    later commit touching a spine `scope:` path) runs only when asked —
    `/workstream-complete` (MK2) never re-runs it, since a shared tree can
    legitimately touch the same file in a later, unrelated plan.

Zero git spawns in `mint` beyond the one commit-trailer walk plus one `%T`
read. `check` makes at most three spawns, all argv-only:
  - `git log -1 --format=%T <terminal>` (tree equality)
  - `git merge-base --is-ancestor <terminal> HEAD` (ancestry)
  - `git log --format=%H <terminal>..HEAD -- <writes>` (supersession, only
    when `supersession=True`)

Negative-spec:
  - `--override-reason` does not reach this refusal anywhere it is wired
    (plan_status_transition._stamp_implemented, close_out_and_stamp) — the
    same standing as `_refuse_if_live_foreign_holder`.
  - Never restates the product-file exclusion rule anywhere else:
    `PRODUCT_FILE_EXCLUDED_PREFIXES` is its one home.

Spec backlink: docs/plans/2026-09-27-review-inside-execute-plan.md § Design,
row MK1.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    rebuild,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.win_portability import no_console_creationflags

_CREATIONFLAGS = no_console_creationflags()
_GIT_TIMEOUT_SECS = 30

#: The product-file rule's one home. A diff line touching only these prefixes
#: is bookkeeping, never product code — prep's `product_files` and the
#: delivery verifier both cite this constant, never restate it.
PRODUCT_FILE_EXCLUDED_PREFIXES = (
    "state/",
    "archive/",
    "tasks/",
    "cross-repo/",
    "docs/plans/",
    "docs/research/",
    ".coordinator-local/",
)

#: Bound on the commit-trailer walk `mint` runs looking for this plan's
#: terminal commit. A plan whose terminal sits further back than this is a
#: known miss — cheap direction, mirrors guard_terminal_review's own bound.
_COMMIT_WALK_BOUND = 500

_APPLIES_RE = re.compile(r"^applies\s+(\S+)")

_FIELD_SEP = "\x1f"
_MULTI_SEP = "\x1e"
_HEADER_SENTINEL = "\x02"


#: The ISO instant this op is published to claude-klabauter. A plan is
#: "subject" (must carry a valid review_stamp to reach `implemented`) only
#: when authorized/created strictly after this instant — see
#: `is_subject_plan`. DELETE-WHEN: never (this is a permanent historical
#: cutoff, not a rolling window).
_REVIEW_STAMP_CUTOFF = "2026-09-28T00:00:00Z"

#: The slate's own delivery is exempt — its plans predate the stamp op by
#: construction and cannot have minted one. DELETE-WHEN: this deliverable
#: closes (dlv-coordinator-claude-klabauter-restructure-to-beat-v-9a5ca9).
_SLATE_EXEMPT_DELIVERABLE = "dlv-coordinator-claude-klabauter-restructure-to-beat-v-9a5ca9"


def is_subject_plan(fm_data: Dict[str, Any]) -> bool:
    """True when `fm_data` (a parsed plan frontmatter mapping) must carry a
    valid `review_stamp` to reach `implemented` — approved strictly after
    `_REVIEW_STAMP_CUTOFF`, per the EM ruling in § Contract. The slate's own
    delivery is never subject."""
    if fm_data.get("deliverable_id") == _SLATE_EXEMPT_DELIVERABLE:
        return False
    auth_at = fm_data.get("execution_authorized_at")
    if isinstance(auth_at, str) and auth_at:
        return auth_at > _REVIEW_STAMP_CUTOFF
    created = fm_data.get("created")
    if created:
        return str(created) > _REVIEW_STAMP_CUTOFF[:10]
    return False


def subject_refusal(plan_path: Path, repo_root: Path, fm_text: str) -> Optional[str]:
    """The one `implemented` gate both stamping paths call: `None` when the plan
    is not subject or its stamp verifies (supersession on), else the refusal."""
    try:
        fm_data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm_data = {}
    if not (isinstance(fm_data, dict) and is_subject_plan(fm_data)):
        return None
    return check(plan_path, repo_root, supersession=True)


class MintRefusal(Exception):
    """Raised by `mint` when the stamp cannot be assembled. Message is the
    one-line refusal, guard-messaging register."""


class _GitUnavailable(Exception):
    pass


def _run_git(args: List[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitUnavailable(str(exc)) from exc
    if result.returncode != 0:
        raise _GitUnavailable(result.stderr.strip() or "git command failed")
    return result.stdout


def product_files(diff_path: Path) -> List[str]:
    """The product (non-bookkeeping) files named in a unified diff at
    `diff_path` — one home for the product-file rule (§ module docstring).
    Reads `+++ b/<path>` / `--- a/<path>` lines; a path under any
    `PRODUCT_FILE_EXCLUDED_PREFIXES` prefix is excluded."""
    if not diff_path.exists():
        return []
    text = diff_path.read_text(encoding="utf-8", errors="replace")
    files: List[str] = []
    seen = set()
    for line in text.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line.split("\t", 1)[0][6:]
            if path in ("dev/null",) or path in seen:
                continue
            seen.add(path)
            if any(path.startswith(prefix) for prefix in PRODUCT_FILE_EXCLUDED_PREFIXES):
                continue
            files.append(path)
    return files


def _load_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _find_sidecar_by_stem(repo_root: Path, stem: str) -> Optional[Path]:
    share_root = repo_root / ".coordinator-local" / "subagent-share"
    if not share_root.exists():
        return None
    matches = sorted(share_root.glob(f"**/{stem}.md"))
    return matches[0] if matches else None


def _read_plan_frontmatter(plan_path: Path):
    text = plan_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    split = split_frontmatter(text)
    if split is None:
        raise MintRefusal(f"review-stamp: no parseable YAML frontmatter in {plan_path}")
    return text, split


def _receipt_session_id(plan_path: Path) -> Optional[str]:
    """The `session_id` recorded in `<plan-stem>.workflow.mjs.emitted.json`,
    the emit receipt `archive_plans._FIRE_SCRIPT_SUFFIXES` names as living
    beside the plan (dispatch_emit/op.py's one producer). `None` on any
    absence/parse failure -- this is a best-effort fallback identity, never
    a hard requirement."""
    receipt_path = plan_path.with_name(plan_path.stem + ".workflow.mjs.emitted.json")
    if not receipt_path.exists():
        return None
    try:
        import json

        data = json.loads(receipt_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    session_id = data.get("session_id") if isinstance(data, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _resolve_terminal_commit(
    repo_root: Path, plan_id: str, plan_path: Optional[Path] = None, *, repair: bool = False
):
    """Newest-first walk of `_COMMIT_WALK_BOUND` commits, looking for an
    `Inline-Review: applies <stem>` trailer whose integration sidecar's
    frontmatter `plan_id` equals this plan's. One git spawn.

    Repair-path fallback (example-retrieval-repo cc6525cf0, plan pln-mcp-index-start-
    takes-a-verb-a-1fb99b): a commit already landed by the pre-fix engine
    carries a sidecar with NO `plan_id` at all (the bug this fallback
    exists to route around, § module docstring `mint`). When `plan_path` is
    supplied, a candidate sidecar missing `plan_id` is still accepted if its
    `lead_session_id` (or legacy `dispatched_by`) matches the `session_id`
    the plan's own `<stem>.workflow.mjs.emitted.json` emit receipt recorded
    -- the receipt is written by the SAME run that dispatched the sidecar,
    so a session-id join is as strong an identity claim as `plan_id` itself
    for a pre-fix commit. Narrow: only fires when the sidecar's `plan_id` is
    absent (never overrides a *mismatched* `plan_id`), and only when a
    receipt with a `session_id` exists.

    Returns (commit_sha, integration_sidecar_path, integration_data) or
    raises MintRefusal."""
    fmt = f"{_HEADER_SENTINEL}%H{_FIELD_SEP}%(trailers:key=Inline-Review,valueonly=true,unfold=true,separator={_MULTI_SEP})"
    try:
        out = _run_git(
            ["log", "--no-merges", f"-{_COMMIT_WALK_BOUND}", f"--pretty=format:{fmt}"],
            cwd=str(repo_root),
        )
    except _GitUnavailable as exc:
        raise MintRefusal(f"review-stamp: git log failed: {exc}") from exc

    receipt_session_id = _receipt_session_id(plan_path) if plan_path is not None else None
    share_root = repo_root / ".coordinator-local" / "subagent-share"
    receipt_share_dir_exists = bool(
        repair and receipt_session_id and (share_root / receipt_session_id).is_dir()
    )
    repair_candidate_sha: Optional[str] = None

    for line in out.splitlines():
        if not line.startswith(_HEADER_SENTINEL):
            continue
        header = line[len(_HEADER_SENTINEL):]
        sha, _, trailer_field = header.partition(_FIELD_SEP)
        for trailer in [t for t in trailer_field.split(_MULTI_SEP) if t]:
            m = _APPLIES_RE.match(trailer.strip())
            if not m:
                continue
            stem = m.group(1)
            if receipt_share_dir_exists and repair_candidate_sha is None:
                # Repair-only, weakest fallback: a trailer whose stem names no
                # readable sidecar at all (the example-retrieval-repo cc6525cf0 shape,
                # literally `applies None`). Newest-first walk, so the FIRST
                # such commit seen is the newest one -- accepted only when the
                # plan's own emit-receipt session has a real subagent-share
                # dir, i.e. some evidence a run actually happened for it.
                repair_candidate_sha = sha
            sidecar_path = _find_sidecar_by_stem(repo_root, stem)
            if sidecar_path is None:
                continue
            data = _load_sidecar(sidecar_path)
            if data is None:
                continue
            sidecar_plan_id = data.get("plan_id")
            if str(sidecar_plan_id or "") == plan_id:
                return sha, sidecar_path, data
            if not sidecar_plan_id and receipt_session_id:
                sidecar_session_id = data.get("lead_session_id") or data.get("dispatched_by")
                if sidecar_session_id and sidecar_session_id == receipt_session_id:
                    return sha, sidecar_path, data
    if repair and repair_candidate_sha is not None:
        return repair_candidate_sha, None, {}
    raise MintRefusal(
        f"review-stamp: no terminal commit found within {_COMMIT_WALK_BOUND} commits carrying "
        f"an Inline-Review trailer whose integration sidecar's plan_id equals {plan_id!r} "
        "(and, for a pre-fix None-trailer commit, no sidecar's lead_session_id matched the "
        "plan's own emit receipt session_id either)"
    )


#: Repair-only heuristic (§ module docstring `mint(..., repair=True)`): a
#: sidecar carrying both fields is shaped like a prep sidecar even with no
#: `prep_sidecar:` pointer anywhere left to follow it by.
_PREP_SHAPE_FIELDS = ("product_files", "whole_diff_sidecars")


def _repair_discover_prep_sidecar(share_dir: Path) -> Optional[Path]:
    for path in sorted(share_dir.glob("*.md")):
        if path.name.endswith(".blocks.md"):
            continue
        data = _load_sidecar(path)
        if data and all(field in data for field in _PREP_SHAPE_FIELDS):
            return path
    return None


def _repair_discover_wave_sidecars(share_dir: Path, exclude: set) -> List[Path]:
    """Reviewer-shaped sidecars under `share_dir`, oldest-name-first,
    excluding `exclude` (the prep sidecar and, when one resolved, the
    integration sidecar) and any `*.blocks.md` companion file. Repair-only
    heuristic -- see `_repair_discover_prep_sidecar`."""
    waves: List[Path] = []
    for path in sorted(share_dir.glob("*.md")):
        if path.name.endswith(".blocks.md") or path in exclude:
            continue
        data = _load_sidecar(path)
        if data is None:
            continue
        agent_type = str(data.get("agent_type") or "")
        if "review" not in agent_type:
            continue
        waves.append(path)
    return waves


def _repair_build_bookkeeping_record(
    repo_root: Path,
    plan_id: str,
    integration_path: Optional[Path],
    integration_data: Dict[str, Any],
    plan_path: Path,
):
    """`--repair`'s core: assemble a bookkeeping record shaped exactly like
    `mint`'s normal read, via `review_mint.wave_bookkeeping.bookkeep_wave`
    over this run's wave sidecars. Raises `MintRefusal` when the session_id,
    its subagent-share dir, a prep-shaped sidecar, or any wave sidecar
    cannot be discovered."""
    session_id = (
        integration_data.get("lead_session_id")
        or integration_data.get("dispatched_by")
        or _receipt_session_id(plan_path)
    )
    if not session_id:
        raise MintRefusal(
            "review-stamp: --repair could not determine a session_id to locate wave sidecars "
            "(no lead_session_id/dispatched_by on the resolved sidecar and no emit receipt)"
        )
    share_dir = repo_root / ".coordinator-local" / "subagent-share" / session_id
    if not share_dir.is_dir():
        raise MintRefusal(f"review-stamp: --repair found no subagent-share dir for session {session_id!r}")

    prep_path = _repair_discover_prep_sidecar(share_dir)
    if prep_path is None:
        raise MintRefusal(f"review-stamp: --repair could not locate a prep-shaped sidecar under {share_dir}")
    prep_rel = str(prep_path.relative_to(repo_root)).replace("\\", "/")

    exclude = {prep_path}
    if integration_path is not None:
        exclude.add(integration_path)
    wave_paths = _repair_discover_wave_sidecars(share_dir, exclude)
    if not wave_paths:
        raise MintRefusal(f"review-stamp: --repair found no wave sidecars under {share_dir}")

    from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave, review_wave_bookkeeping_stem

    record_stem = review_wave_bookkeeping_stem(plan_id, session_id) + ".repair"
    record = bookkeep_wave(
        wave_paths,
        repo_root=repo_root,
        session_id=session_id,
        plan_id=plan_id,
        prep_sidecar=prep_rel,
        record_stem=record_stem,
    )
    return Path(record["sidecar_path"]), record


def mint(
    plan_path: Path, repo_root: Path, *, build_test_path: Optional[str], repair: bool = False
) -> Dict[str, Any]:
    """Assemble and write `review_stamp:` into `plan_path`'s frontmatter.
    Returns the written stamp dict. Raises `MintRefusal` on any refusal
    condition; writes nothing in that case. `repair=True` is the explicit,
    never-default repair path for a pre-b' run -- see module docstring."""
    if not build_test_path:
        raise MintRefusal("review-stamp: refusing to mint: no build/test record (--build-test required)")
    build_test_sidecar = Path(build_test_path)
    if not repo_root.exists():
        raise MintRefusal(f"review-stamp: repo root does not exist: {repo_root}")

    text, split = _read_plan_frontmatter(plan_path)
    plan_id = read_fm_field_unquoted(split.fm_text, "plan_id")
    if not plan_id:
        raise MintRefusal(f"review-stamp: refusing to mint: {plan_path} carries no plan_id")

    terminal_sha, integration_path, integration_data = _resolve_terminal_commit(
        repo_root, plan_id, plan_path, repair=repair
    )

    prep_rel = integration_data.get("prep_sidecar")
    if not prep_rel:
        if not repair:
            if integration_path is None:
                raise MintRefusal(
                    f"review-stamp: refusing to mint: no terminal commit resolved for plan_id {plan_id!r}"
                )
            raise MintRefusal(f"review-stamp: integration sidecar {integration_path} carries no prep_sidecar")
        integration_path, integration_data = _repair_build_bookkeeping_record(
            repo_root, plan_id, integration_path, integration_data, plan_path
        )
        prep_rel = integration_data.get("prep_sidecar")
        if not prep_rel:
            raise MintRefusal(
                "review-stamp: --repair assembled a bookkeeping record with no prep_sidecar -- "
                f"see {integration_path}"
            )
    prep_path = repo_root / prep_rel
    prep_data = _load_sidecar(prep_path)
    if prep_data is None:
        raise MintRefusal(f"review-stamp: could not read prep sidecar {prep_path}")

    run_base_sha = prep_data.get("run_base_sha")
    product_file_list = prep_data.get("product_files") or []
    foreign_claims = prep_data.get("foreign_claims") or []

    whole_diff_sidecars = prep_data.get("whole_diff_sidecars") or {}
    delivery_rel = whole_diff_sidecars.get("delivery")
    delivery_data: Dict[str, Any] = {}
    if delivery_rel:
        delivery_data = _load_sidecar(repo_root / delivery_rel) or {}
    delivery_verdict = delivery_data.get("verdict")

    unresolved = integration_data.get("unresolved") or []
    confinement_violations = integration_data.get("confinement_violations") or []
    fixes_applied = integration_data.get("fixes_applied")
    slices = prep_data.get("slices") or []
    brief_conformance = integration_data.get("brief_conformance") or {}

    build_test_data = _load_sidecar(build_test_sidecar)
    if build_test_data is None:
        raise MintRefusal(f"review-stamp: no build/test record at {build_test_sidecar}")
    tests_status = build_test_data.get("status")
    build_test_verdict = "pass" if tests_status == "pass" else "fail"

    # Refusal predicates, in the order § Contract states them.
    if delivery_verdict != "PASS":
        raise MintRefusal(f"review-stamp: refusing to mint: delivery verdict is {delivery_verdict!r}, not PASS")
    if build_test_verdict != "pass":
        raise MintRefusal(f"review-stamp: refusing to mint: build/test verdict is {tests_status!r}, not pass")
    if len(unresolved) > 0:
        raise MintRefusal(f"review-stamp: refusing to mint: {len(unresolved)} unresolved finding(s)")
    if len(confinement_violations) > 0:
        raise MintRefusal(
            f"review-stamp: refusing to mint: {len(confinement_violations)} confinement violation(s)"
        )
    if len(foreign_claims) > 0:
        raise MintRefusal(f"review-stamp: refusing to mint: {len(foreign_claims)} foreign claim(s) on spine paths")
    if len(product_file_list) == 0:
        raise MintRefusal("review-stamp: refusing to mint: zero product files in the reviewed diff")

    try:
        tree_out = _run_git(["log", "-1", "--format=%T", terminal_sha], cwd=str(repo_root))
    except _GitUnavailable as exc:
        raise MintRefusal(f"review-stamp: could not read tree sha of {terminal_sha}: {exc}") from exc
    terminal_tree_sha = tree_out.strip()

    stamp: Dict[str, Any] = {
        "terminal_commit_sha": terminal_sha,
        "terminal_tree_sha": terminal_tree_sha,
        "run_base_sha": run_base_sha,
        "integration_sidecar": str(integration_path.relative_to(repo_root)).replace("\\", "/"),
        "slices": len(slices),
        "fixes_applied": fixes_applied,
        "em_may_think_differently": integration_data.get("em_may_think_differently") or [],
        "unresolved": [],
        "brief_conformance": {
            "items": brief_conformance.get("items"),
            "met": brief_conformance.get("met"),
            "unmet": brief_conformance.get("unmet"),
        },
        "delivery": {
            "verdict": delivery_verdict,
            "product_files": len(product_file_list),
        },
        "build_test": {
            "verdict": build_test_verdict,
            "ran": build_test_data.get("run"),
            "failed": build_test_data.get("failed"),
            "sidecar": str(build_test_path).replace("\\", "/"),
        },
        "stamped_at": None,
    }
    from datetime import datetime, timezone

    stamp["stamped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_yaml = yaml.safe_dump({"review_stamp": stamp}, default_flow_style=False, sort_keys=False).strip()
    # Drop the synthetic top-level key line; keep everything indented beneath it —
    # `review_stamp` is a nested mapping, so a block form is built by hand
    # rather than routed through insert_fm_field_raw's single-line contract.
    body_lines = raw_yaml.splitlines()[1:]
    indented = "\n".join(f"  {ln}" for ln in body_lines)
    new_fm = split.fm_text.rstrip() + "\n" + "review_stamp:\n" + indented + "\n"
    new_text = rebuild(split, new_fm)
    plan_path.write_text(new_text, encoding="utf-8")
    return stamp


def check(plan_path: Path, repo_root: Path, *, supersession: bool = False) -> Optional[str]:
    """Returns `None` when the plan's `review_stamp` is still valid, or a
    one-line refusal string. At most three git spawns (see module
    docstring)."""
    text = plan_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    split = split_frontmatter(text)
    if split is None:
        return "review-stamp: no parseable YAML frontmatter"
    try:
        fm_data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return "review-stamp: frontmatter is not valid YAML"
    stamp = fm_data.get("review_stamp") if isinstance(fm_data, dict) else None
    if not stamp:
        return "review-stamp: no review_stamp on this plan"

    terminal = stamp.get("terminal_commit_sha")
    expected_tree = stamp.get("terminal_tree_sha")
    if not terminal or not expected_tree:
        return "review-stamp: stamp is missing terminal_commit_sha/terminal_tree_sha"

    try:
        tree_out = _run_git(["log", "-1", "--format=%T", terminal], cwd=str(repo_root))
    except _GitUnavailable as exc:
        return f"review-stamp: could not resolve terminal commit {terminal}: {exc}"
    if tree_out.strip() != expected_tree:
        return f"review-stamp: terminal commit {terminal}'s tree no longer matches the stamped tree"

    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", terminal, "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"review-stamp: could not check ancestry for {terminal}: {exc}"
    if result.returncode == 1:
        # `merge-base --is-ancestor` exits 1 specifically for "genuinely not an
        # ancestor" -- both refs resolved, the relationship just doesn't hold.
        return f"review-stamp: terminal commit {terminal} is not an ancestor of HEAD"
    if result.returncode != 0:
        # Any other non-zero (>1, or a signal) means a ref failed to resolve at all
        # (bad/garbage-collected sha, rewritten history) -- a different failure mode
        # from "not an ancestor" and worth telling the operator apart.
        detail = result.stderr.strip() or "git command failed"
        return f"review-stamp: could not resolve {terminal} or HEAD for ancestry check: {detail}"

    if supersession:
        writes = fm_data.get("scope") or []
        if isinstance(writes, str):
            writes = [writes]
        writes = [str(w) for w in writes if isinstance(w, str)]
        if writes:
            try:
                out = _run_git(["log", "--format=%H", f"{terminal}..HEAD", "--", *writes], cwd=str(repo_root))
            except _GitUnavailable as exc:
                return f"review-stamp: could not check supersession: {exc}"
            if out.strip():
                return (
                    f"review-stamp: a later commit touches a declared write of this plan "
                    f"since the stamped terminal commit {terminal}"
                )

    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-stamp")
    sub = parser.add_subparsers(dest="verb", required=True)

    mint_p = sub.add_parser("mint", help="Mint the review_stamp for a plan.")
    mint_p.add_argument("--plan", required=True)
    mint_p.add_argument("--build-test", required=False, default=None)
    mint_p.add_argument("--repo-root", required=False, default=None)
    mint_p.add_argument(
        "--repair",
        action="store_true",
        help="Explicit, never-default repair path for a pre-b' run whose sidecar predates "
        "wave_bookkeeping's fields (see module docstring mint(..., repair=True)).",
    )

    check_p = sub.add_parser("check", help="Check an existing review_stamp.")
    check_p.add_argument("--plan", required=True)
    check_p.add_argument("--repo-root", required=False, default=None)
    check_p.add_argument("--supersession", action="store_true")

    pf_p = sub.add_parser("product-files", help="Print the product-file count of a diff.")
    pf_p.add_argument("--diff", required=True)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.verb == "product-files":
        files = product_files(Path(args.diff))
        print(len(files))
        return 0

    plan_path = Path(args.plan)
    from coordinator_core.git.repo_root import show_toplevel

    repo_root = Path(args.repo_root) if getattr(args, "repo_root", None) else None
    if repo_root is None:
        top = show_toplevel(str(plan_path.parent))
        repo_root = Path(top) if top else Path.cwd()

    if args.verb == "mint":
        try:
            stamp = mint(
                plan_path, repo_root, build_test_path=args.build_test, repair=bool(getattr(args, "repair", False))
            )
        except MintRefusal as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"review-stamp: minted for {plan_path} at {stamp['terminal_commit_sha']}")
        return 0

    if args.verb == "check":
        reason = check(plan_path, repo_root, supersession=args.supersession)
        if reason is not None:
            print(reason, file=sys.stderr)
            return 1
        print(f"review-stamp: valid for {plan_path}")
        return 0

    parser.error(f"unknown verb {args.verb!r}")
    return 2


from coordinator_core.ipc import register_op  # noqa: E402 — after CLI-safe module body


@register_op("review_stamp.mint")
def _mint_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    plan_path = Path(params["plan"])
    root = repo_root or Path(params.get("repo_root") or ".")
    stamp = mint(plan_path, root, build_test_path=params.get("build_test"), repair=bool(params.get("repair", False)))
    return {"status": "minted", "review_stamp": stamp}


@register_op("review_stamp.check")
def _check_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    plan_path = Path(params["plan"])
    root = repo_root or Path(params.get("repo_root") or ".")
    reason = check(plan_path, root, supersession=bool(params.get("supersession", False)))
    return {"status": "valid" if reason is None else "refused", "reason": reason}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
