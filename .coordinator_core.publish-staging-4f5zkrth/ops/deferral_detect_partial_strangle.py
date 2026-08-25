"""
coordinator_core.ops.deferral_detect_partial_strangle — JSON-RPC "deferral.detect_partial_strangle".

Purpose: a read-only DETECTOR for the "partial strangler" hidden-deferral shape — a
DR-210-style strangler that declares a verb set (e.g. `list`, `draft`, `compose`, `send`)
but only ever ships/plans a subset natively, leaving the rest silently stuck on the legacy
surface with no tracking artifact. DR-210's verb set lives in NLP prose, not anything
machine-checkable — so this detector reads a dedicated **endpoint-manifest convention**
(see `_parse_manifest_block` below) rather than attempting to parse DR prose. Analog:
`ops/draft_plan_aging.py`'s scope-path/frontmatter-scan idiom, re-cast into the registered
three-state dict (not exit codes); registered-shape wrapper modeled on `ops/engine_drift.py`.

THREE-state offers-not-nags contract (mirrors engine_drift.py exactly — do not collapse):
  - "clean"                    — every checkable strangler has empty UNPLANNED (declared
                                  verbs fully accounted for by shipped + planned). Silent;
                                  carries neither "offer" nor "notice".
  - "partial_strangles_found"  — at least one checkable strangler has non-empty UNPLANNED
                                  (declared - shipped - planned). Carries "findings" (one
                                  entry per flagged strangler) and a top-level "offer"
                                  summarizing all findings (offers-not-nags: lead with the
                                  concrete gap, not an alarm).
  - "indeterminate"             — at least one `docs/decisions/*.md` doc that HAS a
                                  ```yaml strangler-endpoint``` fenced block whose content
                                  fails to parse (opted-in-but-broken: present but not
                                  machine-readable). This is NEVER folded into "clean" — an
                                  opted-in-but-broken strangler is not proven complete, it's
                                  unproven. Carries "notice" (a "notices" list when more than
                                  one). If findings AND indeterminate stranglers both exist in
                                  the same run, findings win (state stays
                                  "partial_strangles_found") but the notices list still rides
                                  along — the strongest signal present wins arbitration
                                  (finding > indeterminate > clean), and nothing is dropped.

Endpoint-manifest convention: a strangler DR embeds a fenced
```yaml strangler-endpoint
strangler_id: DR-210
declared_verbs: [list, draft, compose, send]
shipped_native_op:
  send: coordinator_core/ops/fleet/memo_send.py
```
block. `shipped_native_op` is optional per-verb — a declared verb absent from it is never
counted "shipped" by this detector (no NLP fallback attempt at "otherwise resolvable"; see
Negative-spec). Seeded into `docs/decisions/DR-210-makima-native-tooling-ownership-strangler.md`
as the self-test fixture (see that DR's "Strangler endpoint manifest (machine-readable)"
section).

Discovery is OPT-IN by fence presence, DR-home only (2026-07-21 design pivot — see
Negative-spec for the root cause this closes). `_scan_manifest_candidates` scans
`docs/decisions/*.md` with NO filename filter and selects ONLY files whose body contains a
```yaml strangler-endpoint``` fenced block; a DR without the fence is simply not a candidate
and emits nothing (no notice, no noise). `docs/plans/` is NEVER scanned for manifests — it is
scanned only by `_make_planned_check` for planned-evidence (see below), a disjoint surface by
construction.

Spec backlink: tasks/hidden-deferral-detectors/design.md § Detector 1 —
`deferral.detect_partial_strangle`.

Negative-spec:
    - Does NOT parse DR prose with NLP/regex-over-English — the whole point of the
      endpoint-manifest convention is to avoid that fragility.
    - Does NOT use `yaml.safe_load` on frontmatter (that remains `_fm_util`'s
      `extract_frontmatter_scalar` territory) — `yaml.safe_load` is used ONLY on the already
      fenced-block-extracted `strangler-endpoint` YAML content, never on raw frontmatter or
      raw file bodies.
    - "shipped" never falls back to a fuzzy/inferred native-op resolution when a verb is
      absent from `shipped_native_op` — a strangler manifest that omits a verb's shipped path
      is read as "not shipped" for that verb, full stop.
    - Manifest home is `docs/decisions/` ONLY — there is deliberately no `docs/plans/`
      manifest path. Live-verified root cause (2026-07-21, Patrik + code-reviewer
      convergence): the original design allowed a manifest to live in "the strangler DR (or
      strangle plan)" and discovered candidates by filename-globbing
      (`docs/decisions/*strangl*.md` + `docs/plans/*strang*.md`). That shape had two live
      defects on the real tree: (a) the glob scooped ~25 review sidecars
      (`*.patrik-review.md`, `*.sonnet-review.md`, `*.prior-art-check.md`,
      `*.plan-coverage-check.md`, `*.phase0.md`) and ~13 landed legacy strangle plans into a
      38-notice `indeterminate` wall, drowning any real signal; (b) a manifest embedded in
      its OWN `docs/plans/*.md` file self-satisfied `planned_check` for every declared verb
      purely because the manifest's own YAML text mentions the verb names — a strangler could
      declare a verb set, ship none of it, and still read CLEAN forever. Restricting the
      manifest home to `docs/decisions/` (never `docs/plans/`) closes both: discovery is
      opt-in by fence presence (no filename-glob noise), and because `planned_check` only
      ever greps `docs/plans/*.md` — disjoint from the manifest's home — a manifest can never
      be its own planned-evidence. This is closure by construction, not a runtime guard.
    - The "planned" check is a plain-text grep over `docs/plans/*.md` for the strangler
      identifier plus the verb as a whole word. It is NOT filename-filtered — a strangler's
      completion plan need not be named like a strangler plan itself (e.g.
      `2026-07-21-memo-tool-rebuild-full-ownership.md` plans DR-210's `list`/`draft`/`compose`
      verbs without "strang" in its own filename). Residual false-planned risk: this is a
      word-boundary regex for a common English verb co-occurring with `strangler_id` anywhere
      in a plan body — it can mark a verb "planned" on an incidental mention rather than a
      real planning commitment. Future work (not built here): a reciprocal `plans_verbs:`
      declared marker in the referencing plan's own frontmatter, keyed to `strangler_id`, so
      "planned" requires an explicit claim rather than an incidental co-occurrence.
    - `_extract_fenced_manifest_block` is first-match-wins (`re.search`, not `re.finditer`) —
      multiple `strangler-endpoint` fenced blocks in the same doc are unsupported; only the
      first is ever read.

Scan-failure handling: both `_scan_manifest_candidates` (docs/decisions/, manifest discovery)
AND `_make_planned_check` (docs/plans/, planned-evidence discovery — Review: code-reviewer
Finding 1) use `os.scandir` (not `Path.glob`, which silently swallows `PermissionError` while
walking) to list their respective directories; an unreadable directory on EITHER surface
contributes to `scan_errors`, and `_handler` ORs both surfaces' errors into
`scan_incomplete`/`scan_errors` on the result — a scan failure on either surface is never
indistinguishable from a genuinely clean/complete "no manifests / no planned evidence" result.

Self-registration: importing this module calls register_op("deferral.detect_partial_strangle",
...) as a side-effect (same pattern as ops/engine_drift.py / ops/draft_plan_aging.py's sibling
detectors).
"""

