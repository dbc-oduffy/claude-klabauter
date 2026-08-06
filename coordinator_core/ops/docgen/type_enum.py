"""coordinator_core.ops.docgen.type_enum — internal doc-type enumeration (C3).

Purpose: read example-doctrine-repo's authored ``coordinator-registry.manifest.json`` — the SAME
manifest ``coordinator-doc-new`` itself consumes via ``bin/lib/coordinator_registry
.py`` — and reconstruct its ``KNOWN_TYPES``/``DOC_TYPES`` derivation as plain data,
then union in the one known non-manifest supplement the CLI applies locally
post-import. This is an INTERNAL MODULE FUNCTION, NOT an IPC op: no
``@register_op``, no ``OpClass`` entry, no ``_OP_KEY_SCOPE`` entry, no
``OP_MODULE_MAP`` entry anywhere in this package (see the package docstring and
DR-221's zero-op-registration beachhead cut). C2's ``template_format
.available_template_types()`` is a DIFFERENT, narrower thing — a disk-enumeration
convenience over the 22 *extracted templates* this plan stages; this module's
``known_types()`` is the full manifest-derived registry (31 entries: 28 docTypes
+ 3 queueTypes), matching every ``--type``/queue value ``coordinator-doc-new``
itself recognizes, whether or not this plan has extracted a template for it yet
(``lesson``, ``workflow``, ``spike-result``, and the 3 queue-delegate types are
manifest-known but carry no local template — that is expected, not a defect;
do not conflate the two surfaces).

Manifest read (no vendoring): resolved live from the example-doctrine-repo clone via the SAME
``resolve_doe_clone()`` bootstrap-safe pattern ``coordinator_core.ops.emit
.doe_drift`` already uses for the cockpit-contract conformance fixture — both
sides cannot drift together unnoticed, and there is no second resolution
mechanism to keep in sync.

Reconstruction formula (mirrors ``bin/lib/coordinator_registry.py`` verbatim,
per the manifest's own ``_reconstruction`` key):
  KNOWN_TYPES = {d["type"] for d in docTypes} | set(queueTypes)

CLI-local supplement (AC4): ``coordinator-doc-new`` unions ``"run-report"`` in
locally, post-import, because at the time that shim was written the manifest did
not yet carry a ``run-report`` docType entry (gated on
docs/plans/2026-07-13-subagent-run-report-subsume.md § C8a). The manifest now
DOES carry a ``run-report`` entry, making the union idempotent on current data —
but the shim itself is still live in ``coordinator-doc-new`` (harmless-idempotent
per its own comment, not yet retired), so this module reproduces the same union
rather than assuming C8a has landed. ``SUPPLEMENTAL_TYPES`` is the single place
that shim is mirrored; if the CLI ever unions a second ad-hoc type, add it here
too and cite the same spec backlink the CLI comment carries.

A missing type is a fail-loud condition (AC4: set-equality, not subset) —
enforced by the conformance test in ``tests/test_type_enum.py``, which loads
``bin/lib/coordinator_registry.py`` directly from the live example-doctrine-repo clone and asserts
this module's ``known_types()`` is set-EQUAL (not merely a subset) to that
module's own ``KNOWN_TYPES | frozenset({"run-report"})`` (the CLI's actual
post-union resolved set) — a silently absent type here would otherwise produce
no failing conformance case in C6's byte-identity harness (which only iterates
the 22 extracted templates), so this module's own test is where AC4 is proven.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C3 (AC4)
Oracle: /coordinator/bin/lib/coordinator_registry.py (example-doctrine-repo clone) — KNOWN_TYPES/
DOC_TYPES/QUEUE_TYPES derivation; /coordinator/bin/coordinator-doc-new — the
local ``run-report`` union shim (module-level, right after the ``coordinator_registry``
import).
Negative-spec: this module does not write files, does not shell out, and does
not register any op. It also does not vendor a manifest copy — every read
re-resolves the live example-doctrine-repo clone, so a manifest change is visible immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coordinator_core.ops.emit.doe_drift import resolve_doe_clone

# Path inside the example-doctrine-repo clone where the shared doc-type/identity manifest lives —
# the SAME file bin/lib/coordinator_registry.py loads at import time.
_MANIFEST_REL = Path("coordinator") / "schemas" / "coordinator-registry.manifest.json"

# The non-manifest supplements coordinator-doc-new unions in locally, post-import
# (module-level lines immediately after its `from coordinator_registry import ...`).
# Mirror ANY future CLI-local union here, citing the same spec backlink the CLI
# comment carries, so this module never silently falls behind that shim.
#
# Spec backlink (run-report shim origin): docs/plans/2026-07-13-subagent-run-report-subsume.md § C4, C8a
#
# subagent-sidecar formerly lived here too (docs/plans/2026-07-24-canonical-
# resolution-engine.md § W2-B3); the manifest now carries a "subagent-sidecar"
# docTypes entry (schemas/coordinator-registry.manifest.json), so it is
# already present in from_doc_types below and the local union would be
# redundant, not additive — removed rather than kept as a harmless no-op, so
# this frozenset stays an accurate record of what's still manifest-absent.
SUPPLEMENTAL_TYPES: frozenset[str] = frozenset({"run-report"})


class ManifestReadError(RuntimeError):
    """Raised when the example-doctrine-repo-authored manifest cannot be resolved, read, or parsed.

    Distinct from ``coordinator_core.ops.emit.doe_drift.DoeResolveError`` (which
    that module raises for its own clone-resolution failures) so callers of this
    module see a docgen-scoped exception type without needing to import
    ``doe_drift``'s error class to catch a docgen-shaped failure.
    """


def _manifest_path(doe_clone: Path | None = None) -> Path:
    """Resolve the manifest's absolute path inside the (live-resolved) example-doctrine-repo clone."""
    clone = doe_clone if doe_clone is not None else resolve_doe_clone()
    return clone / _MANIFEST_REL


