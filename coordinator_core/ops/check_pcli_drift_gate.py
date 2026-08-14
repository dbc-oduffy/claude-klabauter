"""
coordinator_core.ops.check_pcli_drift_gate — pcli-04 drift gate: detects
divergence between the `dispatch_feed` contract
(DoE-claude `coordinator/schemas/run-report.schema.json`) and the captured
live `Workflow` `agent()` option surface
(DoE-claude `coordinator/schemas/workflow-tool-api-capture.<date>.json`),
plus two adjacent failure modes on the same DoE-owned schema files.

Purpose: `coordinator_core/ops/dispatch_emit/` (pcli-04) generates real
Workflow scripts against the `dispatch_feed` contract. If that contract and
the live `Workflow` `agent()` option surface silently diverge, the failure
surfaces at execute-plan time — the most expensive dispatch shape in the
fleet. Only an EM session holding the `Workflow` tool can read the live API,
so this claude-klabauter-resident gate cannot read it directly; it compares two files
DoE already produced. **The staleness leg (below) is not incidental hygiene
— it is the entire live-detection mechanism.** The contract-vs-capture diff
alone can only catch DoE editing their own schema; nothing else in this gate
ever causes anyone to look at the live API. A version that treats staleness
as optional polish detects nothing about the harness and reports green
forever.

Three FAIL conditions (independent — a message names each that fires):

    1. Contract-vs-capture drift (`compute_contract_capture_drift`).
    2. Capture staleness, `captured_at` more than `_MAX_AGE_DAYS` days old,
       OR the capture filename's date disagreeing with `captured_at`
       (`compute_staleness`).
    3. C7 extension: `subagent-catering-resolution.json`'s recorded
       `source_hashes` no longer matching the live policy/snippet files it
       was generated from (`compute_hash_drift`).

Comparison granularity (leg 1) — resolved, not re-litigable without new
evidence: declared key-mapping SET comparison, not whole-object equality and
not bare key-presence. The two sides have structurally different shapes — a
JSON Schema `properties` block versus a capture record's prose-valued
`opts_fields` dict — so deep equality is undefined on this pair, not merely
undesirable. Order-insensitive set comparison over an explicitly declared
mapping (`_MIRRORED`/`_CONTRACT_ONLY`/`_CAPTURE_ONLY` below) is the only
honest diff on this pair, and it is also the one that does not fire on
cosmetic reordering. `_MIRRORED` is the property pairing itself; the FAIL
conditions in `compute_contract_capture_drift` additionally require every
`opts_fields` key to land in `_MIRRORED.values()` or `_CAPTURE_ONLY`, and
every `dispatch_feed.properties` key to land in `_MIRRORED` or
`_CONTRACT_ONLY` — closed allowlists, so a new live `agent()` option (the
drift this gate exists for) or a new contract field cannot slip through as
"just another key we ignore"; widening either set is a deliberate, reviewable
edit, never automatic.

Named limitation (leg 1), stated rather than papered over with a fake
heuristic: the capture's `opts_fields` values are human prose descriptions,
not type declarations, so a *type* change on an existing option is not
detectable on this pair. Key-set drift is what this leg can see. Closing
that gap is out of scope here — see the module's negative-spec below.

Staleness window (leg 2) — `_MAX_AGE_DAYS = 14`, hard FAIL, not a warning,
transcribed from the resolving debt-backlog entry
(`state/debt-backlog/2026-08-13-pcli-04-drift-gate-dispatch-feed-vs-the-bce793e4e50e.yaml`),
summarized here rather than re-derived: every 7-day FAIL-class threshold in
this fleet carries a second AND-leg as backstop, and this gate structurally
cannot have one — the second signal lives in another process's tool surface.
A single-leg 7-day window pinned to a 7-day ceremony FAILs whenever that
ceremony slips by a day, a false-FAIL treadmill that gets the gate overridden
as noise. 14 days is two workweeks: refresh pins to `/workweek-complete`,
where a DoE EM session holding the `Workflow` tool naturally exists, with one
full ceremony of slack. Named residual: the Workflow API has never been
OBSERVED changing — exactly one capture exists, so the drift rate is
unmeasured, not low. The defensible range is 7-21 days; 14 is the
convention-consistent point in it. A SECOND capture is what closes this —
once two exist, diffing them across a known harness-version delta is the
first real datapoint, and this window should be revisited then.

Clock (leg 2): off the capture's own `captured_at` field, never file mtime
or commit date — precedent
`coordinator_core.ops.check_import_budget_staleness`'s docstring records
that a commit-date clock "would have read FRESH throughout" the regression
it was built for; the same failure mode applies here (a capture's other
fields can be hand-edited repeatedly while `captured_at` sits unrefreshed).
Config: `_MAX_AGE_DAYS` in this module is the authority. An optional
`max_age_days` field in the capture JSON may only SHORTEN the effective
window: `effective = min(_MAX_AGE_DAYS, capture_value or inf)` — capture-only
config would let the watched party set its own deadline, the same hole the
capture's own `$comment` already warns against for hand-editing.

Repo-root / DoE-clone resolution: this module imports and reuses
`coordinator_core.ops.ensure_doe_clone.resolve_doe_clone()` (env override
`REPO_DOE_CLAUDE`, then `machine-local get repos.doe_claude`) rather than
hardcoding a path or re-implementing the tiering — same division of labor as
every other DoE-clone-resolving op in this repo.

Negative-spec (deliberately NOT covered here):
    - Does NOT read the live `Workflow` tool API. Only an EM session holds
      that tool; the live-API read leg and refreshing the C3 capture both
      stay DoE-owned (see module docstring above).
    - Does NOT shell out to `claude --version` or any other binary to
      compare harness versions — not in
      `docs/reference/shell-out-carve-outs.md`; would need a named PM
      carve-out first.
    - Does NOT detect a type change on an existing `dispatch_feed`/
      `opts_fields` mirror pair — see "Named limitation" above.
    - Does NOT edit `run-report.schema.json`, the capture, or
      `subagent-catering-resolution.json` — read-only to this repo, DoE owns
      all three wire-for-wire.
    - Does NOT widen into auditing any other cross-repo contract. One gate,
      three FAIL conditions.

Exit codes (`main`), matching `coordinator/bin/schema-drift-gate`'s
three-way contract — a gate that cannot run must never read as a gate that
ran clean:
    0   PASS  — all three legs clean.
    1   FAIL  — at least one leg fired. stdout names which leg(s) fired and
                the threshold applied (leg 2).
    2   ERROR — cannot run: DoE clone unresolvable, a cited schema file
                absent, or malformed/unexpected-shape JSON. Distinct from
                FAIL.

Spec backlink: state/handoffs/2026-08-13-pcli-04-drift-gate.md
Spec backlink: state/debt-backlog/2026-08-13-pcli-04-drift-gate-dispatch-feed-vs-the-bce793e4e50e.yaml
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.ensure_doe_clone import resolve_doe_clone

_MAX_AGE_DAYS = 14

# contract dispatch_feed property -> capture opts_fields key
_MIRRORED = {
    "label": "label",
    "agent_type": "agentType",
    "model": "model",
    "effort": "effort",
    "schema_ref": "schema",
    "phase": "phase",
}
_CONTRACT_ONLY = {"brief_ref", "gate_kind", "write_files", "est_min"}
    # emitter-derived, not agent() API mirrors — their own schema descriptions say so
_CAPTURE_ONLY = {"isolation"}
    # present in the live API, deliberately never emitted (worktrees banned at the tool seam)

_CAPTURE_FILENAME_RE = re.compile(r"workflow-tool-api-capture\.(\d{4}-\d{2}-\d{2})\.json$")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


class GateError(Exception):
    """A condition under which the gate cannot run at all — maps to
    EXIT_ERROR, never EXIT_FAIL. Distinct from a drift/staleness/hash
    finding, which is a verdict the gate successfully computed."""


# ---------------------------------------------------------------------------
# Leg 1 — contract-vs-capture drift
# ---------------------------------------------------------------------------


def compute_contract_capture_drift(
    contract_properties: "set[str] | list[str]",
    capture_opts_fields: "set[str] | list[str]",
) -> list[str]:
    """Pure predicate. Returns a list of FAIL reason strings (empty == leg
    clean) per the closed-allowlist rule described in the module docstring."""
    contract_properties = set(contract_properties)
    capture_opts_fields = set(capture_opts_fields)
    reasons: list[str] = []

    for contract_key, capture_key in _MIRRORED.items():
        if contract_key not in contract_properties:
            reasons.append(
                f"_MIRRORED contract key '{contract_key}' missing from dispatch_feed.properties"
            )
        if capture_key not in capture_opts_fields:
            reasons.append(
                f"_MIRRORED capture key '{capture_key}' (mirrors contract key '{contract_key}') "
                "missing from capture opts_fields"
            )

    mirrored_values = set(_MIRRORED.values())
    for key in sorted(capture_opts_fields):
        if key not in mirrored_values and key not in _CAPTURE_ONLY:
            reasons.append(
                f"capture opts_fields key '{key}' is in neither _MIRRORED nor _CAPTURE_ONLY — "
                "undeclared live agent() option, precisely the drift this gate exists for"
            )

    for key in sorted(contract_properties):
        if key not in _MIRRORED and key not in _CONTRACT_ONLY:
            reasons.append(
                f"dispatch_feed.properties key '{key}' is in neither _MIRRORED nor _CONTRACT_ONLY"
            )

    return reasons


# ---------------------------------------------------------------------------
# Leg 2 — staleness (the entire live-detection leg; see module docstring)
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def compute_staleness(
    captured_at_str: Any,
    capture_filename: str,
    *,
    max_age_days_capture: Any = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Pure predicate. Returns a dict with `reasons` (list[str], empty ==
    FRESH), `verdict` (FRESH/STALE), `days_since`, and `threshold_days` (the
    applied, possibly-shortened threshold — always present so a FAIL message
    can name it, precedent: `exec_summary.DocStalenessEntry.threshold_days`).

    Raises GateError if `captured_at_str` itself is missing/unparseable —
    that is "cannot run", not a staleness verdict.
    """
    today = today or date.today()
    captured_at = _parse_date(captured_at_str)
    if captured_at is None:
        raise GateError(f"capture captured_at '{captured_at_str}' missing or unparseable (YYYY-MM-DD)")

    reasons: list[str] = []

    match = _CAPTURE_FILENAME_RE.search(capture_filename)
    if match is None:
        reasons.append(
            f"capture filename '{capture_filename}' does not match the expected "
            "workflow-tool-api-capture.<YYYY-MM-DD>.json shape — cannot cross-check captured_at"
        )
    elif match.group(1) != captured_at_str:
        reasons.append(
            f"filename date '{match.group(1)}' disagrees with captured_at '{captured_at_str}' "
            "— tamper check failed"
        )

    effective_max_age = _MAX_AGE_DAYS
    if isinstance(max_age_days_capture, (int, float)) and not isinstance(max_age_days_capture, bool):
        if max_age_days_capture > 0:
            effective_max_age = min(_MAX_AGE_DAYS, max_age_days_capture)

    days_since = (today - captured_at).days
    if days_since < 0:
        days_since = 0

    stale = days_since > effective_max_age
    if stale:
        reasons.append(
            f"capture is {days_since}d old (captured_at={captured_at_str}), exceeds "
            f"threshold_days={effective_max_age}"
        )

    return {
        "verdict": "STALE" if stale else "FRESH",
        "reasons": reasons,
        "days_since": days_since,
        "threshold_days": effective_max_age,
    }


