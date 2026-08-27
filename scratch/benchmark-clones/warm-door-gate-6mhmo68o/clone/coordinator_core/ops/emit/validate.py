"""
coordinator_core.ops.emit.validate — in-process JSON Schema validation against the VENDORED
contract pin.

Purpose: port bash ``validate_main_array`` (emit-cockpit-snapshot.sh) — validate an
entire entity array against the frozen cockpit-contract schema. The claude-klabauter engine validates
against a VENDORED PIN (DR § DD#3: "claude-klabauter-produce validates against a vendored pin … no
skew-exemption for the adjacent repo"), NEVER the coordinator live head.

Negative-spec — retired node/Zod validator (2026-07-21): this module used to shell out to a
per-call ``node`` subprocess running a vendored Zod validator script
(``_vendor/bin/lib/validate-cockpit-record.mjs`` against ``_vendor/cockpit-contract/dist/``).
That toolchain is DEAD — DoE commit ``7cca4d4c`` (2026-07-16) deleted the upstream
``cockpit-contract`` TS/Zod source and its build output wholesale, and the vendored
``node_modules`` tree was never present here, so the spawn failed with
``ERR_MODULE_NOT_FOUND: Cannot find package 'zod'`` on every real invocation — the node-spawn
call sites all had zero production callers (``validate_array`` was reachable only from tests
via skip-guarded fixtures that quietly degraded to "skip" rather than surfacing the breakage).
``validate_array`` now validates IN-PROCESS against the vendored, language-neutral JSON Schema
bundle using ``jsonschema`` (a declared dependency, not opportunistic — see pyproject.toml).
No node/subprocess prerequisite remains on this path.

Pin resolution (OQ-1 → vendored-pin): the pinned copy lives under this package's
``_vendor/`` tree:
    _vendor/cockpit-contract/schema/cockpit-contract.schema.json — bundle (.version source)
    _vendor/cockpit-contract/schema/<entity>.schema.json         — per-entity JSON Schema
All resolution is fail-loud with remediation if the pin is absent.

Version guards (plan § D4 / the Staff Engineer F5, reworked 2026-07-21):
    - The content-based version-desync guard now cross-checks the vendored schema bundle
      ``.version`` against CLAUDE-KLABAUTER'S OWN ``CONTRACT_VERSION``
      (``coordinator_core.contract.cockpit_schema.CONTRACT_VERSION``) — the emitter's own
      contract version, not a second vendored copy — and is ALWAYS fail-loud on mismatch,
      never a skip (see ``assert_version_consistency``).
    - NOT ported: the src/*.ts-newer-than-dist mtime freshness guard (nondeterministic across
      a fresh clone / vendor copy; the src/*.ts tree it compared against no longer exists).
      Replaced by a pin-integrity assertion (schema bundle + per-entity schema dir present).

Wiring status (Phase 5, 2026-07-21): this module's validation is NOT yet wired into
``envelope.build()`` — sections do not call ``validate_array`` on their own output today, and
the envelope does not validate each array as part of a normal ``build()``/``emit()`` pass. Do
not assume otherwise from this module's presence; wiring is a deliberately deferred, separate
decision (measured, not made, by the 2026-07-21 in-process-validation cutover).

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C1 / D4
Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — validate_main_array
  and the version guards.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Deferred (not optional) ``jsonschema`` import — DR-2026-07-21 lazy-ops fix.
# ---------------------------------------------------------------------------
# Negative-spec: this module briefly carried a TOP-LEVEL ``import jsonschema``
# (2026-07-21, same day). That made ``import coordinator_core.ops`` transitively
# HARD-REQUIRE jsonschema for every one of the ~55 op modules
# ``_eager_import_all()`` imports (ops/__init__.py) — including callers that never
# touch validation at all. On a machine where jsonschema resolves only from user
# site-packages (HOME-dependent), any subprocess spawned under a different HOME
# (e.g. this suite's ``conftest.py`` HOME-quarantine fixture, or a real CLI
# entry point invoked under a non-default HOME in production) died at collection/
# import time with ``ModuleNotFoundError: No module named 'jsonschema'`` — even
# for code paths that never call into this module's validators.
#
# This is now a DEFERRED import, not an OPTIONAL one: ``_jsonschema()`` is called
# at every point of use (never at module-import time), so merely importing this
# module — and therefore ``coordinator_core.ops`` — no longer requires the
# package. But the import itself is NOT wrapped in a try/except anywhere; if
# jsonschema is genuinely absent at the point of use, the bare ``import
# jsonschema`` inside ``_jsonschema()`` raises ``ModuleNotFoundError`` and it
# propagates uncaught — exactly as loud as the top-level import it replaces.
#
# Do NOT reintroduce the PRE-2026-07-21 shape this module used to have: an
# opportunistic, function-local ``import jsonschema`` wrapped in
# ``except ImportError: pass``, which silently degraded whole-array contract
# validation down to the weaker structural-only check with no signal that the
# stronger check never ran. That was a fail-open hole; this is not — the only
# thing deferred here is WHEN the import happens, never WHETHER a genuine
# absence is reported.
_jsonschema_module = None


def _jsonschema():
    """Import (once) and return the ``jsonschema`` module — deferred, not optional.

    Caches the imported module in a module-level global so every call site pays
    the import cost at most once per process, same as a top-level import would.
    Raises ``ModuleNotFoundError`` uncaught if jsonschema is genuinely not
    installed — see the module-level comment above this function for why this
    must never degrade to a silent skip.
    """
    global _jsonschema_module
    if _jsonschema_module is None:
        import jsonschema as _js

        _jsonschema_module = _js
    return _jsonschema_module


# ---------------------------------------------------------------------------
# Vendored-pin path resolution — anchored to THIS file, never coordinator live head.
# ---------------------------------------------------------------------------
_VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor"
_VENDOR_CONTRACT = _VENDOR_ROOT / "cockpit-contract"
_VENDOR_SCHEMA_DIR = _VENDOR_CONTRACT / "schema"
VENDOR_SCHEMA_BUNDLE = _VENDOR_SCHEMA_DIR / "cockpit-contract.schema.json"
class ContractPinError(RuntimeError):
    """Raised when the vendored cockpit-contract pin is missing or internally inconsistent.

    ``structurally_wedged = True`` is a duck-type marker consumed by
    ``coordinator_core.ipc.dispatch_message``: an op handler that raises an exception
    carrying this attribute gets the distinct ``STRUCTURAL_PIN_ERROR`` JSON-RPC code
    instead of the generic ``INTERNAL_ERROR`` one, and — downstream —
    ``coordinator_core.invoke.__main__`` selects a distinct process exit code (2) for it.
    The distinction matters because a ``ContractPinError`` is not a transient fault: the
    emitter is structurally incapable of emitting (CONTRACT_VERSION and the vendored
    bundle disagree) and this WILL recur on every subsequent invocation until the pin is
    remediated — unlike a transient dispatch fault (absent seam, timeout, transport
    hiccup), which may not recur on the next run. Any future sibling "cannot recover
    without an out-of-band remediation step" error should set this same attribute rather
    than inventing a parallel signal.

    Spec backlink: cross-repo/inbox/2026-07-22-example-cockpit-repo-em-cockpit-contract-version-desync-wedges-emit-cadence.md
    """

    structurally_wedged = True


class ValidationError(RuntimeError):
    """Raised when a record array fails JSON Schema validation against the pinned schema."""


def assert_pin_integrity() -> None:
    """Fail loud if the vendored contract SCHEMA pin is incomplete.

    Pin-integrity check (replaces the non-portable mtime freshness guard, plan § D4, and the
    pre-2026-07-21 dist/index.js + node-validator presence check — both retired artifacts of
    the dead Zod toolchain, see module docstring). The schema BUNDLE and at least one
    per-entity schema file MUST be present under ``_vendor/cockpit-contract/schema/``. A
    stripped / half-vendored install is a hard error with remediation, not a silent skip — a
    producer that cannot validate against its pin must not emit.
    """
    missing = []
    if not VENDOR_SCHEMA_BUNDLE.exists():
        missing.append(VENDOR_SCHEMA_BUNDLE)
    if not _VENDOR_SCHEMA_DIR.is_dir() or not any(_VENDOR_SCHEMA_DIR.glob("*.schema.json")):
        missing.append(_VENDOR_SCHEMA_DIR)
    if missing:
        raise ContractPinError(
            "claude-klabauter cockpit-contract vendored pin is incomplete — missing: "
            + ", ".join(str(p) for p in missing)
            + ". Re-run the vendor step (python bin/claude-klabauter-revendor-cockpit-contract.py) "
            f"to repopulate {_VENDOR_SCHEMA_DIR}."
        )


def read_schema_version() -> str:
    """Return the pinned schema bundle ``.version`` (bash:2751 SCHEMA_VERSION).

    Fail-loud when the bundle is absent or the ``.version`` field is missing/empty — mirrors
    the bash guard at :2745-2760.
    """
    if not VENDOR_SCHEMA_BUNDLE.exists():
        raise ContractPinError(
            f"vendored contract schema bundle not found: {VENDOR_SCHEMA_BUNDLE}. "
            "Re-run the vendor step (python bin/claude-klabauter-revendor-cockpit-contract.py)."
        )
    try:
        bundle = json.loads(VENDOR_SCHEMA_BUNDLE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractPinError(
            f"vendored contract schema bundle is not valid JSON: {VENDOR_SCHEMA_BUNDLE}: {exc}"
        ) from exc
    version = bundle.get("version")
    if not version:
        raise ContractPinError(
            f".version field missing or empty in {VENDOR_SCHEMA_BUNDLE}"
        )
    return str(version)


def assert_version_consistency() -> str:
    """Enforce the emitter-CONTRACT_VERSION-vs-vendored-bundle-.version guard.

    Reworked 2026-07-21 (see module docstring): the PRE-2026-07-21 form of this guard
    cross-checked TWO vendored copies against each other (a vendored ``src/index.ts``
    CONTRACT_VERSION vs. the vendored schema bundle ``.version``) and treated a missing
    ``src/index.ts`` as a non-fatal SKIP — once ``src/`` was deleted (DoE 7cca4d4c), that skip
    branch would have silently turned this guard into a permanent no-op, hiding the exact
    class of desync it exists to catch (DSR-2026-06-23-4 silent-break guard).

    The guard now cross-checks the vendored schema bundle's ``.version`` against CLAUDE-KLABAUTER'S OWN
    ``CONTRACT_VERSION`` (``coordinator_core.contract.cockpit_schema.CONTRACT_VERSION``) — the
    question that actually matters: is the emitter's own contract version consistent with the
    schema bundle it validates emitted records against? There is no non-fatal branch — any
    mismatch is ALWAYS fail-loud. Local import (not top-of-file) to keep this module's import
    graph minimal and avoid pulling the full ``cockpit_schema`` entity-model tree into every
    ``validate`` import; no cycle exists (``cockpit_schema`` does not import ``ops.emit``).

    Returns the resolved schema_version on success.
    """
    schema_version = read_schema_version()
    from coordinator_core.contract.cockpit_schema import CONTRACT_VERSION as _claude_klabauter_contract_version

    if schema_version != _claude_klabauter_contract_version:
        raise ContractPinError(
            "cockpit-contract version desync — claude-klabauter's own CONTRACT_VERSION="
            f"{_claude_klabauter_contract_version} but the vendored schema bundle .version="
            f"{schema_version}. The emitter's contract version and the schema bundle it "
            "validates emitted records against must match (DSR-2026-06-23-4 silent-break "
            "guard). Diagnose before remediating — WHICH side is stale determines the fix:\n"
            "  (a) DoE's RELEASE TAG is already at CONTRACT_VERSION, only our vendored copy "
            "lags — re-vendor: python bin/claude-klabauter-revendor-cockpit-contract.py\n"
            "  (b) DoE has regenerated at CONTRACT_VERSION and committed it, but the "
            "release tag has NOT been advanced onto that commit — a default re-vendor "
            "resolves the tag and so pulls the SAME stale version, failing identically. "
            "Re-vendor at the explicit commit instead (--ref <sha>, --ack-major after "
            "reviewing the printed delta); vendoring ahead of the tag is the sanctioned "
            "DR-203 reader-first window and the drift-check reports it as expected. Then "
            "memo DoE to run --advance-ref so the default ref is correct again.\n"
            "  (c) CONTRACT_VERSION was bumped here and DoE's bundle has NOT been "
            "regenerated at all — no DoE commit carries this version, so there is nothing "
            "to vendor at any ref. DoE's bundle is derived output of "
            "coordinator_core.contract.cockpit_schema.emit_schema, so it must be "
            "regenerated FIRST: python coordinator/bin/regen-cockpit-schema.py, commit it "
            "in DoE, then --advance-ref, then re-vendor here at that SHA.\n"
            "  (d) CONTRACT_VERSION was bumped in error — revert it. Never bump it DOWN to "
            "match a stale bundle: that silently un-lands whatever contract widening the "
            "bump was carrying.\n"
            "Discriminate by reading the version AT THE REF THE RE-VENDOR ACTUALLY PULLS, "
            "not DoE's working tree — the working tree routinely runs ahead of the release "
            "tag, and reading it instead reports (a) when the truth is (b), sending you "
            "into a re-vendor that fails identically:\n"
            "  git -C <doe-clone> show refs/tags/cockpit-contract-release:"
            "coordinator/cockpit-contract/schema/cockpit-contract.schema.json\n"
            "Tag == CONTRACT_VERSION -> (a). Tag stale but some DoE commit carries "
            "CONTRACT_VERSION (git log on that schema path) -> (b). No commit carries it "
            "-> (c) or (d)."
        )
    return schema_version


def contract_declares_backlog_history() -> bool:
    """Return True when the vendored contract declares a concrete ``backlog_history`` block.

    Purpose: contract-presence gate for backlog-history D9-hold decoupling (plan § Design
    decision → Option C). The block first appears in the vendored schema at whatever version
    the coordinator lands it (contract v2.7.0; DoE+PM convention call + the Director of Engineering review); this
    probe self-activates at that re-vendor without hardcoding any version number.

    Reads ``$defs['snapshot-envelope']['properties']['backlog_history']`` from the vendored
    bundle and returns True ONLY for a **concrete object shape** — a dict containing either
    a ``$ref`` key (pointer to a backlog-history ``$def``) or a non-empty ``"properties"``
    dict (inline object shape). Explicitly rejects the placeholder idiom
    ``{"anyOf":[{},{"type":"null"}]}`` already used in the 2.5.0 schema for
    ``narrative_views``, and rejects absent (None) or empty ``{}`` — both map to False
    (D9 safe default).

    Graceful ``.get()``-chaining: a missing ``$defs``, ``snapshot-envelope``, or
    ``properties`` key returns False (older bundle → D9 safe default), never raises.

    Spec backlink:
        docs/plans/2026-07-05-backlog-history-emit-gate-decouple.md § Design decision → Option C
    """
    if not VENDOR_SCHEMA_BUNDLE.exists():
        return False
    try:
        bundle = json.loads(VENDOR_SCHEMA_BUNDLE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return False
    # Review: code-reviewer — guard against non-dict bundle root (list/string/number produces
    # AttributeError on .get(); the docstring guarantees graceful .get()-chaining → False, never raises).
    if not isinstance(bundle, dict):
        return False

    bh_schema = (
        bundle
        .get("$defs", {})
        .get("snapshot-envelope", {})
        .get("properties", {})
        .get("backlog_history")
    )

    # Absent → False.
    if bh_schema is None:
        return False
    # Must be a dict — non-dict is not a concrete shape.
    if not isinstance(bh_schema, dict):
        return False
    # Empty dict → placeholder or stub → False.
    if not bh_schema:
        return False
    # Reject the {"anyOf":[{},{"type":"null"}]} placeholder idiom.
    if "anyOf" in bh_schema and "$ref" not in bh_schema and "properties" not in bh_schema:
        return False
    # Concrete shape: either a $ref (pointer) or a non-empty properties dict.
    if "$ref" in bh_schema:
        return True
    props = bh_schema.get("properties")
    if isinstance(props, dict) and props:
        return True
    return False


# Enums and the ref-null conditional, pinned from the vendored schema (Review: code-reviewer
# — slice1-F2/F3 — read directly from
# _vendor/cockpit-contract/schema/emission-scope.schema.json's per-repo `provenance` shape,
# not guessed). Retained because `validate_array` still reads these enums; the
# emission-scope projection pass that used to compose with them went with `artifact.emit`
# (PM cut 2026-08-22).
_SOURCE_KIND_ENUM = frozenset(
    {
        "github_graphql",
        "github_rest",
        "git_commit",
        "local_fs",
        "coordinator_artifact",
        "transcript_summary",
        "sec_edgar",
        # Review: code-reviewer — Finding 2 (2026-07-14 entity_anchor slice review) —
        # v2.17.0 SourceKind widen added code_comparison; the structural mirror had
        # drifted stale relative to the vendored enum.
        "code_comparison",
    }
)
_DERIVATION_ENUM = frozenset({"raw", "parsed", "rolled_up", "computed", "synthesized"})
# source_kind values whose provenance.ref MUST be non-null (a real ref pointer).
_SOURCE_KIND_REQUIRES_NON_NULL_REF = frozenset({"github_graphql", "github_rest", "git_commit"})
# source_kind values whose provenance.ref MUST be null (no ref pointer concept applies).
_SOURCE_KIND_REQUIRES_NULL_REF = frozenset(
    {
        "local_fs",
        "coordinator_artifact",
        "transcript_summary",
        "sec_edgar",
        # Review: code-reviewer — Finding 2 — code_comparison is in the vendored
        # isNonGit list (provenance.ts), so ref must be null for it too.
        "code_comparison",
    }
)


def _entity_schema_path(entity_name: str) -> Path:
    """Resolve the vendored per-entity JSON Schema file for ``entity_name``.

    Filenames mirror the entity name verbatim (e.g. ``"roadmap-dag-node"`` →
    ``roadmap-dag-node.schema.json``) — no translation table; the vendored ``schema/``
    directory's per-entity files were emitted 1:1 with the entity names callers already pass.
    """
    return _VENDOR_SCHEMA_DIR / f"{entity_name}.schema.json"


@functools.lru_cache(maxsize=1)
def _schema_registry():
    """Build a ``referencing.Registry`` over EVERY vendored per-entity schema file.

    Purpose: cross-file ``$ref`` resolution support for ``validate_array``, so a future
    vendored schema that DOES reference a sibling file (e.g. an entity ``$ref``-ing
    ``provenance-envelope.schema.json`` instead of inlining it) resolves correctly rather than
    raising ``jsonschema.exceptions.RefResolutionError``. Verified empirically (2026-07-21,
    this port): as of the 2.20.0 pin, NONE of the 29 vendored per-entity schema files contain
    a ``"$ref"`` key — the ``emit_schema.py`` port deliberately inlines nested shapes at the
    Zod-parity use-site (see that module's docstring, post-processing pass 1, "Inline, don't
    $ref") — so this registry is not exercised by any $ref today, but is real and complete for
    the day that changes, not a placebo.

    Each resource is registered under its bare filename (e.g. ``"goal.schema.json"``) since
    none of the vendored files declare a ``$id`` — a filename-relative ``$ref`` is the only
    resolution shape a same-directory sibling schema could plausibly use.

    ``maxsize=1``: the vendored ``schema/`` directory is a fixed set of files under one pin,
    read once per process; nothing here is monkeypatched per-test, so there is no path
    variance to key on.
    """
    from referencing import Registry, Resource

    resources = []
    for schema_file in sorted(_VENDOR_SCHEMA_DIR.glob("*.schema.json")):
        contents = json.loads(schema_file.read_text(encoding="utf-8"))
        resources.append((schema_file.name, Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@functools.lru_cache(maxsize=None)
def _compiled_entity_validator_for_path(schema_path: Path):
    """Build+cache a compiled ``jsonschema`` validator for ``schema_path``, ONCE per path.

    Perf: recompiling the validator/registry from the raw schema dict on every call is pure waste when
    ``validate_array`` runs once per entity ARRAY (not per record), so this cache amortizes
    the compile cost across every record in the array, and across every call for the same
    entity within a process. ``maxsize=None`` (unbounded): the vendored ``schema/`` directory
    holds a fixed, small (29-entity) set — there is no monkeypatch-per-test variance to bound
    against here.

    Keyed on ``schema_path`` (not ``entity_name``) — Review: code-reviewer, Finding 5. Keying on
    the resolved path means a future test that monkeypatches ``_VENDOR_SCHEMA_DIR`` to
    exercise a missing-schema-file ``ContractPinError`` path for a previously-validated
    entity name gets a cache MISS (fresh path key), not a stale cached validator for a name
    that used to resolve elsewhere — structurally safe rather than resting on "no test
    currently does this."
    """
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema = _jsonschema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, registry=_schema_registry())


def _compiled_entity_validator(entity_name: str):
    """Resolve ``entity_name`` to its vendored schema path and return the compiled validator.

    Thin existence-checking wrapper around ``_compiled_entity_validator_for_path`` (the
    actual cache) — see that function's docstring for the path-keyed caching rationale.

    Raises ``ContractPinError`` if no vendored schema file exists for ``entity_name`` — an
    unknown/mistyped entity name is a caller defect, not a producer-data defect.
    """
    schema_path = _entity_schema_path(entity_name)
    if not schema_path.exists():
        raise ContractPinError(
            f"no vendored JSON Schema for entity {entity_name!r} — expected {schema_path}. "
            "Check the entity name, or re-run the vendor step "
            "(python bin/claude-klabauter-revendor-cockpit-contract.py) if the pin is stale."
        )
    return _compiled_entity_validator_for_path(schema_path)


# Per-record violation cap for validate_array's diagnostic output (Review: code-reviewer —
# Finding 2). Bounds output volume for a record failing many independent checks at once;
# the cap is always stated in the raised message, never a silent truncation.
_MAX_VIOLATIONS_PER_RECORD = 10


def validate_array(records: list, entity_name: str, ctx=None) -> None:
    """Validate an entire entity-record array IN-PROCESS against the pinned JSON Schema.

    Negative-spec (2026-07-21): this function used to serialize the array to a temp JSON file
    and spawn ``node validate-cockpit-record.mjs --array <entity> <file>`` — a per-call subprocess
    against the vendored Zod build. That toolchain is dead (DoE 7cca4d4c retired the upstream
    TS/Zod source + build output; the vendored ``node_modules``/``zod`` dep tree was never
    present here — confirmed empirically via ``ERR_MODULE_NOT_FOUND``) and had zero production
    callers. Validation now runs entirely in-process against the vendored, language-neutral
    JSON Schema bundle via ``jsonschema`` — no ``node``/``subprocess``/``tempfile`` round-trip,
    no external runtime prerequisite.

    Parity: bash ``validate_main_array`` (:193-207) — validate the WHOLE array against one
    entity's schema; a single invalid record fails the whole call. Pin-integrity and version
    consistency are asserted before validation, exactly as before.

    ``ctx`` is accepted for interface uniformity with the section/envelope layer; it is not
    required here (validation is pin-relative, not repo-relative). Wiring status: sections do
    NOT call this directly today, and the envelope does not call it as part of a normal
    ``build()``/``emit()`` pass either — wiring ``validate_array`` into the envelope is a
    DELIBERATELY DEFERRED, separate decision (see module docstring, "Wiring status"); do not
    read this function's existence as evidence the envelope validates each array today.

    Raises ``ValidationError`` naming the entity and, for every invalid record, its array
    index, and for EVERY violation on that record (not just one representative one), the
    JSON-pointer-style path within the record, the schema violation message, and the
    offending value. Per-record violations are capped at ``_MAX_VIOLATIONS_PER_RECORD``
    (the cap is stated explicitly in the output — never a silent truncation) to bound
    output volume on a record failing many independent checks at once.

    Review: code-reviewer — Finding 2: a prior revision of this function used
    ``jsonschema.exceptions.best_match()``, which surfaces exactly ONE (the "most relevant")
    sub-error per record — a record failing on three independent grounds (missing required
    field AND wrong enum value AND bad date format, say) reported only one, forcing an
    iterative fix-rerun-fix cycle to discover the rest. This now collects every violation
    from ``validator.iter_errors(record)`` per record in one pass, which is what makes the
    "at least as diagnostic as the retired Zod validator's stderr output" claim (Zod's
    ``safeParse()`` returned the full ``.error.issues`` list) actually checkable and true.
    """
    assert_pin_integrity()
    assert_version_consistency()

    jsonschema = _jsonschema()
    validator = _compiled_entity_validator(entity_name)

    violations: list[str] = []
    for index, record in enumerate(records):
        errors = sorted(validator.iter_errors(record), key=jsonschema.exceptions.relevance)
        if not errors:
            continue
        shown = errors[:_MAX_VIOLATIONS_PER_RECORD]
        for error in shown:
            pointer = "/" + "/".join(str(part) for part in error.absolute_path)
            violations.append(
                f"  [{index}] at {pointer}: {error.message} (value={error.instance!r})"
            )
        omitted = len(errors) - len(shown)
        if omitted > 0:
            violations.append(
                f"  [{index}] ... {omitted} additional violation(s) omitted "
                f"(capped at {_MAX_VIOLATIONS_PER_RECORD} per record)"
            )

    if violations:
        raise ValidationError(
            f"cockpit-contract validation failed for entity {entity_name!r} "
            f"({len(violations)} violation(s) across {len(records)} record(s)):\n"
            + "\n".join(violations)
        )