def load_manifest(doe_clone: Path | None = None) -> dict[str, Any]:
    """Read + parse the example-doctrine-repo-authored registry manifest. Raises ManifestReadError on defect.

    No vendoring: reads the live example-doctrine-repo clone's HEAD-on-disk manifest every call —
    the anti-drift guarantee this module shares with ``doe_drift.read_doe_fixture``.
    """
    path = _manifest_path(doe_clone)
    if not path.exists():
        raise ManifestReadError(
            f"example-doctrine-repo registry manifest not found at {path}. "
            "Expected schemas/coordinator-registry.manifest.json in the example-doctrine-repo clone."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestReadError(f"example-doctrine-repo registry manifest at {path} is not readable/valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestReadError(f"example-doctrine-repo registry manifest at {path} must parse to a JSON object")
    for key in ("docTypes", "queueTypes"):
        if key not in data:
            raise ManifestReadError(f"example-doctrine-repo registry manifest at {path} is missing required key {key!r}")
    return data


def doc_types(doe_clone: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Return the raw ``docTypes`` entries (schemaName/offerable/isSidecar/etc.) as data.

    Mirrors ``bin/lib/coordinator_registry.py``'s ``DOC_TYPES`` — the tuple form
    callers needing per-type fields (``schemaName``, ``offerable``, ``isSidecar``,
    ``excludeReason``) read, rather than re-deriving from ``known_types()`` alone.
    """
    manifest = load_manifest(doe_clone)
    return tuple(manifest["docTypes"])


def queue_types(doe_clone: Path | None = None) -> frozenset[str]:
    """Return the manifest's ``queueTypes`` as a frozenset (improvement-queue/bug-backlog/debt-backlog)."""
    manifest = load_manifest(doe_clone)
    return frozenset(manifest["queueTypes"])


def known_types(doe_clone: Path | None = None) -> frozenset[str]:
    """Return the full recognized ``--type``/queue-type registry, manifest ∪ supplements.

    Reconstruction (byte-for-byte mirror of ``coordinator_registry.KNOWN_TYPES``):
        {d["type"] for d in docTypes} | set(queueTypes)
    then unioned with ``SUPPLEMENTAL_TYPES`` — the same local shim
    ``coordinator-doc-new`` applies post-import (AC4: this union must be
    idempotent-or-additive, never lossy, against the CLI's own resolved set).
    """
    manifest = load_manifest(doe_clone)
    from_doc_types = frozenset(d["type"] for d in manifest["docTypes"])
    from_queue_types = frozenset(manifest["queueTypes"])
    return from_doc_types | from_queue_types | SUPPLEMENTAL_TYPES


def sidecar_types(doe_clone: Path | None = None) -> frozenset[str]:
    """Return the subset of ``known_types()`` that are plan sidecars (``isSidecar: true``)."""
    manifest = load_manifest(doe_clone)
    return frozenset(d["type"] for d in manifest["docTypes"] if d.get("isSidecar"))


def schema_name_for(doc_type: str, doe_clone: Path | None = None) -> str | None:
    """Return the manifest ``schemaName`` for a given doc_type, or None if unset/absent.

    Raises ManifestReadError (via ``doc_types()``) on manifest read failure; returns
    plain ``None`` — not an exception — for a doc_type whose manifest entry sets
    ``schemaName: null`` (e.g. ``memo``, ``workflow``) or that is not a manifest key
    at all, since "no schema" is itself a legitimate manifest-declared state.
    """
    for entry in doc_types(doe_clone):
        if entry.get("type") == doc_type:
            return entry.get("schemaName")
    return None


def is_offerable(doc_type: str, doe_clone: Path | None = None) -> bool:
    """Return the manifest ``offerable`` flag for a doc_type; False if the type is unknown."""
    for entry in doc_types(doe_clone):
        if entry.get("type") == doc_type:
            return bool(entry.get("offerable", False))
    return False