# ---------------------------------------------------------------------------
# Leg 3 — C7 extension: subagent-catering-resolution.json source_hashes
# ---------------------------------------------------------------------------


def compute_hash_drift(
    source_root: "str | Path",
    hash_algorithm: Any,
    source_hashes: dict[str, str],
) -> list[str]:
    """Pure(-ish; reads files) predicate. Returns a list of FAIL reason
    strings (empty == leg clean).

    Raises GateError if `hash_algorithm` is missing or unsupported by
    `hashlib` — that is "cannot run", not a per-file drift finding.
    """
    if not isinstance(hash_algorithm, str) or not hash_algorithm:
        raise GateError("subagent-catering-resolution.json missing 'hash_algorithm'")
    try:
        hashlib.new(hash_algorithm)
    except (ValueError, TypeError) as exc:
        raise GateError(f"unsupported hash_algorithm '{hash_algorithm}': {exc}") from exc

    source_root = Path(source_root).resolve()
    reasons: list[str] = []
    for rel_path in sorted(source_hashes):
        expected = source_hashes[rel_path]
        file_path = (source_root / rel_path).resolve()
        if not file_path.is_relative_to(source_root):
            raise GateError(f"source_hashes entry '{rel_path}' resolves outside {source_root}")
        if not file_path.is_file():
            reasons.append(f"source_hashes entry '{rel_path}' not found under {source_root}")
            continue
        digest = hashlib.new(hash_algorithm)
        digest.update(file_path.read_bytes())
        actual = digest.hexdigest()
        if actual.lower() != str(expected).lower():
            reasons.append(
                f"source_hashes entry '{rel_path}' hash mismatch: expected {expected}, got {actual}"
            )
    return reasons


