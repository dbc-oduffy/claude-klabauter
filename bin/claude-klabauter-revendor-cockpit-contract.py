"""
bin/claude-klabauter-revendor-cockpit-contract.py — repeatable cockpit-contract pin+bundle re-vendor
with an inline magnitude-aware MAJOR-delta ack gate.

Purpose: fail-closed, repeatable re-vendor of the DoE cockpit-contract bundle into claude-klabauter's
vendored pin at coordinator_core/ops/emit/_vendor/cockpit-contract/. Vendors ONLY the
language-neutral JSON Schema (schema/) — claude-klabauter binds to the wire contract, not to any
TS/Zod derivative.

Enforces the correctness invariants that previously lived only in lesson prose:
  - Full-SHA pin (never a short SHA; never ABSENT from the re-vendor path).
  - Consumer-visible-delta inline ack gate (reader-first invariant, fail-closed before any
    writes) — magnitude-aware: minor/additive/no-delta re-vendors proceed with NO gate;
    a MAJOR / consumer-visible shape-changing delta requires an explicit inline
    ``--ack-major`` flag. No on-disk sentinel file is used or required.
  - Direction-aware downgrade guard (fail-closed before any writes, independent of the
    shape-delta gate above): refuses whenever the incoming semver version is LOWER than
    the currently-vendored version, whether or not a shape delta also fired. Requires an
    explicit inline ``--allow-downgrade`` flag — deliberately NOT satisfied by
    ``--ack-major``, since the two flags answer different questions ("I reviewed the
    shape" vs "I intend to go backwards"). No on-disk sentinel file is used or required.
  - Byte-identity verify for schema/ against the pinned git ref.
  - Post-vendor run_drift_check() green.
  - Idempotent: re-running at the same ref with unchanged bundle exits 0.

Usage:
    python3 bin/claude-klabauter-revendor-cockpit-contract.py [--ref <sha-or-tag>] [--doe-clone <path>]
        [--ack-major] [--allow-downgrade] [--dry-run]

    A consumer-visible / MAJOR shape-changing delta (new property, placeholder→concrete,
    type/cardinality change, enum-narrowing, additive-optional widen) is detected inline
    by ``_detect_consumer_visible_delta``. If detected, the script refuses (non-zero exit)
    and prints the delta unless ``--ack-major`` is passed — an explicit, shown-the-delta
    operator acknowledgment. There is no sentinel file to place on disk.

    Independently, a version downgrade (incoming semver LOWER than the currently-vendored
    version) is detected inline by ``_detect_downgrade`` — this runs even when no shape
    delta fired at all, closing the hole where a pure version regression with zero shape
    change would otherwise sail through ungated. If detected, the script refuses (non-zero
    exit) unless ``--allow-downgrade`` is passed. ``--ack-major`` does NOT satisfy this gate
    — the two flags answer different questions and BOTH are required together to land an
    acked downgrade that also carries a shape delta.

Spec backlink: pln-producer-emit-hold-removal-rea-48bd64 § C3
Lesson: state/lessons/2026-07-05-re-vendoring-a-bundle-that-newly-declare.yaml

Negative-spec: this script does NOT read or require any on-disk sentinel file (the prior
``state/cockpit-revendor-pending-<version>`` primitive is retired). Do not reintroduce a
file-based gate under a new name — the gate is inline-only (``--ack-major`` /
``--allow-downgrade``).

Negative-spec (2026-07-23): ``--ack-major`` must NEVER be treated as sufficient to permit
a downgrade, and the downgrade check must NEVER be gated on ``_detect_consumer_visible_delta``
having fired. A downgrade usually also carries a shape delta (so it presents as an ordinary
MAJOR-delta refusal) but not always — a pure version regression with an otherwise-identical
bundle produces ``delta_detected is False`` and would sail through with no gate at all if
the downgrade check depended on that predicate. The two checks (``_detect_consumer_visible_
delta`` / ``_enforce_major_delta_gate`` vs ``_detect_downgrade`` / ``_enforce_downgrade_gate``)
are independent by construction — do not collapse them.

Negative-spec (2026-07-21): this script no longer vendors, builds, or byte/functional-
verifies ``src/`` or ``dist/``. Upstream DoE commit 7cca4d4c (2026-07-16) deleted the
entire cockpit-contract TS/Zod toolchain (``src/``, ``package.json``, ``pnpm-lock.yaml``,
``tsconfig*.json``, the pnpm build scripts) — only ``schema/`` (the JSON Schema files),
``conformance/``, ``DECISIONS.md``, and ``README.md`` remain upstream. Re-vendoring any
ref past that commit against the old src/+dist/ contract fails at the precondition check
before ever reaching a write. Do not reintroduce a ``src/`` precondition, a pnpm/node
``dist/`` build step, or a worktree-based build — the schema/ JSON files are the sole
authoritative wire format claude-klabauter consumes.
"""

from __future__ import annotations

