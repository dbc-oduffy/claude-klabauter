"""
coordinator_core.ops.fleet.capability_index — "fleet.aggregate_capability_index" op.

Purpose: aggregate every registered sibling repo's AUTHORED capability manifest
(``state/capabilities/manifest.json``, DoE-schema ``capability-manifest.schema.json``)
into ONE persisted fleet index (``state/capabilities/fleet-index.json``, DoE-schema
``fleet-capability-index.schema.json``) — the engine-tier read-aggregation makima
was asked to build per DR-047/DR-059 (engine-tier read-aggregation is makima-owned).

Origin: cross-repo/archive/2026-07-18-claude-central-em-fleet-capability-aggregation-op.md
(the ask) and state/handoffs/2026-07-21_190702_fleet-capability-aggregation-op.md (the
spinoff that gated the build on three DoE unblock artifacts, since cleared: both schemas
published+frozen with fixtures, the memo formally sent+PM-relayed, and a real authored
manifest exemplar at project-rag's own state/capabilities/manifest.json).

Contract (DoE-authored, frozen — see the two vendored schemas in this package's
``schemas/`` subdirectory):
  - Schema 1 (``capability-manifest.schema.json``): the per-repo AUTHORED manifest this
    op reads verbatim, never infers/greps.
  - Schema 2 (``fleet-capability-index.schema.json``): the aggregated shape this op
    produces. Both are ``additionalProperties: false`` at every level.

Behaviour (see ``build_fleet_index`` for the full algorithm):
  1. Sibling enumeration via ``_memo_resolver.read_registry_repos()`` — the ONE
     canonical machine-local-registry reader; no second registry parser, no
     directory-scan fallback (mirrors that module's own negative-spec).
  2. Per-repo manifest read + Schema-1 validation; a missing manifest is a normal
     skip, an INVALID manifest is skipped-with-reason (never fatal, never poisons
     the index).
  3. Aggregation into Schema-2 shape, ``host_repo`` on every entry, INCLUDING the
     host repo's own entries (F1c — never filter self out).
  4. Index-level ``generated_at`` + its own ``ttl`` (distinct from any manifest's
     ``refresh_cadence``), overridable at the invocation seam via the ``ttl`` param.
  5. Fail-closed provenance/maturity downgrade — see ``_project_entry``'s docstring
     for the exact rule this op implements (no network/subprocess reachability
     probe is performed; the rule is a fully static, from-the-read-alone check).
  6. OSS-safe degrade: registry absent, zero active siblings, or zero manifests
     found still produces a schema-valid EMPTY index and the op call succeeds
     (no exception) — DoE's review pre-flight must never be blocked by this.
  7. Read-only against every sibling tree — the only filesystem write this op
     performs is the single ``state/capabilities/fleet-index.json`` path under the
     INVOKING repo's own worktree (see ``_fleet_aggregate_capability_index``).
  8. Registered through makima's existing op-invocation seam (``register_op`` +
     the four registration-quad surfaces: ``coordinator_core/authz/classification.py``,
     ``coordinator_core/op_scopes.py``, ``coordinator_core/ops/_registry_map.py``,
     ``coordinator_core/ops/__init__.py::_EAGER_OP_MODULES``), ``common_dir``-scoped
     like the sibling ``fleet.*`` archival ops — the caller's own repo (resolved via
     ``main_worktree_root(repo_root)``, since ``common_dir``-scoped handlers receive
     the git common dir, not the worktree root — see ``coordinator_core.ipc.
     resolve_op_repo_key``) is both the write target AND included as a host entry.

"Active sibling" resolution (the handoff's "status: active siblings" made concrete):
the machine-local registry carries NO literal ``status: active`` field on a ``repos.*``
entry — see ``_memo_resolver.read_registry_repos()``'s own contract. This op resolves
"active" as: a registered ``repos.*`` key whose value is a non-empty path that IS an
existing directory on this machine (``Path.is_dir()``). A registered-but-uncloned
sibling (path absent on disk) and an unregistered sibling both degrade to "not
enumerated this run" — silently, by construction, since a missing manifest read is
itself a normal skip; this op does NOT additionally report which registry keys were
skipped for that reason (unlike ``consumer_corpus_preflight``'s ``unresolvable``
bucket) because — unlike that module's fixed, curated ``FLEET_REPO_KEYS`` — every
``repos.*`` key is a candidate sibling here, so "not on disk" is exactly as
uninformative as "not registered at all": neither is an anomaly worth a dedicated
report bucket for this op's purpose (a best-effort read-aggregation, not a
completeness gate).

Negative-spec:
  - Does NOT fork a second registry parser or add a directory-scan fallback —
    ``_memo_resolver.read_registry_repos()`` is the ONE reader.
  - Does NOT write into any sibling repo's tree — read-only against every path
    this op enumerates other than the invoking repo's own
    ``state/capabilities/fleet-index.json``.
  - Does NOT perform network or subprocess reachability probes against any
    ``consume_seam`` — see ``_project_entry``'s docstring for the exact static
    rule this op substitutes.
  - Does NOT do heuristic/keyword extraction from wiki prose or
    ``version_highlights[]`` — the authored manifest is the only substrate read.
  - A genuine ``RegistryReadError`` (a PRESENT registry file that fails to parse,
    or ``tomllib`` unavailable) is NOT swallowed into the empty-index degrade — it
    propagates uncaught out of ``build_fleet_index``/the op handler. Only
    "registry absent" (``read_registry_repos()`` returning ``{}``, per that
    function's own contract) is the empty-degrade case.

Spec backlink: state/handoffs/2026-07-21_190702_fleet-capability-aggregation-op.md
               cross-repo/archive/2026-07-18-claude-central-em-fleet-capability-aggregation-op.md
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.install._shared import atomic_write_bytes
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.fleet._memo_resolver import (
    RegistryReadError,
    read_registry_repos,
    same_repo_path,
)

_LOG = logging.getLogger(__name__)

# Vendored DoE schemas live in coordinator_core/frontmatter/schemas/ — the one
# directory coordinator_core.frontmatter.schema_drift_watch globs, so a DoE-side
# change to either frozen contract surfaces on the daily drift watch by
# construction rather than needing this op to be remembered.
_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas"
_MANIFEST_SCHEMA_PATH = _SCHEMAS_DIR / "capability-manifest.schema.json"
_INDEX_SCHEMA_PATH = _SCHEMAS_DIR / "fleet-capability-index.schema.json"

# Index-level staleness budget default — distinct from any single manifest's own
# refresh_cadence (see module docstring point 4). Overridable per-call via the
# op's optional `ttl` param (the "invocation seam" override the spec asks for).
_DEFAULT_TTL = "P1D"

_MANIFEST_REL_PATH = ("state", "capabilities", "manifest.json")
_INDEX_REL_PATH = ("state", "capabilities", "fleet-index.json")

# Minimal ISO-8601 duration parser — supports the P#Y#M#W#D[T#H#M#S] subset the
# two schemas' own descriptions cite as the expected form (e.g. "P7D", "P1D").
# Y/M are approximated at 365/30 days respectively — adequate for a staleness
# comparison at day-scale cadences; this op never needs calendar-exact duration
# arithmetic. Returns None (never raises) for anything else, INCLUDING a "human
# cadence label" a manifest's refresh_cadence is explicitly permitted to carry
# (capability-manifest.schema.json's own description: "an ISO-8601 duration...
# or a human cadence label") — see _is_stale for how an unparseable cadence is
# treated (fail-closed, not "assume fresh").
_ISO8601_DURATION_RE = re.compile(
    r"^P(?!$)(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def _parse_iso8601_duration(raw: object) -> Optional[datetime.timedelta]:
    """Parse a subset of ISO-8601 durations into a timedelta, or None.

    Never raises. Returns None for anything not a non-empty string matching the
    supported subset — including deliberately-non-duration "human cadence
    label" refresh_cadence values the manifest schema permits (see
    _ISO8601_DURATION_RE's comment).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    match = _ISO8601_DURATION_RE.match(raw.strip())
    if not match:
        return None
    years, months, weeks, days, hours, minutes, seconds = (
        int(g) if g else 0 for g in match.groups()
    )
    return datetime.timedelta(
        days=years * 365 + months * 30 + weeks * 7 + days,
        hours=hours, minutes=minutes, seconds=seconds,
    )