# ---------------------------------------------------------------------------
# File resolution + JSON loading
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise GateError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"malformed JSON in {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise GateError(f"non-UTF-8 content in {path}: {exc}") from exc


def _find_capture_file(schemas_dir: Path) -> Path:
    """Most recent `workflow-tool-api-capture.<date>.json` under
    *schemas_dir* — ISO dates sort lexicographically, so the
    lexicographically-greatest match is the most recent capture. Raises
    GateError if none exist."""
    candidates = sorted(schemas_dir.glob("workflow-tool-api-capture.*.json"))
    if not candidates:
        raise GateError(f"no workflow-tool-api-capture.*.json found under {schemas_dir}")
    return candidates[-1]


def _extract_dispatch_feed_properties(contract: Any, contract_path: Path) -> "set[str]":
    try:
        props = contract["properties"]["dispatch_feed"]["properties"]
    except (KeyError, TypeError) as exc:
        raise GateError(
            f"{contract_path}: properties.dispatch_feed.properties not found (unexpected shape)"
        ) from exc
    if not isinstance(props, dict):
        raise GateError(f"{contract_path}: properties.dispatch_feed.properties is not an object")
    return set(props.keys())


def _extract_capture_opts_fields(capture: Any, capture_path: Path) -> "set[str]":
    try:
        opts = capture["script_globals"]["agent"]["opts_fields"]
    except (KeyError, TypeError) as exc:
        raise GateError(
            f"{capture_path}: script_globals.agent.opts_fields not found (unexpected shape)"
        ) from exc
    if not isinstance(opts, dict):
        raise GateError(f"{capture_path}: script_globals.agent.opts_fields is not an object")
    return set(opts.keys())