from __future__ import annotations
import sys

import logging
import os
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import yaml

from coordinator_core.ipc import register_op

logger = logging.getLogger(__name__)

_FENCE_TAG = "strangler-endpoint"
_FENCE_PATTERN = re.compile(
    r"```yaml[ \t]+" + re.escape(_FENCE_TAG) + r"[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)


def _extract_fenced_manifest_block(text: str) -> Optional[str]:
    """Extract the content of the first ```yaml strangler-endpoint``` fenced block.

    Returns the raw YAML text between the opening and closing fences, or None if no such
    fenced block is present in `text` at all.
    """
    match = _FENCE_PATTERN.search(text)
    if match is None:
        return None
    return match.group(1)


def _derive_slug(path: str, strangler_id: Optional[str] = None) -> str:
    """Derive a human-readable slug from a strangler doc's basename.

    Strips the `.md` extension and, if the basename starts with "<strangler_id>-", strips
    that prefix too (so "DR-210-makima-native-tooling-ownership-strangler.md" with
    strangler_id "DR-210" yields "makima-native-tooling-ownership-strangler", not a
    redundant "DR-210-makima-native-...").
    """
    stem = Path(path).stem
    if strangler_id and stem.startswith(f"{strangler_id}-"):
        return stem[len(strangler_id) + 1:]
    return stem


def _parse_manifest_candidate(path: str, text: str) -> dict:
    """Classify one strangler-shaped doc into a manifest-present or indeterminate candidate.

    Returns a dict with "kind" == "manifest" (parseable ```yaml strangler-endpoint``` block
    present, with strangler_id/declared_verbs/shipped_native_op fields) or "kind" ==
    "indeterminate" (no fenced block at all, or the fenced content fails to parse as YAML —
    both read as "unmanifested", never crash the scan).
    """
    block = _extract_fenced_manifest_block(text)
    if block is None:
        return {"kind": "indeterminate", "path": path, "slug": _derive_slug(path)}

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        print(f"skip: _parse_manifest_candidate: data = yaml.safe_load(block) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {"kind": "indeterminate", "path": path, "slug": _derive_slug(path)}

    if not isinstance(data, dict):
        return {"kind": "indeterminate", "path": path, "slug": _derive_slug(path)}

    strangler_id = data.get("strangler_id")
    if not strangler_id:
        return {"kind": "indeterminate", "path": path, "slug": _derive_slug(path)}

    declared_verbs = data.get("declared_verbs") or []
    shipped_native_op = data.get("shipped_native_op") or {}

    return {
        "kind": "manifest",
        "path": path,
        "strangler_id": str(strangler_id),
        "slug": _derive_slug(path, str(strangler_id)),
        "declared_verbs": [str(v) for v in declared_verbs],
        "shipped_native_op": {str(k): str(v) for k, v in shipped_native_op.items()},
    }


def _render_planned_group(planned_verbs: List[str], planned_via: dict) -> str:
    """Render the "planned {...}" offer segment, grouping verbs by covering plan.

    Verbs whose `planned_via` entry is a plan path are grouped and suffixed
    "via <plan-basename>" (design.md § Detector 1's output example); verbs with no known
    plan attribution (planned_check returned a truthy-but-non-string value) are rendered
    bare, matching the pre-attribution format. Groups are joined "; "; order follows
    `planned_verbs`' declared-verb order, deduplicated by first-seen plan.
    """
    if not planned_verbs:
        return ""

    groups: List[str] = []
    seen_keys: List[Optional[str]] = []
    bucket: dict = {}
    for verb in planned_verbs:
        key = planned_via.get(verb)
        if key not in bucket:
            bucket[key] = []
            seen_keys.append(key)
        bucket[key].append(verb)

    for key in seen_keys:
        verbs = bucket[key]
        if key:
            groups.append(f"{', '.join(verbs)} via {Path(key).stem}")
        else:
            groups.append(", ".join(verbs))
    return "; ".join(groups)


def classify_partial_strangles(
    manifests: List[dict],
    shipped_check: Callable[[str], bool],
    planned_check: Callable[[str, str], Optional[str]],
) -> dict:
    """Pure decision logic — no I/O, deterministically testable.

    Args:
        manifests: one entry per strangler-shaped doc found by the scan. Each entry is
            either `{"kind": "manifest", "path", "strangler_id", "slug", "declared_verbs",
            "shipped_native_op"}` (a parseable endpoint-manifest block was found) or
            `{"kind": "indeterminate", "path", "slug"}` (a `docs/decisions/` doc opted in via
            the fence but its block failed to parse — never treated as clean).
        shipped_check: callable(path) -> bool — True iff the given `shipped_native_op[verb]`
            path exists on disk. Pure dict-lookup drives WHICH path gets checked; this
            callable only does the fs-existence I/O.
        planned_check: callable(verb, strangler_id) -> Optional[str] — the relative path of
            some plan under `docs/plans/*.md` that references the strangler (by strangler_id)
            and mentions the verb, or `None` if no such plan exists. Truthy-but-non-string
            return values (e.g. a test double returning `True`) are treated as "planned, plan
            unknown" — the "via <plan>" attribution is best-effort, not load-bearing for the
            planned/unplanned classification itself. Only consulted for verbs not already
            counted shipped.

    Returns a result dict with a "state" key set to one of "clean" |
    "partial_strangles_found" | "indeterminate". Arbitration: finding > indeterminate >
    clean — if both findings and indeterminate stranglers are present, state stays
    "partial_strangles_found" but a "notices" list still carries the indeterminate ones (see
    module docstring's three-state contract).
    """
    findings: List[dict] = []
    notices: List[dict] = []

    for entry in manifests:
        if entry.get("kind") == "indeterminate":
            notice_text = (
                f"{entry['slug']} ({entry['path']}): no machine-readable endpoint manifest "
                f"— add a ```yaml {_FENCE_TAG}``` block to make it checkable"
            )
            notices.append({"path": entry["path"], "slug": entry["slug"], "notice": notice_text})
            continue

        strangler_id = entry["strangler_id"]
        declared_verbs = entry.get("declared_verbs", [])
        shipped_map = entry.get("shipped_native_op", {})

        shipped_verbs = [
            verb
            for verb in declared_verbs
            if shipped_map.get(verb) and shipped_check(shipped_map[verb])
        ]
        shipped_set = set(shipped_verbs)

        # verb -> covering-plan-path (or None if planned but attribution unknown), for
        # verbs not already shipped and for which planned_check returned something truthy.
        planned_via: dict = {}
        for verb in declared_verbs:
            if verb in shipped_set:
                continue
            plan_ref = planned_check(verb, strangler_id)
            if plan_ref:
                planned_via[verb] = plan_ref if isinstance(plan_ref, str) else None

        planned_verbs = [verb for verb in declared_verbs if verb in planned_via]
        planned_set = set(planned_verbs)

        unplanned_verbs = [
            verb for verb in declared_verbs if verb not in shipped_set and verb not in planned_set
        ]

        if unplanned_verbs:
            offer = (
                f"[deferral] {strangler_id} {entry['slug']}: "
                f"shipped {{{', '.join(shipped_verbs)}}}; "
                f"planned {{{_render_planned_group(planned_verbs, planned_via)}}}; "
                f"UNPLANNED {{{', '.join(unplanned_verbs)}}}"
            )
            findings.append(
                {
                    "strangler_id": strangler_id,
                    "path": entry["path"],
                    "shipped": shipped_verbs,
                    "planned": planned_verbs,
                    "unplanned": unplanned_verbs,
                    "offer": offer,
                }
            )

    if findings:
        result = {
            "state": "partial_strangles_found",
            "findings": findings,
            "offer": "; ".join(f["offer"] for f in findings),
        }
        if notices:
            result["notices"] = notices
        return result

    if notices:
        if len(notices) == 1:
            notice = notices[0]["notice"]
        else:
            notice = (
                f"{len(notices)} strangler docs have no machine-readable endpoint manifest — "
                + "; ".join(n["notice"] for n in notices)
            )
        return {"state": "indeterminate", "notices": notices, "notice": notice}

    return {"state": "clean"}


def _scan_manifest_candidates(repo_root: Path) -> Tuple[List[dict], List[str]]:
    """Scan `docs/decisions/*.md` for opted-in strangler-endpoint manifests.

    Discovery is OPT-IN by fence presence, NOT filename-glob (2026-07-21 pivot — see module
    docstring's Negative-spec for the root cause this closes). Every `docs/decisions/*.md`
    file is a candidate; a file is included in the returned list ONLY if its body contains a
    ```yaml strangler-endpoint``` fenced block. A decisions doc with no such fence is not a
    candidate at all — it is skipped silently, emitting neither a "manifest" nor an
    "indeterminate" entry (no noise for the ~hundreds of unrelated DRs). `docs/plans/` is
    NEVER scanned here — manifests live in `docs/decisions/` only; `docs/plans/` is scanned
    solely by `_make_planned_check` for planned-evidence, a disjoint surface by construction.

    NOTE: uses `os.scandir()`, NOT `Path.glob("*.md")` — `Path.glob()`'s selector silently
    swallows `PermissionError` while walking (empirically re-verified: a chmod-000 dir yields
    an empty iterator from `glob()`, no exception), which would make an unreadable
    `docs/decisions/` indistinguishable from "no strangler manifests exist here" — exactly the
    silent-clean-on-scan-failure shape this detector must not produce (see the module
    docstring's three-state contract: an unproven corpus is never folded into "clean").

    Returns `(candidates, scan_errors)`:
      candidates   — one dict per opted-in file (sorted for deterministic output), each
                     already classified by `_parse_manifest_candidate` into "manifest" (fence
                     present, parses) or "indeterminate" (fence present, fails to parse —
                     opted-in-but-broken). An unreadable individual file is skipped silently
                     (mirrors draft_plan_aging.py's degrade-not-crash convention on a
                     transient read failure) rather than raising.
      scan_errors  — non-empty ONLY when `docs/decisions/` itself could not be listed (e.g.
                     permission-denied). Callers MUST treat a non-empty `scan_errors` as "this
                     candidate list may be incomplete", never as "docs/decisions/ genuinely has
                     no manifests" — see `_handler`'s `scan_incomplete`/`scan_errors` passthrough.
    """
    decisions_dir = repo_root / "docs" / "decisions"
    if not decisions_dir.is_dir():
        return [], []

    candidates: List[dict] = []
    scan_errors: List[str] = []

    try:
        entries = sorted(os.scandir(decisions_dir), key=lambda e: e.name)
    except OSError as exc:
        # Review: code-reviewer Finding 5 — switched from print(file=sys.stderr) to a
        # module logger, matching every sibling silent-enumeration fix in this same
        # commit wave (ripe_filter.py, deliverable_rollup.py, memo_triage.py,
        # claims.py) and making this scan-failure observable via caplog in tests.
        logger.warning(
            "deferral.detect_partial_strangle: cannot scan %s — %s; "
            "manifest discovery degraded (NOT the same as \"no manifests found\")",
            decisions_dir,
            exc,
        )
        scan_errors.append(f"{decisions_dir}: {exc}")
        return candidates, scan_errors

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        path = Path(entry.path)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            print(f"skip: _scan_manifest_candidates: text = path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if _extract_fenced_manifest_block(text) is None:
            continue
        rel_path = str(path.relative_to(repo_root))
        candidates.append(_parse_manifest_candidate(rel_path, text))

    return candidates, scan_errors


def _make_shipped_check(repo_root: Path) -> Callable[[str], bool]:
    """Build the fs-existence shipped_check callable, closed over repo_root."""

    def _shipped_check(rel_path: str) -> bool:
        return (repo_root / rel_path).exists()

    return _shipped_check


def _make_planned_check(
    repo_root: Path, scan_errors: Optional[List[str]] = None
) -> Callable[[str, str], Optional[str]]:
    """Build the plan-grep planned_check callable, closed over repo_root.

    A verb is "planned" for a strangler iff some `docs/plans/*.md` file's text contains the
    strangler_id AND mentions the verb as a whole word (word-boundary regex — avoids a
    substring false-positive like "list" inside "checklist"). Returns the first matching
    plan's repo-relative path (for offer attribution — "planned {verb via <plan>}"), or
    `None` if no plan matches. `docs/plans/` is scanned in its entirety here regardless of
    filename shape — this is the ONLY surface `_make_planned_check` reads, and it is disjoint
    from `_scan_manifest_candidates`'s `docs/decisions/`-only manifest scan by construction
    (see module docstring's Negative-spec on why that disjointness matters).

    NOTE: uses `os.scandir()`, NOT `Path.glob("*.md")` — `Path.glob()`'s selector silently
    swallows `PermissionError` while walking (same defect class documented on
    `_scan_manifest_candidates` above), which would make an unreadable `docs/plans/`
    indistinguishable from "no planned evidence exists" — this INVERTS the detector's
    three-state contract (an indeterminate scan reads as a confident, and wrong,
    `partial_strangles_found`). Review: code-reviewer Finding 1.

    `scan_errors`, when given a list, has `"{plans_dir}: {exc}"` appended (at most once per
    `_make_planned_check` instance) the first time `docs/plans/` itself cannot be listed —
    callers (see `_handler`) OR this into the same `scan_incomplete`/`scan_errors` signal
    `_scan_manifest_candidates` already produces, so a degraded planned-evidence scan is
    never silently indistinguishable from a genuinely clean one.
    """
    plans_dir = repo_root / "docs" / "plans"
    _reported = {"done": False}

    def _report_scan_error(exc: OSError) -> None:
        if scan_errors is None or _reported["done"]:
            return
        scan_errors.append(f"{plans_dir}: {exc}")
        _reported["done"] = True

    def _planned_check(verb: str, strangler_id: str) -> Optional[str]:
        if not plans_dir.is_dir():
            return None
        verb_re = re.compile(r"\b" + re.escape(verb) + r"\b")
        try:
            entries = sorted(os.scandir(plans_dir), key=lambda e: e.name)
        except OSError as exc:
            logger.warning(
                "deferral.detect_partial_strangle: cannot scan %s — %s; "
                "planned-evidence discovery degraded (NOT the same as \"no plans found\")",
                plans_dir,
                exc,
            )
            _report_scan_error(exc)
            return None
        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            path = Path(entry.path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                print(f"skip: _planned_check: text = path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            if strangler_id not in text:
                continue
            if verb_re.search(text):
                return path.relative_to(repo_root).as_posix()
        return None

    return _planned_check


@register_op("deferral.detect_partial_strangle")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "deferral.detect_partial_strangle" handler.

    Params: none consumed (this detector is date-independent — no aging window).
    repo_root: resolved main-worktree root for the caller's repo (common_dir scope);
        falls back to `Path.cwd()` when None (the `python3 -m` standalone-invocation path,
        per design.md's "Shared helpers" convention).

    Scans `docs/decisions/*.md` for opted-in (fence-present) ```yaml strangler-endpoint```
    manifest blocks, builds the fs-existence and plan-grep I/O callables, and delegates the
    three-state decision to the pure `classify_partial_strangles()`. Adds `scan_incomplete`
    (bool) and `scan_errors` (list[str]) to the result — True/non-empty when EITHER
    `docs/decisions/` (manifest discovery) OR `docs/plans/` (planned-evidence discovery, via
    `_make_planned_check`'s degraded-scan signal — Review: code-reviewer Finding 1) could not
    be listed. Callers MUST treat `scan_incomplete=True` as "this result may be missing
    manifests or planned-evidence", never as equivalent to a genuinely clean/complete scan —
    the three-state decision itself is otherwise returned unmodified.
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    manifests, scan_errors = _scan_manifest_candidates(root)
    shipped_check = _make_shipped_check(root)
    planned_scan_errors: List[str] = []
    planned_check = _make_planned_check(root, planned_scan_errors)
    result = classify_partial_strangles(manifests, shipped_check, planned_check)
    all_scan_errors = scan_errors + planned_scan_errors
    result["scan_incomplete"] = len(all_scan_errors) > 0
    result["scan_errors"] = all_scan_errors
    return result