def _parse_iso8601_datetime(raw: object) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 datetime string (as capability-manifest.schema.json's
    `generated_at` format:date-time requires) into a timezone-aware datetime, or
    None. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _is_stale(
    manifest_generated_at: object, refresh_cadence: object, build_time: datetime.datetime
) -> bool:
    """True when a manifest is past its own declared refresh_cadence, relative to
    build_time — OR when either value cannot be parsed at all.

    Fail-closed by construction: an unparseable generated_at or refresh_cadence
    means this op cannot POSITIVELY establish freshness, so it is treated as
    stale rather than silently assumed fresh. This is the one case where a
    manifest carrying a "human cadence label" (schema-permitted, see
    _parse_iso8601_duration's docstring) rather than a real ISO-8601 duration
    always downgrades its entries — there is no numeric comparison this op can
    perform against free text.
    """
    generated_dt = _parse_iso8601_datetime(manifest_generated_at)
    cadence_td = _parse_iso8601_duration(refresh_cadence)
    if generated_dt is None or cadence_td is None:
        return True
    return (generated_dt + cadence_td) < build_time


def _seam_self_verified_reachable(consume_seam: object) -> bool:
    """The ENTIRE "self-verified reachable" check this op performs — deliberately
    NOT a network/subprocess probe (explicitly out of scope per the memo's
    Contract terms: "Do NOT attempt network or subprocess reachability
    probes"). "Self-verified reachable" here reduces to the only thing a pure
    read of the manifest can establish: consume_seam is present as a non-empty
    string. capability-manifest.schema.json already requires `consume_seam` to
    be a non-empty string (minLength 1) for every schema-valid entry, so for any
    entry that reached this function post-validation this check is structurally
    a no-op (always True) — it exists so the "not self-verified reachable"
    downgrade trigger from the memo's Contract terms has a concrete, documented
    implementation rather than being silently dropped, and so a FUTURE entry
    shape (e.g. a schema widening that makes consume_seam nullable) does not
    silently start asserting reachability for a seam this op never checked.
    """
    return isinstance(consume_seam, str) and bool(consume_seam.strip())