def run_gate(doe_root: "str | Path", *, today: Optional[date] = None) -> list[str]:
    """Runs all three legs against a resolved DoE-claude clone root (or any
    directory shaped like one — tests point this at a synthetic tmp_path
    fixture, never the live clone). Returns a flat list of report lines:
    empty == PASS. Raises GateError for any "cannot run" condition."""
    doe_root = Path(doe_root)
    schemas_dir = doe_root / "coordinator" / "schemas"

    contract_path = schemas_dir / "run-report.schema.json"
    resolution_path = schemas_dir / "subagent-catering-resolution.json"

    if not contract_path.is_file():
        raise GateError(f"contract schema not found: {contract_path}")
    if not resolution_path.is_file():
        raise GateError(f"C7 resolution file not found: {resolution_path}")

    capture_path = _find_capture_file(schemas_dir)

    contract = _load_json(contract_path)
    capture = _load_json(capture_path)
    resolution = _load_json(resolution_path)

    if not isinstance(capture, dict):
        raise GateError(f"{capture_path}: top-level JSON is not an object")
    if not isinstance(resolution, dict):
        raise GateError(f"{resolution_path}: top-level JSON is not an object")

    contract_props = _extract_dispatch_feed_properties(contract, contract_path)
    capture_opts = _extract_capture_opts_fields(capture, capture_path)

    lines: list[str] = []

    drift_reasons = compute_contract_capture_drift(contract_props, capture_opts)
    if drift_reasons:
        lines.append("LEG 1 (contract-vs-capture drift):")
        lines.extend(f"  - {reason}" for reason in drift_reasons)

    staleness = compute_staleness(
        capture.get("captured_at"),
        capture_path.name,
        max_age_days_capture=capture.get("max_age_days"),
        today=today,
    )
    if staleness["reasons"]:
        lines.append(f"LEG 2 (staleness, threshold_days={staleness['threshold_days']}):")
        lines.extend(f"  - {reason}" for reason in staleness["reasons"])

    source_hashes = resolution.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise GateError(
            f"{resolution_path}: source_hashes missing, empty, or not an object"
        )

    hash_reasons = compute_hash_drift(
        doe_root,
        resolution.get("hash_algorithm"),
        source_hashes,
    )
    if hash_reasons:
        lines.append("LEG 3 (C7 source-hash drift, subagent-catering-resolution.json):")
        lines.extend(f"  - {reason}" for reason in hash_reasons)

    return lines


def main(argv: list[str]) -> int:  # noqa: ARG001 — no flags today; argv reserved for CLI parity
    doe_root = resolve_doe_clone()
    if not doe_root:
        print("ERROR: DoE clone unresolvable (REPO_DOE_CLAUDE / repos.doe_claude not set)", file=sys.stderr)
        return EXIT_ERROR
    if not Path(doe_root).is_dir():
        print(f"ERROR: DoE clone path does not exist: {doe_root}", file=sys.stderr)
        return EXIT_ERROR

    try:
        lines = run_gate(doe_root)
    except GateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if lines:
        print("FAIL: pcli-04 drift gate found issues:")
        for line in lines:
            print(line)
        return EXIT_FAIL

    print("PASS: pcli-04 drift gate — contract-vs-capture, staleness, and C7 hash legs all clean.")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