import argparse
import enum
import json
import re
import subprocess
import sys
import warnings
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root + sys.path — must precede coordinator_core imports.
# ---------------------------------------------------------------------------
_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# coordinator_core imports — reused, NOT re-implemented (AC2).
# ---------------------------------------------------------------------------
from coordinator_core.ops.emit.doe_drift import (  # noqa: E402
    _CONTRACT_RELEASE_REF,
    _VENDOR_CONTRACT,
    PIN_SHA_FILE,
    DoeResolveError,
    DriftError,
    DriftWarning,
    resolve_doe_clone,
    run_drift_check,
)
# Review: code-reviewer (F1, F2) — removed unused `probe_freshness_ref`, `_FIXTURE_REL`
# imports; neither is referenced anywhere in this file.
# 2026-07-21: VENDOR_VALIDATOR_MJS (node/pnpm dist/ functional-verify import) removed —
# schema-only re-vendor has no dist/ to functionally verify. See module docstring
# negative-spec (7cca4d4c).

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Review: code-reviewer (2026-07-23 F2) — removed the dead `_STATE_DIR = _REPO_ROOT /
# "state"` constant; it had no reader in this module (a vestige of the retired
# on-disk sentinel-file gate the module docstring negative-spec forbids reintroducing).
_DOE_CONTRACT_REL = "coordinator/cockpit-contract"
_DOE_SCHEMA_REL = f"{_DOE_CONTRACT_REL}/schema"

# Remediation text — emitted verbatim when the inline MAJOR-delta ack gate fires.
# Negative-spec: this is an INLINE gate only. Do NOT re-introduce an on-disk sentinel
# file (in any name/form) as a precondition here — reader-first confirmation is now an
# explicit operator flag (--ack-major), not a pre-placed file.
_MAJOR_DELTA_GATE_REMEDIATION = """\
Consumer-visible (MAJOR / shape-changing) delta detected in the incoming cockpit-contract
bundle. One or more schema surfaces differ vs the currently-vendored bundle (new property,
placeholder→concrete, type/cardinality change incl. array↔object, enum-narrowing,
or additive-optional widen such as the 2.4.0→2.5.0 content_hash R5 case).

Detected surfaces:
{surfaces}

Before re-vendoring you MUST:
  1. Confirm all readers (cockpit, rag) have widened to accept the new shape.
  2. Re-run this script with --ack-major once you have reviewed the delta above and
     confirmed readers are ready.

This gate encodes the reader-first invariant: an un-acked re-vendor would silently
arm the producer to emit the new shape before readers widen — an array-vs-object
silent-drop at the consumer's ingest. --ack-major is an explicit, shown-the-delta
operator confirmation; it is never auto-applied.
Lesson: state/lessons/2026-07-05-re-vendoring-a-bundle-that-newly-declare.yaml
"""

# Remediation text — emitted verbatim when the direction-aware downgrade guard fires.
# Negative-spec: this gate is INDEPENDENT of --ack-major and of _detect_consumer_visible_delta.
# --ack-major means "I reviewed the shape change"; it says nothing about direction, so it
# must never satisfy this gate. Do not merge this gate's flag into --ack-major.
_DOWNGRADE_GATE_REMEDIATION = """\
Downgrade detected: the currently-vendored cockpit-contract is at {current}, but the
incoming ref resolves to {incoming} — an OLDER version.

This gate is independent of the consumer-visible-delta / --ack-major gate above:
--ack-major is an acknowledgment that you reviewed a SHAPE change, not that you intend
to move the vendored bundle backwards. It does not satisfy this gate.

A downgrade here is most often NOT what you want: origin's moving release tag can
legitimately lag the version already vendored locally (e.g. this ref was last vendored
directly from a newer SHA, and the release tag has not caught up). In that case the fix
is to pass a different --ref — an immutable per-version tag/alias if one is published,
or the specific SHA you actually intend to vendor — not to pass --allow-downgrade.

If you truly intend to move the vendored bundle backwards to {incoming}, re-run with:
  --allow-downgrade
(and --ack-major too, if a consumer-visible shape delta was also detected above — the
two flags are independent and BOTH are required to land an acked downgrade that also
changes shape).
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="claude-klabauter-revendor-cockpit-contract.py",
        description=(
            "Re-vendor the DoE cockpit-contract JSON Schema (schema/ only) into claude-klabauter's "
            "vendored pin. Enforces full-SHA pin, consumer-visible-delta inline ack gate "
            "(reader-first invariant), a direction-aware downgrade guard (independent of "
            "the ack gate), schema/ byte-identity verify, and post-vendor drift-check green."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Standard re-vendor at the cockpit-contract-release tag:\n"
            "  python3 bin/claude-klabauter-revendor-cockpit-contract.py\n\n"
            "  # Dry-run — check what would happen without writing anything:\n"
            "  python3 bin/claude-klabauter-revendor-cockpit-contract.py --dry-run\n\n"
            "  # Re-vendor a MAJOR / consumer-visible-delta bundle (after reviewing the\n"
            "  # printed delta):\n"
            "  python3 bin/claude-klabauter-revendor-cockpit-contract.py --ack-major\n\n"
            "  # Re-vendor a specific SHA:\n"
            "  python3 bin/claude-klabauter-revendor-cockpit-contract.py --ref <40-char-sha>\n\n"
            "  # Intentionally move the vendored bundle backwards (after confirming this\n"
            "  # is truly what you want, not a stale/lagging --ref):\n"
            "  python3 bin/claude-klabauter-revendor-cockpit-contract.py --allow-downgrade\n"
        ),
    )
    p.add_argument(
        "--ref",
        default=_CONTRACT_RELEASE_REF,
        metavar="SHA_OR_TAG",
        help=(
            "Ref (tag refspec or SHA) to vendor from the DoE origin. "
            f"Default: {_CONTRACT_RELEASE_REF!r}."
        ),
    )
    p.add_argument(
        "--doe-clone",
        metavar="PATH",
        help=(
            "Override the DoE clone path. Default: resolve via machine-local registry "
            "(repos.doe_claude in registry.local.toml / registry.toml). "
            "Reuses resolve_doe_clone() — never re-implements registry parsing (AC2)."
        ),
    )
    p.add_argument(
        "--ack-major",
        action="store_true",
        help=(
            "Explicit operator acknowledgment required when a consumer-visible / MAJOR "
            "shape-changing delta is detected in the incoming cockpit-contract bundle "
            "(new property, placeholder→concrete, type/cardinality change, enum-narrowing, "
            "or additive-optional widen). Review the printed delta before passing this flag — "
            "it is never auto-applied. No on-disk sentinel file is used or required."
        ),
    )
    p.add_argument(
        "--allow-downgrade",
        action="store_true",
        help=(
            "Explicit operator acknowledgment required when the incoming cockpit-contract "
            "version is LOWER (semver) than the currently-vendored version. Independent of "
            "--ack-major — a downgrade is refused even when --ack-major is passed, and this "
            "flag alone does not satisfy the shape-delta gate. Consider whether you actually "
            "want a different --ref (origin's release tag can legitimately lag the locally- "
            "vendored version) before reaching for this flag."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run steps 1–3 (clone resolve, fetch+rev-parse, delta detection) and report "
            "the re-vendor plan without writing any files or updating the pin."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _die(msg: str, code: int = 1) -> None:
    """Print error to stderr and exit."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _info(msg: str) -> None:
    print(msg, flush=True)


def _git(clone: Path, *args) -> subprocess.CompletedProcess:
    """Run git -C <clone> <args>, capturing all output. Does NOT auto-raise on non-zero."""
    return subprocess.run(
        ["git", "-C", str(clone)] + [str(a) for a in args],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _git_show_bytes(clone: Path, sha: str, rel_path: str) -> bytes:
    """Return raw bytes of <sha>:<rel_path> via git show. Fails loud on error."""
    r = _git(clone, "show", f"{sha}:{rel_path}")
    if r.returncode != 0:
        _die(
            f"git show {sha[:8]}:{rel_path} failed:\n"
            f"{r.stderr.decode(errors='replace').strip()}"
        )
    return r.stdout


def _list_tree_files(clone: Path, sha: str, subtree: str) -> list[str]:
    """Return file paths under <subtree> at <sha>, relative to <subtree>."""
    r = _git(clone, "ls-tree", "-r", "--name-only", sha, subtree)
    if r.returncode != 0:
        return []
    prefix = subtree.rstrip("/") + "/"
    result = []
    for line in r.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            result.append(line[len(prefix):])
    return [f for f in result if f]


# ---------------------------------------------------------------------------
# Step 2: fetch + rev-parse + precondition
# ---------------------------------------------------------------------------

def _fetch_and_resolve_sha(clone: Path, ref: str) -> str:
    """Fetch <ref> from origin and resolve to a 40-char PEELED COMMIT SHA (AC3).

    Peels via ``rev-parse FETCH_HEAD^{commit}`` (same convention as
    ``doe_drift.probe_freshness_ref``'s ``rev-parse <sha>^{}`` dereference) so the
    pin is ALWAYS a commit SHA regardless of whether <ref> is a branch, a lightweight
    tag, an annotated tag, or a raw SHA. For a branch/lightweight-tag/plain-commit ref
    this is a no-op (FETCH_HEAD already points at a commit); for an annotated tag
    (the default ``cockpit-contract-release`` ref, standard release practice) FETCH_HEAD
    is the tag OBJECT, and only the peel yields the commit.

    Negative-spec: does NOT record a bare ``rev-parse FETCH_HEAD`` — that would record
    the tag-object SHA for an annotated tag, which changes if the tag is ever re-cut
    even when the underlying commit is unchanged, spuriously reading as pin drift, and
    breaks ``doe_drift.py``'s ``merge-base --is-ancestor`` ancestry check (which requires
    a commit, not a tag object). This regressed once (2026-07-21 review finding) —
    do not reintroduce a bare FETCH_HEAD rev-parse here.
    """
    _info(f"  git fetch origin {ref!r} ...")
    fetch_r = _git(clone, "fetch", "origin", ref)
    if fetch_r.returncode != 0:
        _die(
            f"git -C {clone} fetch origin {ref!r} failed:\n"
            f"{fetch_r.stderr.decode(errors='replace').strip()}"
        )

    rp_r = _git(clone, "rev-parse", "FETCH_HEAD^{commit}")
    if rp_r.returncode != 0:
        _die(
            f"git rev-parse FETCH_HEAD^{{commit}} failed after fetch:\n"
            f"{rp_r.stderr.decode(errors='replace').strip()}"
        )
    sha = rp_r.stdout.decode(errors="replace").strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        _die(
            f"git rev-parse FETCH_HEAD^{{commit}} returned a non-40-char value: {sha!r}. "
            "Expected a full SHA. Check the ref and DoE clone."
        )
    return sha


def _check_preconditions(clone: Path, sha: str) -> None:
    """Assert the schema/ subtree exists at <sha> via cat-file -e (AC3).

    Negative-spec: does NOT check for src/ — upstream commit 7cca4d4c (2026-07-16)
    deleted the cockpit-contract TS/Zod toolchain; only schema/ is guaranteed present
    on any ref past that commit.
    """
    r = _git(clone, "cat-file", "-e", f"{sha}:{_DOE_SCHEMA_REL}")
    if r.returncode != 0:
        _die(
            f"Precondition FAILED: '{_DOE_SCHEMA_REL}' not found at SHA {sha!r} in {clone}.\n"
            "This SHA does not contain the cockpit-contract schema tree."
        )


# ---------------------------------------------------------------------------
# Step 3: consumer-visible delta detection + inline MAJOR-delta ack gate
# ---------------------------------------------------------------------------

def _read_incoming_version(clone: Path, sha: str) -> str:
    """Read the .version field from the incoming cockpit-contract.schema.json at <sha>."""
    path = f"{_DOE_SCHEMA_REL}/cockpit-contract.schema.json"
    content = _git_show_bytes(clone, sha, path)
    try:
        schema = json.loads(content)
    except json.JSONDecodeError as exc:
        _die(f"Cannot parse incoming cockpit-contract.schema.json: {exc}")
    version = schema.get("version")
    if not version:
        _die("Incoming cockpit-contract.schema.json has no .version field.")
    return str(version)


def _read_vendored_version() -> str | None:
    """Read the top-level .version field from the currently-vendored
    cockpit-contract.schema.json, if any.

    Returns None when there is nothing to compare against: no vendored bundle exists
    yet (fresh/greenfield vendor into an empty dir), or the vendored file exists but
    has no readable top-level ``version`` field. Both are treated identically by the
    downgrade guard — "nothing to compare" proceeds without gating, it is NOT the
    same as "a version is present but garbled" (see ``_detect_downgrade``, which fails
    loud on that case instead).

    Negative-spec: does NOT raise or exit on a missing/unreadable vendored file — the
    greenfield case must never fail loud (AC: "Do not fail loud on the greenfield case").
    """
    path = _VENDOR_CONTRACT / "schema" / "cockpit-contract.schema.json"
    if not path.exists():
        return None
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    version = schema.get("version")
    if not version:
        return None
    return str(version)


def _parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse a strict numeric MAJOR.MINOR.PATCH semver tuple for direction comparison.

    Returns None if <version> is not exactly three dot-separated non-negative integers
    (pre-release/build-metadata suffixes, garbage, etc. all parse as None).

    Negative-spec: comparison MUST use the returned numeric tuple, never the raw string
    — "10.0.0" < "9.0.0" lexically, which is exactly the trap a string compare here
    would fall into.
    """
    parts = version.split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


class DowngradeStatus(enum.Enum):
    """Tri-state outcome of ``_detect_downgrade``.

    Review: code-reviewer (2026-07-23 F1) — a bare bool collapsed two semantically
    different outcomes into the same ``False``: genuinely-verified-not-a-downgrade,
    and direction-unverifiable-but-proceeding-per-override (``--allow-downgrade`` or
    ``--dry-run``). ``main()`` then printed "is not older than vendored" on BOTH,
    directly contradicting the "cannot determine direction" WARNING that prints
    immediately above it on the unverifiable path. The tri-state lets ``main()`` print
    a distinct, truthful line for each outcome.
    """

    NOT_DOWNGRADE = "not_downgrade"
    DOWNGRADE = "downgrade"
    UNVERIFIABLE_OVERRIDDEN = "unverifiable_overridden"


def _detect_downgrade(
    current_version: str | None,
    incoming_version: str,
    allow_downgrade: bool,
    dry_run: bool = False,
) -> DowngradeStatus:
    """Return the tri-state downgrade-direction outcome for <incoming_version> vs
    <current_version> — see ``DowngradeStatus``.

    Direction-aware: numeric (major, minor, patch) tuple comparison, independent of
    ``_detect_consumer_visible_delta`` — this must be checked even when that predicate
    reports no shape delta (a pure version downgrade with zero shape change would
    otherwise sail through ungated).

    Returns ``DowngradeStatus.NOT_DOWNGRADE`` when <current_version> is None — no
    currently-vendored bundle to compare against (greenfield vendor); proceeding is
    correct here, not a gate-worthy event.

    When a vendored bundle exists but either version string does not parse as a strict
    MAJOR.MINOR.PATCH tuple, direction is unverifiable at exactly the moment this
    script is about to overwrite the vendored bundle:
      - <allow_downgrade> True → do NOT die. Emit a loud warning that direction could
        not be verified and is being overridden by explicit operator flag, then return
        ``DowngradeStatus.UNVERIFIABLE_OVERRIDDEN`` (proceed). Rationale:
        --allow-downgrade asserts "I accept moving backwards" — an unverifiable
        direction is strictly weaker than a KNOWN downgrade, so an operator who has
        already accepted the worst case has, by construction, also accepted the
        unknown case. One escape hatch, not two.
      - <allow_downgrade> False AND <dry_run> True → do NOT die either. --dry-run
        writes nothing and enforces no gate at all; killing a diagnostic-only run on
        this path would deny the operator the exact tool they'd reach for to
        investigate it. Emit a loud warning (distinct wording from the override case
        above — this is deferred enforcement, not an accepted override) and return
        ``DowngradeStatus.UNVERIFIABLE_OVERRIDDEN`` so the dry-run report can still
        complete and exit 0.
      - both False → fail loud (sys.exit), naming --allow-downgrade as the override.
        2026-07-23 review finding: this branch previously named --allow-downgrade as a
        way out but the parameter did not exist on this function, so the instruction
        was false and the operator had no real escape. It is now load-bearing.
    """
    if current_version is None:
        return DowngradeStatus.NOT_DOWNGRADE

    current_tuple = _parse_semver(current_version)
    incoming_tuple = _parse_semver(incoming_version)
    if current_tuple is None or incoming_tuple is None:
        if allow_downgrade:
            _info(
                "  WARNING: cannot determine re-vendor direction — currently-vendored "
                f"version {current_version!r} and/or incoming version "
                f"{incoming_version!r} does not parse as a strict MAJOR.MINOR.PATCH "
                "semver tuple. Proceeding because --allow-downgrade was explicitly "
                "passed: an unverifiable direction is strictly weaker than a KNOWN "
                "downgrade, and the operator has already accepted that worst case."
            )
            return DowngradeStatus.UNVERIFIABLE_OVERRIDDEN
        if dry_run:
            _info(
                "  WARNING: cannot determine re-vendor direction — currently-vendored "
                f"version {current_version!r} and/or incoming version "
                f"{incoming_version!r} does not parse as a strict MAJOR.MINOR.PATCH "
                "semver tuple. --dry-run makes no writes and enforces no gate — this "
                "is informational only. A real (non-dry-run) invocation will refuse "
                "unless --allow-downgrade is passed."
            )
            return DowngradeStatus.UNVERIFIABLE_OVERRIDDEN
        _die(
            "Cannot determine re-vendor direction: currently-vendored version "
            f"{current_version!r} and/or incoming version {incoming_version!r} does not "
            "parse as a strict MAJOR.MINOR.PATCH semver tuple. Direction is unverifiable "
            "exactly when this script is about to overwrite the vendored bundle — "
            "refusing rather than guessing. Re-run with --allow-downgrade once you have "
            "manually confirmed the incoming version is not a downgrade (these are "
            "machine-emitted semver strings, so this should be rare)."
        )

    return (
        DowngradeStatus.DOWNGRADE
        if incoming_tuple < current_tuple
        else DowngradeStatus.NOT_DOWNGRADE
    )


def _enforce_downgrade_gate(
    current_version: str,
    incoming_version: str,
    allow_downgrade: bool,
) -> None:
    """Reader-first inline guard for a detected version downgrade.

    Only called when ``_detect_downgrade`` has already fired. Gate passes iff
    ``allow_downgrade`` (``--allow-downgrade``) was explicitly passed; otherwise refuses
    loudly (non-zero exit).

    Negative-spec: does NOT accept ``ack_major``/``--ack-major`` as satisfying this
    gate — the two flags answer different questions ("I reviewed the shape" vs "I
    intend to go backwards") and collapsing them into one flag is the exact bug this
    gate exists to close. There is no on-disk sentinel file involved — do not
    reintroduce one.
    """
    if allow_downgrade:
        _info(
            f"  --allow-downgrade provided — downgrade {current_version} -> "
            f"{incoming_version} acknowledged, proceeding."
        )
        return

    _die(_DOWNGRADE_GATE_REMEDIATION.format(current=current_version, incoming=incoming_version))


# Semver-shaped substring (e.g. "2.17.0") — used to normalize the version stamp
# embedded in a schema's top-level `description` field before delta comparison.
_SEMVER_STAMP_RE = re.compile(r"\d+\.\d+\.\d+")


def _strip_version_stamp_churn(obj: dict) -> dict:
    """Return a shallow copy of a TOP-LEVEL schema object with pure version-stamp churn
    normalized out: drop the top-level ``version`` field, and blank any semver-shaped
    substring in the top-level ``description`` field.

    Purpose: every vendored schema file embeds ``"version": "<X.Y.Z>"`` and a
    ``"description"`` containing that same version substring (e.g. "... version
    2.17.0."). A version bump alone rewrites those two surfaces on ALL 29 schema
    files, which drowns the ``--ack-major`` gate's one job — surfacing real SHAPE
    changes — in noise the operator is trained to ack blindly (2026-07-21 review
    finding). Stripping them here means the gate fires only on genuine structural
    delta: new/removed properties, $defs changes, type/cardinality changes,
    enum-narrowing/widening, or added/removed schema files.

    Negative-spec: does NOT touch nested descriptions ($defs/properties/etc.) — only
    the TOP-LEVEL version/description fields are pure version-stamp churn. A NEW
    description added to a nested property (e.g. the 2.17.0→2.20.0 sha256 `pattern`
    properties gaining descriptions) is a real shape change and MUST still trip the
    gate; do not widen this normalization to strip nested description text.
    """
    if not isinstance(obj, dict):
        return obj
    normalized = dict(obj)
    normalized.pop("version", None)
    desc = normalized.get("description")
    if isinstance(desc, str):
        normalized["description"] = _SEMVER_STAMP_RE.sub("X.Y.Z", desc)
    return normalized


def _detect_consumer_visible_delta(
    clone: Path,
    sha: str,
) -> tuple[bool, list[str]]:
    """Diff consumer-visible surfaces: schema/*.schema.json (entity files, snapshot-envelope,
    cockpit-contract.schema.json including its $defs) vs currently-vendored bundle.

    Treats ANY structural change (new property, placeholder→concrete, type/cardinality incl.
    array↔object, enum-narrowing, or additive-optional widen) as requiring-ack — i.e. any JSON
    inequality on the named surfaces AFTER normalizing out pure version-stamp churn via
    ``_strip_version_stamp_churn`` (AC6 conservative predicate; 2026-07-21 refinement —
    the predicate is conservative on SHAPE, not on the version stamp that changes on
    every single bump regardless of shape).

    Returns (delta_detected, list_of_surface_descriptions).

    Negative-spec: does not diff dist/ or src/ — those are not consumer-visible contract
    surfaces; the schema/*.schema.json files are the authoritative wire format.

    Negative-spec (2026-07-21): a version-only bump (top-level ``version`` +
    ``description`` version-substring change, nothing else) produces `delta_detected
    is False` — --ack-major is NOT required. Do not regress this back to "any JSON
    inequality including pure version churn" — that trains operators to ack blindly.
    """
    incoming_files = _list_tree_files(clone, sha, _DOE_SCHEMA_REL)
    schema_files = [f for f in incoming_files if f.endswith(".schema.json")]

    vendored_schema_dir = _VENDOR_CONTRACT / "schema"
    vendored_names: set[str] = set()
    if vendored_schema_dir.exists():
        # Review: code-reviewer (F8) — use rglob with relative paths (not p.name) so that
        # subdirectory schemas (e.g. subdir/foo.schema.json from ls-tree -r) compare correctly
        # against incoming_names which also carries relative-path strings from _list_tree_files.
        vendored_names = {
            str(p.relative_to(vendored_schema_dir))
            for p in vendored_schema_dir.rglob("*.schema.json")
        }

    changed: list[str] = []

    for fname in schema_files:
        rel_path = f"{_DOE_SCHEMA_REL}/{fname}"
        incoming_bytes = _git_show_bytes(clone, sha, rel_path)
        try:
            incoming_obj = json.loads(incoming_bytes)
        except json.JSONDecodeError:
            changed.append(f"{fname} (parse error — treating as delta)")
            continue

        vendored_path = vendored_schema_dir / fname
        if not vendored_path.exists():
            changed.append(f"{fname} (new file — not in vendored bundle)")
            continue

        try:
            vendored_obj = json.loads(vendored_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            changed.append(f"{fname} (vendored parse error — treating as delta)")
            continue

        incoming_normalized = _strip_version_stamp_churn(incoming_obj)
        vendored_normalized = _strip_version_stamp_churn(vendored_obj)

        if incoming_normalized != vendored_normalized:
            # For cockpit-contract.schema.json, name $defs specifically (the highest-risk surface).
            if fname == "cockpit-contract.schema.json":
                incoming_defs = incoming_obj.get("$defs", {})
                vendored_defs = vendored_obj.get("$defs", {})
                if incoming_defs != vendored_defs:
                    changed.append(f"{fname} ($defs changed — highest-risk consumer surface)")
                else:
                    changed.append(f"{fname} (non-$defs change)")
            else:
                changed.append(fname)

    # Also flag schema files present in vendored but absent in incoming (removed entity).
    incoming_names = set(schema_files)
    for removed in vendored_names - incoming_names:
        if removed.endswith(".schema.json"):
            changed.append(f"{removed} (removed from incoming — structural change)")

    return bool(changed), changed


def _enforce_major_delta_gate(
    changed_surfaces: list[str],
    ack_major: bool,
) -> None:
    """Reader-first inline ack gate for a detected consumer-visible / MAJOR delta.

    Magnitude-aware: this function is only called when
    ``_detect_consumer_visible_delta`` has already fired (a consumer-visible delta is
    present). Gate passes iff ``ack_major`` (``--ack-major``) was explicitly passed;
    otherwise refuses loudly (non-zero exit), printing the detected delta so the
    operator can review it before re-running with ``--ack-major``.

    Negative-spec: there is no on-disk sentinel file involved in this gate — do not
    reintroduce one. Minor/additive/no-delta re-vendors never reach this function
    (the caller only invokes it when a delta was detected) and are never gated.
    """
    if ack_major:
        _info("  --ack-major provided — MAJOR delta acknowledged, proceeding.")
        return

    surfaces_block = "\n".join(f"  - {s}" for s in changed_surfaces)
    _die(_MAJOR_DELTA_GATE_REMEDIATION.format(surfaces=surfaces_block))


# ---------------------------------------------------------------------------
# AC9: idempotent check
# ---------------------------------------------------------------------------

def _find_stale_vendored_files(vendor_dir: Path, incoming_set: set) -> list[Path]:
    """Return vendored files under <vendor_dir> that are absent from <incoming_set>.

    Shared enumeration logic factored out of three call sites (idempotent-check,
    authoritative copy, byte-identity verify) that each need the same "which
    vendored files are stale relative to the incoming ref" answer but differ only
    in what they do with it (return False / unlink / _die).
    Review: code-reviewer (F6) — extracted to remove the triplicated rglob loop.
    """
    if not vendor_dir.exists():
        return []
    stale: list[Path] = []
    for existing in vendor_dir.rglob("*"):
        if existing.is_file():
            rel = str(existing.relative_to(vendor_dir))
            if rel not in incoming_set:
                stale.append(existing)
    return stale


def _is_already_vendored(clone: Path, sha: str) -> bool:
    """Return True if the current pin already equals <sha> AND schema/ is byte-identical.

    Any error (missing files, git failure) returns False — the re-vendor proceeds.

    Also checks for extra vendored files not present in the incoming ref — vendor dir
    must EXACTLY equal the incoming ref, not merely contain it (F1 / Review: code-reviewer).

    Negative-spec: does NOT check src/ — schema/ is the only vendored subtree (2026-07-21
    schema-only re-vendor; see module docstring negative-spec).
    """
    try:
        if not PIN_SHA_FILE.exists():
            return False
        current_pin = PIN_SHA_FILE.read_text(encoding="utf-8").strip()
        if current_pin != sha:
            return False

        files = _list_tree_files(clone, sha, _DOE_SCHEMA_REL)
        if not files:
            return False
        incoming_set = set(files)
        for fname in files:
            r = _git(clone, "show", f"{sha}:{_DOE_SCHEMA_REL}/{fname}")
            if r.returncode != 0:
                return False
            expected_bytes = r.stdout
            vendored_path = _VENDOR_CONTRACT / "schema" / fname
            if not vendored_path.exists():
                return False
            if vendored_path.read_bytes() != expected_bytes:
                return False
        # Review: code-reviewer (F1) — extra files in vendor dir mean the set does not
        # match the incoming ref; return False so re-vendor prunes the stale files.
        vendor_dir = _VENDOR_CONTRACT / "schema"
        if _find_stale_vendored_files(vendor_dir, incoming_set):
            return False

        return True
    except Exception as exc:  # noqa: BLE001
        _info(f"  Idempotent check inconclusive ({exc!r}) — proceeding with re-vendor.")
        return False


# ---------------------------------------------------------------------------
# Step 4: bundle copy
# ---------------------------------------------------------------------------

def _copy_schema(clone: Path, sha: str) -> None:
    """AC3: copy schema/ from git show <sha>:<path> into the vendored tree.

    Uses git-show per-file — never copies the DoE clone's dirty working tree.

    Authoritative replace (not additive): files absent from the incoming ref are pruned
    from the vendor dir so the vendored tree EXACTLY equals the incoming ref set.

    Negative-spec (2026-07-21): does NOT copy src/ or build/copy dist/ — upstream commit
    7cca4d4c deleted the TS/Zod toolchain; schema/ is the only vendored subtree. Do not
    reintroduce a src/ copy loop or a pnpm dist/ build step here.
    """
    files = _list_tree_files(clone, sha, _DOE_SCHEMA_REL)
    incoming_set = set(files)
    vendor_dir = _VENDOR_CONTRACT / "schema"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    # Review: code-reviewer (F1) — prune stale vendored files absent from the incoming ref.
    # An additive-only copy leaves removed files in the vendor dir, so the bundle no longer
    # matches the pinned SHA and _is_already_vendored cements the stale state on the next run.
    for stale in _find_stale_vendored_files(vendor_dir, incoming_set):
        stale.unlink()
    for fname in files:
        content = _git_show_bytes(clone, sha, f"{_DOE_SCHEMA_REL}/{fname}")
        dest = vendor_dir / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


# ---------------------------------------------------------------------------
# Step 5: verify
# ---------------------------------------------------------------------------

def _verify_schema_byte_identity(clone: Path, sha: str) -> None:
    """AC5: re-read each vendored schema/ file and assert byte-identity vs
    git show <sha>:<path>.

    Mismatch → fail loud, no pin write (caller must not proceed to AC4).
    Also asserts no extra files exist in the vendor dir — the vendored set must EXACTLY
    equal the incoming ref, not merely contain it (Review: code-reviewer F1).

    Negative-spec (2026-07-21): no longer verifies src/ (removed) or dist/ (there is no
    functional verify step — schema/ JSON has no build artifact to load). See module
    docstring negative-spec (7cca4d4c).
    """
    files = _list_tree_files(clone, sha, _DOE_SCHEMA_REL)
    incoming_set = set(files)
    vendor_dir = _VENDOR_CONTRACT / "schema"
    for fname in files:
        expected = _git_show_bytes(clone, sha, f"{_DOE_SCHEMA_REL}/{fname}")
        actual_path = vendor_dir / fname
        if not actual_path.exists():
            _die(
                f"AC5 byte-identity FAIL: vendored file missing after copy: {actual_path}\n"
                "Aborting — no pin write."
            )
        actual = actual_path.read_bytes()
        if actual != expected:
            _die(
                f"AC5 byte-identity MISMATCH: {actual_path}\n"
                f"  Expected {len(expected)} bytes (git show {sha[:8]}), "
                f"got {len(actual)} bytes on disk.\n"
                "The vendored copy does not match source at pinned ref. Aborting — no pin write."
            )
    # Review: code-reviewer (F1) — verify no extra files in vendor dir beyond the incoming
    # ref. Extra files mean the bundle does not match the pinned SHA.
    stale_files = _find_stale_vendored_files(vendor_dir, incoming_set)
    if stale_files:
        rel = str(stale_files[0].relative_to(vendor_dir))
        _die(
            f"AC5 stale-file FAIL: vendored schema/{rel} is not present "
            f"at pinned ref {sha[:8]} — vendor dir contains extra files.\n"
            "Aborting — no pin write."
        )


# ---------------------------------------------------------------------------
# Step 6: pin write
# ---------------------------------------------------------------------------

def _write_pin(sha: str) -> None:
    """AC4: write the 40-char SHA newline-terminated to PIN_SHA_FILE.

    Negative-spec: never writes ABSENT (bootstrap-only sentinel), never writes a short SHA.
    The SHA is always the one resolved from FETCH_HEAD (local, post-fetch).
    """
    PIN_SHA_FILE.write_text(sha + "\n", encoding="utf-8", newline="\n")
    try:
        _pin_display = PIN_SHA_FILE.relative_to(_REPO_ROOT)
    except ValueError:
        _pin_display = PIN_SHA_FILE  # out-of-tree pin path (e.g. test fixture) — show absolute
    _info(f"  Pin written: {_pin_display} <- {sha}")


# ---------------------------------------------------------------------------
# Step 7: post-vendor drift-check
# ---------------------------------------------------------------------------

def _post_vendor_drift_check(doe_clone: Path) -> None:
    """AC8: run run_drift_check(); non-green (DriftError or unexpected DriftWarning) → fail loud."""
    caught_drift_warnings: list[str] = []

    with warnings.catch_warnings(record=True) as w_list:
        warnings.simplefilter("always")
        try:
            run_drift_check(doe_clone=doe_clone)
        except DriftError as exc:
            _die(f"Post-vendor drift-check FAILED (DriftError):\n{exc}")

    for w in w_list:
        if issubclass(w.category, DriftWarning):
            caught_drift_warnings.append(str(w.message))

    # Review: code-reviewer (F9) — log UserWarning entries (e.g. version-band check skipped
    # when min_supported_contract_version is absent from the fixture) so the operator is
    # not silently denied that information.
    for w in w_list:
        if issubclass(w.category, UserWarning):
            _info(f"WARNING (post-vendor drift check): {w.message}")

    if caught_drift_warnings:
        _die(
            "Post-vendor drift-check FAILED (unexpected DriftWarning):\n"
            + "\n".join(caught_drift_warnings)
        )

    _info("  Post-vendor drift-check PASSED.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:  # noqa: C901  (complexity: deliberate — sequential steps are load-bearing)
    args = _parse_args()

    # ------------------------------------------------------------------
    # Step 1: Resolve DoE clone (AC2: reuse resolve_doe_clone(), never re-implement).
    # ------------------------------------------------------------------
    _info("[1] Resolving DoE clone...")
    if args.doe_clone:
        doe_clone = Path(args.doe_clone).expanduser().resolve()
        if not doe_clone.is_dir():
            _die(f"--doe-clone path does not exist or is not a directory: {doe_clone}")
        _info(f"  DoE clone (--override): {doe_clone}")
    else:
        try:
            doe_clone = resolve_doe_clone()
        except DoeResolveError as exc:
            _die(str(exc))
        _info(f"  DoE clone (registry): {doe_clone}")

    # ------------------------------------------------------------------
    # Step 2: Fetch-then-rev-parse (AC3) + precondition assertion.
    # ------------------------------------------------------------------
    _info(f"[2] Fetching and resolving SHA for ref {args.ref!r}...")
    sha = _fetch_and_resolve_sha(doe_clone, args.ref)
    _info(f"  SHA: {sha}")

    _info("[2b] Precondition: schema/ exists at SHA...")
    _check_preconditions(doe_clone, sha)
    _info("  Preconditions OK.")

    # ------------------------------------------------------------------
    # AC9: idempotent check — before delta detection (fast exit).
    # ------------------------------------------------------------------
    _info("[AC9] Idempotent check...")
    if _is_already_vendored(doe_clone, sha):
        _info(f"Already vendored at {sha} — no-op. Exiting 0.")
        sys.exit(0)
    _info("  Not idempotent — proceeding with re-vendor.")

    # Incoming contract version (reported in dry-run/ack-gate output).
    incoming_version = _read_incoming_version(doe_clone, sha)
    _info(f"  Incoming contract version: {incoming_version}")

    # ------------------------------------------------------------------
    # Direction-aware downgrade guard — INDEPENDENT of the shape-delta gate below.
    # Must run even when Step 3 finds no consumer-visible delta at all: a pure
    # version downgrade with zero shape change would otherwise sail through
    # ungated (2026-07-23 review finding, hole #2 — the wider of the two holes).
    # ------------------------------------------------------------------
    current_vendored_version = _read_vendored_version()
    downgrade_status = _detect_downgrade(
        current_vendored_version,
        incoming_version,
        args.allow_downgrade,
        dry_run=args.dry_run,
    )
    # Review: code-reviewer (2026-07-23 F1) — three distinct, truthful messages, one
    # per DowngradeStatus outcome. The prior two-way branch printed "is not older than
    # vendored" on the unverifiable-overridden path too, directly contradicting the
    # WARNING that _detect_downgrade had just printed one line above it.
    if downgrade_status is DowngradeStatus.DOWNGRADE:
        _info(
            f"  DOWNGRADE DETECTED: currently-vendored {current_vendored_version} -> "
            f"incoming {incoming_version}."
        )
    elif downgrade_status is DowngradeStatus.UNVERIFIABLE_OVERRIDDEN:
        _info("  Direction unverifiable — proceeding per override (see WARNING above).")
    else:
        _info("  No downgrade detected (incoming version is not older than vendored).")

    downgrade_detected = downgrade_status is DowngradeStatus.DOWNGRADE

    # ------------------------------------------------------------------
    # Step 3: Consumer-visible delta gate — BEFORE any writes (AC6/AC7).
    # ------------------------------------------------------------------
    _info("[3] Consumer-visible delta detection...")
    delta_detected, changed_surfaces = _detect_consumer_visible_delta(doe_clone, sha)

    # Review: code-reviewer (F5) — the full per-surface listing is printed here (stdout)
    # UNLESS the ack gate is about to fire and reprint it via _die (stderr): duplicating
    # it in both streams gave an operator piping either stream alone an inconsistent
    # view (stdout-only misses the remediation text; stderr-only sees the list twice).
    # --dry-run never reaches the gate, so it always gets the full listing here.
    will_hit_gate = delta_detected and not args.dry_run and not args.ack_major
    if delta_detected:
        _info(f"  Delta detected ({len(changed_surfaces)} surface(s)):")
        if will_hit_gate:
            _info("  (surfaces listed in the remediation message below)")
        else:
            for surface in changed_surfaces:
                _info(f"    - {surface}")
    else:
        _info("  No consumer-visible delta detected.")

    # ------------------------------------------------------------------
    # --dry-run: report plan BEFORE the ack gate so the operator gets the
    # full report (delta status, changed surfaces, ack requirement) even
    # when --ack-major was not passed — that is precisely the moment
    # --dry-run is most useful. The gate is a pre-write guard; --dry-run
    # makes no writes.
    # (Review: code-reviewer F2 — gate was aborting before this block ran)
    # ------------------------------------------------------------------
    if args.dry_run:
        _info("\n[DRY RUN] Plan report — no files written:")
        _info(f"  SHA to vendor: {sha}")
        _info(f"  Incoming contract version: {incoming_version}")
        _info(f"  Consumer-visible delta: {delta_detected}")
        if delta_detected:
            _info(f"  Changed surfaces: {changed_surfaces}")
            _info(f"  --ack-major required to proceed: {not args.ack_major}")
        _info(f"  Downgrade detected: {downgrade_detected}")
        if downgrade_detected:
            _info(f"  Version direction: {current_vendored_version} -> {incoming_version}")
            _info(f"  --allow-downgrade required to proceed: {not args.allow_downgrade}")
        _info("  (--dry-run: no files written, no pin written)")
        sys.exit(0)

    if delta_detected:
        _info("  Enforcing reader-first inline --ack-major gate...")
        _enforce_major_delta_gate(changed_surfaces, args.ack_major)

    if downgrade_detected:
        _info("  Enforcing direction-aware downgrade gate...")
        _enforce_downgrade_gate(current_vendored_version, incoming_version, args.allow_downgrade)

    # ------------------------------------------------------------------
    # Step 4: Copy schema/ from git show.
    # ------------------------------------------------------------------
    _info("[4] Copying schema/ from git show...")
    _copy_schema(doe_clone, sha)
    _info("  schema/ copied.")

    # ------------------------------------------------------------------
    # Step 5: Verify — byte-identity (schema/).
    # Fail loud here → no pin write (AC5 contract).
    # ------------------------------------------------------------------
    _info("[5] Byte-identity verification (schema/)...")
    _verify_schema_byte_identity(doe_clone, sha)
    _info("  Byte-identity OK.")

    # ------------------------------------------------------------------
    # Step 6: Pin write (AC4; MAJOR-delta ack, if required, confirmed above before this point).
    # ------------------------------------------------------------------
    _info("[6] Writing pin...")
    _write_pin(sha)

    # ------------------------------------------------------------------
    # Step 7: Post-vendor drift-check green (AC8).
    # ------------------------------------------------------------------
    _info("[7] Post-vendor drift-check...")
    _post_vendor_drift_check(doe_clone)

    _info(f"\nRe-vendor complete. Cockpit-contract pinned at {sha}.")


if __name__ == "__main__":
    main()