def _project_entry(cap: dict, manifest: dict, build_time: datetime.datetime) -> dict:
    """Project one source manifest capability entry into an aggregated index entry.

    Provenance/maturity fail-closed rule (memo Contract terms, "Provenance-aware,
    fail-closed maturity" — implemented exactly as follows, since the memo asks
    this op to document the precise rule rather than leave it implicit):

      - `provenance` is passed through UNCHANGED — this op never rewrites it.
      - `maturity` is passed through UNCHANGED unless a downgrade trigger fires,
        and a downgrade NEVER strengthens maturity (no upgrade path exists here).
      - `absent` (the schema's tombstone value — the repo actively declaring it
        does NOT offer this capability) is NEVER downgraded further: rewriting a
        tombstone to "unverified" would misrepresent an explicit non-offer as an
        unverified live one.
      - For every other declared maturity, a downgrade to `unverified` fires
        when EITHER: (a) the source manifest is stale relative to its own
        refresh_cadence (`_is_stale`), OR (b) the seam is not self-verified
        reachable per `_seam_self_verified_reachable` (structurally a no-op for
        schema-valid entries — see that function's docstring).
      - Additionally, "generated"/"asserted" provenance fails closed HARDER than
        "curated" (memo: "provenance: generated or asserted fail closed HARDER
        on maturity"): a declared `maturity: "live"` is downgraded to
        `unverified` whenever provenance is "generated" or "asserted", even when
        neither trigger (a)/(b) above fires — a non-curated (machine-derived or
        bare-assertion) claim of "live" is never taken at face value the way a
        curated one is. A curated "live" claim survives untouched when both (a)
        and (b) pass.

    Returns a dict carrying exactly the 7 fields fleet-capability-index.schema.json's
    indexEntry requires — additionalProperties:false at that level, so no other
    key from the source entry survives the projection.
    """
    maturity = cap["maturity"]
    provenance = cap["provenance"]

    if maturity != "absent":
        stale = _is_stale(
            manifest.get("generated_at"), manifest.get("refresh_cadence"), build_time
        )
        seam_ok = _seam_self_verified_reachable(cap.get("consume_seam"))
        harder_fail_closed_live = provenance in ("generated", "asserted") and maturity == "live"
        if stale or not seam_ok or harder_fail_closed_live:
            maturity = "unverified"

    return {
        "capability_id": cap["capability_id"],
        "capability_class": cap["capability_class"],
        "capability_label": cap["capability_label"],
        "consume_seam": cap["consume_seam"],
        "maturity": maturity,
        "provenance": provenance,
        "host_repo": cap["host_repo"],
    }


def _read_manifest(repo_path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """Read + Schema-1-validate one repo's state/capabilities/manifest.json.

    Returns (manifest_dict, None) on success, (None, None) when the manifest
    file is simply absent (a NORMAL skip — most repos have not authored one
    yet), or (None, reason) when the file is present but unreadable,
    unparseable, not a JSON object, or fails schema validation — an ABNORMAL
    skip the caller records but does not treat as fatal (memo Contract terms:
    "An INVALID manifest must not abort the run or poison the index").
    """
    manifest_path = repo_path.joinpath(*_MANIFEST_REL_PATH)
    if not manifest_path.is_file():
        return None, None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{manifest_path}: unreadable/unparseable ({exc})"
    if not isinstance(raw, dict):
        return None, f"{manifest_path}: did not parse to a JSON object"
    try:
        errors = validate_frontmatter(raw, _MANIFEST_SCHEMA_PATH)
    except Exception as exc:  # noqa: BLE001 — a malformed manifest must never abort the run
        return None, f"{manifest_path}: schema validation raised ({exc})"
    if errors:
        return None, f"{manifest_path}: failed schema validation ({errors})"
    return raw, None


def _enumerate_repo_paths(worktree_root: Path) -> Dict[str, Path]:
    """Resolve every candidate repo path to read a manifest from.

    Sibling resolution: every registry.toml/registry.local.toml `repos.*` entry
    whose value is a non-empty path that IS an existing directory on this
    machine (see module docstring's "Active sibling resolution" section for why
    this — not a literal `status: active` field, which the registry does not
    carry). `RegistryReadError` (a PRESENT-but-corrupt registry file, or
    `tomllib` unavailable) propagates UNCAUGHT — a genuine defect, not the
    empty-degrade case (module negative-spec).

    Host inclusion: `worktree_root` (the invoking repo — the ONLY repo this op
    ever writes into) is ALWAYS included, deduped against any registry entry
    resolving to the same path via `same_repo_path` — so the host is never
    read twice, and is read even when it happens to be unregistered.

    De-duplication: two registry keys may alias the SAME checkout (observed live
    on this fleet); each distinct repo path is enumerated exactly once, so a
    repo's capabilities can never appear twice in the aggregated index.

    Returns {label: absolute_path}. `label` is the registry key ("repos.<x>")
    for a sibling, or the literal "__host__" for the (possibly-unregistered)
    invoking repo — used only for de-duplication and skip-reason messages,
    never surfaced on the wire.
    """
    repos: Dict[str, Path] = {}
    for key, path_str in sorted(read_registry_repos().items()):  # RegistryReadError propagates
        if not path_str:
            continue
        candidate = Path(path_str)
        if not candidate.is_dir():
            continue
        # Two registry keys legitimately alias one checkout (observed live:
        # repos.doe_claude and repos.example_doctrine_repo both resolve to the
        # DoE-claude clone). Reading that repo once per key would emit its
        # capabilities twice into entries[] — a duplicate a consumer computing
        # host_repo asymmetry (F1c) has no way to tell from two genuine offers.
        # Dedup on resolved path; the lowest-sorting key wins the label, so the
        # enumeration is deterministic across runs rather than dict-order-dependent.
        if any(same_repo_path(candidate, seen) for seen in repos.values()):
            continue
        repos[key] = candidate

    if not any(same_repo_path(worktree_root, p) for p in repos.values()):
        repos["__host__"] = worktree_root

    return repos


def build_fleet_index(
    worktree_root: Path,
    *,
    ttl: str = _DEFAULT_TTL,
    build_time: Optional[datetime.datetime] = None,
) -> Tuple[dict, List[str]]:
    """Build the aggregated fleet-capability index for `worktree_root`.

    Returns (index_dict, skipped_reasons) — index_dict conforms to
    fleet-capability-index.schema.json (validated by the caller before persist,
    see `_fleet_aggregate_capability_index`); skipped_reasons is a list of
    human-readable strings, one per repo whose manifest was present but invalid
    (see `_read_manifest`) — NEVER populated for a repo with simply no manifest
    file (a normal, silent skip).

    OSS-safe degrade: when `read_registry_repos()` returns `{}` (registry
    absent — its own documented "nothing configured" case, not an error) AND
    `worktree_root` itself has no manifest, this still returns a schema-valid
    index with `entries: []` — no exception, no special-cased branch; the
    aggregation loop below naturally produces zero entries.
    """
    build_time = build_time or datetime.datetime.now(datetime.timezone.utc)
    skipped: List[str] = []
    entries: List[dict] = []

    repos = _enumerate_repo_paths(worktree_root)
    for _label, repo_path in sorted(repos.items(), key=lambda kv: str(kv[1])):
        manifest, err = _read_manifest(repo_path)
        if err:
            skipped.append(err)
            continue
        if manifest is None:
            continue  # no manifest present — normal skip, not recorded
        for cap in manifest.get("capabilities", []):
            entries.append(_project_entry(cap, manifest, build_time))

    index = {
        "generated_at": build_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl": ttl,
        "entries": entries,
    }
    return index, skipped


@register_op("fleet.aggregate_capability_index")
def _fleet_aggregate_capability_index(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.aggregate_capability_index" handler.

    common_dir-scoped (coordinator_core/op_scopes.py) — `repo_root` here is the
    git COMMON dir for the invoking repo (per coordinator_core.ipc.
    resolve_op_repo_key's common_dir branch), not the worktree root; this
    handler resolves the real worktree via `main_worktree_root(repo_root)`,
    mirroring every other common_dir-scoped fleet.* op's own resolution.

    Params:
        ttl (str, optional): index-level staleness budget override (an
            ISO-8601 duration string, e.g. "P1D"). Defaults to _DEFAULT_TTL.
            Must be a non-empty string when supplied — a present-but-invalid
            value raises ValueError rather than being silently ignored.

    Returns:
        {"out": <abs path written>, "entry_count": int,
         "skipped": [str, ...], "generated_at": str, "ttl": str}

    Raises:
        ValueError: no resolvable repo_root, or an invalid `ttl` param.
        RegistryReadError: propagated from build_fleet_index — a genuine
            registry-read defect, never swallowed into the empty-index degrade.
        RuntimeError: the built index somehow fails its OWN schema (a defect in
            this op, never expected in normal operation) — checked before
            persisting so a schema-invalid file is never written to disk.
    """
    if repo_root is None:
        raise ValueError(
            "fleet.aggregate_capability_index: no resolvable repo (common_dir scope) — "
            "the invoking session must be inside a registered git repo"
        )
    worktree_root = main_worktree_root(repo_root)

    ttl = params.get("ttl", _DEFAULT_TTL)
    if not isinstance(ttl, str) or not ttl.strip():
        raise ValueError(
            f"fleet.aggregate_capability_index: ttl, when supplied, must be a "
            f"non-empty string, got {ttl!r}"
        )

    index, skipped = build_fleet_index(worktree_root, ttl=ttl)

    index_errors = validate_frontmatter(index, _INDEX_SCHEMA_PATH)
    if index_errors:
        raise RuntimeError(
            f"fleet.aggregate_capability_index: built index failed its own schema "
            f"(fleet-capability-index.schema.json) — this is a defect in this op, "
            f"never expected in normal operation: {index_errors}"
        )

    out_path = worktree_root.joinpath(*_INDEX_REL_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(index, indent=2, sort_keys=False) + "\n").encode("utf-8")
    atomic_write_bytes(out_path, payload)

    return {
        "out": str(out_path),
        "entry_count": len(index["entries"]),
        "skipped": skipped,
        "generated_at": index["generated_at"],
        "ttl": index["ttl"],
    }
