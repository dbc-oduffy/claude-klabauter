"""
coordinator_core.frontmatter.schema_drift_watch — cadence watch over the whole
vendored DoE schema set.

Purpose: aggregate `check_schema_drift_advisory` across every schema vendored under
`coordinator_core/frontmatter/schemas/` into ONE non-gating verdict a cadence surface
can act on ("has DoE moved since our pin?"). This is the wiring that closes the
2026-07-22 class defect: the advisory existed but had zero callers, so claude-klabauter's
vendored `improvement-queue.schema.json` sat ~12h behind DoE and the divergence was
only discovered when a sibling repo's CLI rejected a value valid on their surface.

Cadence seam: the `claude-klabauter.schema.vendor_drift` doctor probe
(`bin/claude-klabauter-doctor-probe.py`, manifest entry in `bin/doctor-probes.toml`). That probe
runs in the `--triage` set, whose envelope writes `state/doctor-last-run.json`, which
DoE's `/workday-start` already reads via
`coordinator_core.ops.check_claude_klabauter_doctor_sentinel`. Drift therefore surfaces daily
with NO change to any DoE-owned surface — claude-klabauter owns its probe manifest
(`bin/doctor-probes.toml`) and its probe script; the consumer contract is the existing
sentinel JSON.

Schema-set derivation — DISK GLOB, deliberately (not a hand-maintained list). The
sibling test class `TestPinnedQueueSchemaDrift` in
`coordinator_core/frontmatter/tests/test_schema_validate.py` (backed by the
hand-maintained `_QUEUE_SCHEMA_PINS` dict) is explicit ON PURPOSE, one test method
per schema by its own docstring's admission — a newly vendored, pin-tracked schema
does NOT automatically gain gating drift coverage there; a human must add both the
pin entry and the paired test method. That reasoning inverts here. A drift WATCH
whose coverage does not automatically extend to a newly vendored schema reproduces
exactly the failure class it exists to close — an unwatched vendored file.
Coverage-by-construction is the requirement, so this module globs.

Public seam (2026-07-26, cross-repo ratification — see
cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md):
`scan_vendored_schema_drift()` is a STABLE, externally-consumable entrypoint. Sibling
repos (DoE-claude) MAY import and gate on it directly — this is the answer to their
Ask 1 option 3 ("read scan_vendored_schema_drift through a named seam"): this module
IS that seam, not an internal we'd rather they avoid. Its returned-dict keys are
additive-only across versions — existing keys never change shape or get removed,
new keys only ever get added (see the `local_version`/`doe_version` keys on
`drifted[]` entries, added by this same ratification, as the worked example).

Why TWO surfaces exist (this function AND the `claude-klabauter.schema.vendor_drift` sentinel
key below) rather than just one: the sentinel is written on a DAILY cadence
(`--triage`/full doctor runs only — see `bin/claude-klabauter-doctor-probe.py`'s
`_write_doctor_sentinel`), so it is stale-by-construction the moment any commit lands
between doctor runs. That staleness is fine for a daily nudge; it is NOT fine for a
commit-time gate, which needs a same-commit-or-newer verdict. A commit-time gate
(e.g. DoE's proposed contract-bump RED gate) MUST call `scan_vendored_schema_drift()`
live, in-process, rather than trust the sentinel's last-triage snapshot — reading the
sentinel for that purpose would gate a commit against yesterday's answer.

Negative-spec:
  - NEVER raises. Every path — unresolvable DoE root, absent clone, unreadable schema,
    unexpected exception — folds into a returned verdict dict. Callers must not be
    able to fail a suite or a ceremony off this module (the advisory's whole design).
  - NEVER reports "no drift" for a comparison it could not perform. An unreadable DoE
    clone yields INDETERMINATE, never MATCH — and never DRIFT either (indeterminacy
    is not evidence of divergence).
  - Does NOT re-vendor, write, or mutate anything — read-only probe. Re-vendoring is
    the operator's call (see `bin/claude-klabauter-revendor-handoff-schema.py` for that half).
  - Does NOT replace the GATING tamper-check `check_schema_drift(..., ref=...)`. That
    one pins against a ref and raises by design; this one is the advisory HEAD signal.
    Keeping them structurally separate is deliberate — do not merge them.
  - Does NOT spawn a shell. The only subprocess is the advisory's own `git show`
    (naked-Python runtime convention; git is the consumer, not bash).

Spec backlink: CLAUDE.md § Architecture (vendored-schema/DoE boundary);
               docs/wiki/claude-klabauter-install-doctor-system.md § Probe clusters.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory
from coordinator_core.git_scope import foreign_repo_unusable_reason, scoped_git_env
from coordinator_core.machine_resolver import registry_get

# Directory holding claude-klabauter's vendored copies of DoE's canonical schemas.
VENDORED_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

# Path, relative to a DoE clone root, where the canonical schemas live. Mirrors the
# `coordinator/schemas/<name>` ref that check_schema_drift_advisory resolves via git.
DOE_SCHEMAS_SUBPATH = Path("coordinator") / "schemas"

# GENERATED-NOT-VENDORED (Decision-0, 2026-07-24, plan
# docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md § C5):
# cross-repo-memo.schema.json and archived-memo.schema.json are OUT OF SCOPE
# for this watch by construction, not by omission. This module's whole
# premise is "has DoE moved since our pin?" — a comparison that only makes
# sense for a file claude-klabauter VENDORS FROM DoE. Those two memo schemas are now
# the reverse: claude-klabauter generates them from its own SSOT
# (coordinator_core.contract.emit_memo_schema.emit_schemas) and DoE
# CONSUMES the emission, exactly like the cockpit-contract JSON. Comparing
# them against a DoE `git show` ref would ask "has the consumer moved since
# our pin?" — backwards, and not what this watch exists to answer. They are
# also never written into VENDORED_SCHEMAS_DIR (see emit_memo_schema's
# module docstring CRITICAL placement note), so the disk glob below never
# picks them up in the first place; this frozenset is defense-in-depth
# documentation, not a functional filter that does load-bearing work today.
GENERATED_NOT_VENDORED_SCHEMAS = frozenset(
    {"cross-repo-memo.schema.json", "archived-memo.schema.json"}
)

# Verdict vocabulary. Ordered by the precedence applied in scan_vendored_schema_drift.
STATUS_DRIFT = "DRIFT"
STATUS_INDETERMINATE = "INDETERMINATE"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_MATCH = "MATCH"


def vendored_schema_paths(schemas_dir: Optional[Path] = None) -> list[Path]:
    """Every vendored schema file, sorted — the watch's coverage set, globbed from disk.

    Returns [] (never raises) when the directory is absent or unreadable; the caller
    turns an empty set into an INDETERMINATE verdict rather than a vacuous MATCH.

    Excludes GENERATED_NOT_VENDORED_SCHEMAS defensively — those two names are
    never expected to appear under `directory` (see that constant's
    docstring), but a future accidental write into
    `coordinator_core/frontmatter/schemas/` (the exact placement mistake
    `emit_memo_schema` exists to prevent) must not silently get treated as
    an ordinary vendored-schema drift-watch entry.
    """
    directory = Path(schemas_dir) if schemas_dir is not None else VENDORED_SCHEMAS_DIR
    try:
        return sorted(
            p for p in directory.glob("*.schema.json")
            if p.is_file() and p.name not in GENERATED_NOT_VENDORED_SCHEMAS
        )
    except OSError:
        return []


# Vendored-source coverage set — a PARALLEL, equally-globbed comparison to the
# JSON-schema one above (see this module's docstring, "durability gap" addendum,
# 2026-08-19 — the six pinned test vectors in person_resolver keep claude-klabauter's
# derivation in sync with claude-klabauter's OWN prior run, never with cockpit's; this is
# the drift watch that closes that gap). `*.vendor.ts` is deliberately DISK-GLOB
# derived, same coverage-by-construction posture as `vendored_schema_paths` — a
# newly vendored `<x>.vendor.ts` file gains watch coverage the moment it lands,
# no second list to remember.
COCKPIT_SOURCE_GLOB = "*.vendor.ts"

# Machine-readable header line each `*.vendor.ts` file carries (see
# `contributor-id.vendor.ts`'s header comment) — the cockpit-repo-relative
# path this vendored copy mirrors. Read at scan time so a future vendored file
# is fully self-describing: no filename convention, no second path-mapping dict.
_COCKPIT_SOURCE_PATH_PREFIX = "// cockpit-source-path:"


def vendored_source_paths(schemas_dir: Optional[Path] = None) -> list[Path]:
    """Every vendored cockpit-source file, sorted — glob-derived like `vendored_schema_paths`.

    Returns [] (never raises) when the directory is absent or unreadable.
    """
    directory = Path(schemas_dir) if schemas_dir is not None else VENDORED_SCHEMAS_DIR
    try:
        return sorted(
            p for p in directory.glob(COCKPIT_SOURCE_GLOB) if p.is_file()
        )
    except OSError:
        return []


def _read_cockpit_source_path(vendor_path: Path) -> Optional[str]:
    """Parse the `// cockpit-source-path: <repo-relative path>` header line.

    Returns None (never raises) when the file is unreadable or carries no such
    header — the caller folds that into INDETERMINATE, never a claimed match.
    """
    try:
        text = vendor_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines()[:10]:
        stripped = line.strip()
        if stripped.startswith(_COCKPIT_SOURCE_PATH_PREFIX):
            value = stripped[len(_COCKPIT_SOURCE_PATH_PREFIX):].strip()
            return value or None
    return None


def _vendor_body_without_header(text: str) -> str:
    """Strip the leading `//`-comment header block, leaving the mirrored body.

    The header (cockpit-source-path, cockpit-source-pin, and any accompanying
    `//` prose) is a claude-klabauter-local annotation absent from cockpit's own file, so
    it must not itself register as drift. Strips every contiguous leading line
    that starts with `//`; the first line that does not (cockpit's real content
    always starts with `/**` or `import` for the file this row watches) ends the
    strip. A future vendored file whose OWN upstream content also opens with a
    `//` line would have that line incorrectly stripped too — acceptable for
    this module's advisory, non-gating contract (worst case: a same-shaped
    false MATCH on that one line, never a false DRIFT), not worth a stricter
    delimiter for a single-entry coverage set.
    """
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and lines[idx].lstrip().startswith("//"):
        idx += 1
    return "".join(lines[idx:])


def resolve_cockpit_repo_path() -> Optional[Path]:
    """Best-effort resolution of the example-cockpit-repo sibling clone root.

    Ladder (first rung that yields a directory wins), mirroring
    `resolve_doe_repo_path`'s shape:
      1. REPO_EXAMPLE_COCKPIT_REPO env var — operator/caller override.
      2. registry `repos.example_cockpit_repo` (`coordinator_core.machine_resolver.registry_get`).

    Returns None when neither rung resolves to an existing directory — the
    honest "cockpit clone not present on this machine" answer. Never raises.
    """
    candidates: list[str] = []

    env_root = os.environ.get("REPO_EXAMPLE_COCKPIT_REPO", "").strip()
    if env_root:
        candidates.append(env_root)

    try:
        registry_root = registry_get("repos.example_cockpit_repo") or ""
    except Exception:
        registry_root = ""
    if registry_root:
        candidates.append(registry_root)

    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_dir():
                return path
        except OSError:
            continue
    return None


def check_source_drift_advisory(vendor_path: Path, cockpit_repo_path: Path) -> dict[str, Any]:
    """Non-gating drift check for one `*.vendor.ts` file against cockpit HEAD.

    Same field shape as `schema_validate.check_schema_drift_advisory` (schema,
    diverged, determinate, direction, divergence_kind, local_version,
    doe_version, local_bump_class, doe_bump_class, doe_bump_note, detail) so
    the aggregate reducer in `_scan` can treat both comparison kinds
    uniformly — version/bump-class fields are always None here (cockpit
    source files carry no such headers), never fabricated.

    Negative-spec (mirrors check_schema_drift_advisory exactly):
      - NEVER raises. An unreadable/foreign cockpit clone, an unresolvable
        header, or a failed `git show` all fold into determinate=False,
        diverged=False — indeterminate, never a claimed match or a false
        drift alarm.
    """
    schema_filename = vendor_path.name

    def _indeterminate(detail: str) -> dict[str, Any]:
        return {
            "schema": schema_filename,
            "diverged": False,
            "determinate": False,
            "direction": None,
            "divergence_kind": None,
            "local_version": None,
            "doe_version": None,
            "local_bump_class": None,
            "doe_bump_class": None,
            "doe_bump_note": None,
            "detail": detail,
        }

    source_path = _read_cockpit_source_path(vendor_path)
    if source_path is None:
        return _indeterminate(
            f'Vendored file "{schema_filename}" carries no readable '
            f'"{_COCKPIT_SOURCE_PATH_PREFIX}" header — cannot resolve which cockpit '
            "file it mirrors; drift could not be determined."
        )

    unusable = foreign_repo_unusable_reason(cockpit_repo_path, timeout=30)
    if unusable is not None:
        return _indeterminate(
            f"cockpit repo ({cockpit_repo_path}) could not be read as a git "
            f"repository ({unusable}) — drift could not be determined; this is "
            "not a claim that the vendored copy has diverged."
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(cockpit_repo_path), "show", f"HEAD:{source_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=scoped_git_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _indeterminate(
            f"Could not run git against cockpit repo ({cockpit_repo_path}): {exc}"
        )

    if result.returncode != 0:
        return _indeterminate(
            f'Cannot read cockpit HEAD source "{source_path}": {result.stderr.strip()}. '
            f"cockpit repo path ({cockpit_repo_path}) unreadable or missing this file at HEAD."
        )

    cockpit_content = result.stdout
    try:
        local_content = vendor_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _indeterminate(f"Could not read vendored copy at {vendor_path}: {exc}")

    local_body = _vendor_body_without_header(local_content)
    diverged = local_body.rstrip("\n") != cockpit_content.rstrip("\n")

    if diverged:
        return {
            "schema": schema_filename,
            "diverged": True,
            "determinate": True,
            "direction": None,
            "divergence_kind": None,
            "local_version": None,
            "doe_version": None,
            "local_bump_class": None,
            "doe_bump_class": None,
            "doe_bump_note": None,
            "detail": (
                f'Vendored copy "{schema_filename}" diverges from cockpit HEAD '
                f"({cockpit_repo_path}:{source_path}) — cockpit has moved since the "
                "pin; re-derive and re-vendor (see the C1 derivation this row "
                "guards)."
            ),
        }

    return {
        "schema": schema_filename,
        "diverged": False,
        "determinate": True,
        "direction": None,
        "divergence_kind": None,
        "local_version": None,
        "doe_version": None,
        "local_bump_class": None,
        "doe_bump_class": None,
        "doe_bump_note": None,
        "detail": (
            f'Vendored copy "{schema_filename}" matches cockpit HEAD '
            f"({cockpit_repo_path}:{source_path})."
        ),
    }


def resolve_doe_repo_path() -> Optional[Path]:
    """Best-effort resolution of the DoE-claude sibling clone root — no subprocess.

    Ladder (first rung that yields a directory containing `coordinator/schemas/` wins):
      1. REPO_DOE_CLAUDE env var — the operator/caller override honoured fleet-wide.
      2. `coordinator_core.doe_root_pointer.read_doe_root_pointer()` — registry-first
         (DR-071 `repos.doe_claude`), durable-pointer-file, then legacy-pointer-file
         fallback (see that module's docstring for the full 3-sub-rung ladder). This
         already IS the reset-safe, registry-anchored resolution — it does not assume
         any fixed checkout layout.

    Returns None when no rung resolves — the honest "DoE clone not present on this
    machine" answer (fresh machine, CI without the sibling checked out, or a machine
    whose registry has no `repos.doe_claude` entry yet). Deliberately subprocess-free:
    this runs inside a doctor probe on the cheap first-pass triage path, so the
    `machine-local get` spawn rung used by `coordinator_core.ops.coordinator_doe_root`
    is NOT part of this ladder.

    Negative-spec:
      - Never raises, and never returns a path lacking `coordinator/schemas/` — a
        wrong-but-present path would produce a wall of false indeterminates.
      - Does NOT walk `Path(__file__).resolve().parents[N]` to guess a flat-sibling
        `<claude-klabauter repo root>/../DoE-claude` layout. A prior rung 3 did exactly that
        and was retired 2026-07-22: it hardcoded both claude-klabauter's checkout depth from
        this file AND a flat-sibling directory layout, so it silently reported "DoE
        clone not present" on any machine where DoE-claude isn't checked out next
        to claude-klabauter — the antipattern `coordinator_core/tests/test_no_hardcoded_paths.py`
        now gates against fleet-wide. `read_doe_root_pointer()`'s registry rung
        already subsumes the case that depth-walk existed for (a DoE-claude clone
        present but not yet pointer-configured) — once `repos.doe_claude` or either
        pointer file is populated, which every install-chain walk does, rung 2
        resolves it correctly regardless of checkout layout. No replacement rung is
        added; there is nothing left for it to cover.
    """
    candidates: list[Path] = []

    env_root = os.environ.get("REPO_DOE_CLAUDE", "").strip()
    if env_root:
        candidates.append(Path(env_root))

    try:
        pointer_root = read_doe_root_pointer().strip()
    except Exception:
        pointer_root = ""
    if pointer_root:
        candidates.append(Path(pointer_root))

    for candidate in candidates:
        try:
            if (candidate / DOE_SCHEMAS_SUBPATH).is_dir():
                return candidate
        except OSError:
            continue
    return None


def scan_vendored_schema_drift(
    doe_repo_path: Optional[Path | str] = None,
    schemas_dir: Optional[Path | str] = None,
    cockpit_repo_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Run the advisory over every vendored schema/source and reduce to one non-gating verdict.

    Args:
        doe_repo_path: DoE clone root. None → resolve_doe_repo_path().
        schemas_dir: vendored-schema/source directory. None → VENDORED_SCHEMAS_DIR.
        cockpit_repo_path: example-cockpit-repo clone root, for the `*.vendor.ts`
            comparison. None → resolve_cockpit_repo_path(). Additive parameter
            (2026-08-19, C7); appended last with a default so every existing
            positional call site (doe_repo_path, schemas_dir) is unaffected.

    Returns a dict with keys:
        status (str): one of UNRESOLVED / DRIFT / INDETERMINATE / MATCH.
        doe_repo_path (str | None): the DoE clone root actually used, if resolved.
        cockpit_repo_path (str | None): the example-cockpit-repo clone root actually
            used, if resolved — via resolve_cockpit_repo_path() (REPO_EXAMPLE_COCKPIT_REPO
            env var, then registry `repos.example_cockpit_repo`). Additive key (2026-08-19,
            C7 — the `*.vendor.ts`-vs-cockpit-HEAD parallel comparison described
            below). None whenever no cockpit clone resolved on this machine; the two
            repo-path keys resolve independently, so one being None does not imply
            the other is.
        checked (int): number of vendored files compared — JSON schemas (against
            DoE HEAD) plus `*.vendor.ts` files (against cockpit HEAD), summed.
        matched (list[str]): filenames confirmed to match their upstream HEAD
            (DoE for `*.schema.json`, cockpit for `*.vendor.ts`).
        drifted (list[dict]): {schema, detail, direction, divergence_kind,
            local_version, doe_version, local_bump_class, doe_bump_class,
            doe_bump_note} per diverged schema.
            direction is one of schema_validate.DIRECTION_WE_AHEAD /
            DIRECTION_WE_BEHIND / DIRECTION_BOTH (or None if an older advisory
            build didn't emit it) — see schema_validate._infer_drift_direction.
            divergence_kind is "shape" / "prose-only" / None, orthogonal to
            direction — whether the delta touches validation shape or is
            confined to prose (schema_validate.check_schema_drift_advisory's
            `divergence_kind` doc has the full contract) — passed through
            verbatim, never recomputed here (see this module's "SHAPE TO AVOID"
            note). Additive key (2026-08-13 parity-tail exchange).
            local_version/doe_version are the two sides' top-level
            `x-schema-version` values (str | None each, via
            schema_validate._read_schema_version) — passed through verbatim from
            check_schema_drift_advisory, never re-parsed here (see this module's
            "SHAPE TO AVOID" note). Additive keys (2026-07-26, cross-repo
            schema-version surfacing — see cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md).
            local_bump_class/doe_bump_class are the two sides' top-level
            `x-bump-class` values (str | None each, via
            schema_validate._read_bump_class) — closed vocabulary
            `top-level-array-additive` / `nested-field-additive` / `major` on the
            producer side, but this watch does not validate membership and derives
            NO hold/no-hold verdict from it (holding is axis-dependent — DR-097 §
            Reconciliation — and out of scope here). doe_bump_note is DoE HEAD's
            optional one-line `x-bump-note` (str | None, via
            schema_validate._read_bump_note). All three: passed through verbatim
            from check_schema_drift_advisory, never re-parsed here (see this
            module's "SHAPE TO AVOID" note); None is the ordinary case while
            upstream adoption is partial, never an error. Additive keys
            (2026-07-27, bump-class surfacing — see
            cross-repo/inbox/2026-07-27-doe-claude-em-bump-class-shipped-and-a-correction.md).
        indeterminate (list[dict]): {schema, detail} per schema whose comparison
            could NOT be performed.
        summary (str): one-line operator-facing sentence.

    Status precedence — DRIFT outranks INDETERMINATE outranks MATCH, so a partially
    unreadable clone can never mask a real divergence that WAS observed. UNRESOLVED is
    the distinct "no DoE clone at all" case: not applicable rather than not working,
    and the caller SKIPs on it instead of nagging a machine that has no sibling repo.

    Negative-spec: never raises — an unexpected exception is reported as
    INDETERMINATE with the exception text in `summary`.
    """
    try:
        return _scan(doe_repo_path, schemas_dir, cockpit_repo_path)
    except Exception as exc:  # noqa: BLE001 — total containment is this module's contract
        return {
            "status": STATUS_INDETERMINATE,
            "doe_repo_path": str(doe_repo_path) if doe_repo_path else None,
            "cockpit_repo_path": str(cockpit_repo_path) if cockpit_repo_path else None,
            "checked": 0,
            "matched": [],
            "drifted": [],
            "indeterminate": [],
            "summary": (
                "Vendored-schema drift check could not run: "
                f"{type(exc).__name__}: {exc}"
            ),
        }


def _scan(
    doe_repo_path: Optional[Path | str],
    schemas_dir: Optional[Path | str],
    cockpit_repo_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Inner scan body — see scan_vendored_schema_drift for the contract it satisfies.

    Runs TWO independent, equally-globbed comparisons and folds both into one
    aggregate verdict: the existing JSON-schema-vs-DoE-HEAD comparison, and a
    parallel `*.vendor.ts`-vs-cockpit-HEAD comparison (2026-08-19, the C7
    durability-gap mitigation for C1's cockpit `contributor_slug` derivation —
    see this module's docstring). The two external repos resolve
    independently; one being absent on this machine does NOT mask a real
    drift finding from the other, so UNRESOLVED is reserved for the case where
    NEITHER comparison could even be attempted.
    """
    resolved_doe = (
        Path(doe_repo_path) if doe_repo_path is not None else resolve_doe_repo_path()
    )
    resolved_cockpit = (
        Path(cockpit_repo_path) if cockpit_repo_path is not None else resolve_cockpit_repo_path()
    )

    schema_paths = vendored_schema_paths(Path(schemas_dir) if schemas_dir is not None else None)
    source_paths = vendored_source_paths(Path(schemas_dir) if schemas_dir is not None else None)

    if resolved_doe is None and resolved_cockpit is None:
        return {
            "status": STATUS_UNRESOLVED,
            "doe_repo_path": None,
            "checked": 0,
            "matched": [],
            "drifted": [],
            "indeterminate": [],
            "summary": (
                "No sibling schema-source clone resolved on this machine (checked "
                "REPO_DOE_CLAUDE, the .doe-root pointer, REPO_EXAMPLE_COCKPIT_REPO, and the "
                "registry repos.example_cockpit_repo key) — vendored drift is not determinable "
                "here."
            ),
        }

    if not schema_paths and not source_paths:
        directory = (
            Path(schemas_dir) if schemas_dir is not None else VENDORED_SCHEMAS_DIR
        )
        return {
            "status": STATUS_INDETERMINATE,
            "doe_repo_path": str(resolved_doe) if resolved_doe else None,
            "checked": 0,
            "matched": [],
            "drifted": [],
            "indeterminate": [],
            "summary": (
                f"No vendored schemas or sources found under {directory} — nothing to "
                "compare; treating as indeterminate rather than clean (an empty coverage "
                "set is not evidence of no drift)."
            ),
        }

    matched: list[str] = []
    drifted: list[dict[str, str]] = []
    indeterminate: list[dict[str, str]] = []

    if resolved_doe is None and schema_paths:
        for schema_path in schema_paths:
            indeterminate.append({
                "schema": schema_path.name,
                "detail": (
                    "No sibling DoE-claude clone resolved on this machine — drift "
                    "could not be determined."
                ),
            })
    elif resolved_doe is not None:
        for schema_path in schema_paths:
            result = check_schema_drift_advisory(schema_path, resolved_doe)
            name = str(result.get("schema") or schema_path.name)
            detail = str(result.get("detail") or "")
            # `determinate` is the advisory's discriminator for its diverged=False overload
            # (matches vs could-not-read). Absent key → treat as determinate so an older
            # advisory build degrades to today's diverged-only semantics, not to a wall of
            # false indeterminates.
            if not result.get("determinate", True):
                indeterminate.append({"schema": name, "detail": detail})
            elif result.get("diverged"):
                # `direction` (WE_AHEAD/WE_BEHIND/BOTH, absent -> None on an older advisory
                # build) is the field this watch exists to surface: a DRIFT verdict that
                # cannot say who moved is unactionable — see this module's docstring and
                # the memo it links.
                drifted.append({
                    "schema": name,
                    "detail": detail,
                    "direction": result.get("direction"),
                    "divergence_kind": result.get("divergence_kind"),
                    "local_version": result.get("local_version"),
                    "doe_version": result.get("doe_version"),
                    "local_bump_class": result.get("local_bump_class"),
                    "doe_bump_class": result.get("doe_bump_class"),
                    "doe_bump_note": result.get("doe_bump_note"),
                })
            else:
                matched.append(name)

    if resolved_cockpit is None and source_paths:
        for source_path in source_paths:
            indeterminate.append({
                "schema": source_path.name,
                "detail": (
                    "No sibling example-cockpit-repo clone resolved on this machine (checked "
                    "REPO_EXAMPLE_COCKPIT_REPO and the registry repos.example_cockpit_repo key) — "
                    "drift could not be determined."
                ),
            })
    elif resolved_cockpit is not None:
        for source_path in source_paths:
            result = check_source_drift_advisory(source_path, resolved_cockpit)
            name = str(result.get("schema") or source_path.name)
            detail = str(result.get("detail") or "")
            if not result.get("determinate", True):
                indeterminate.append({"schema": name, "detail": detail})
            elif result.get("diverged"):
                drifted.append({
                    "schema": name,
                    "detail": detail,
                    "direction": result.get("direction"),
                    "divergence_kind": result.get("divergence_kind"),
                    "local_version": result.get("local_version"),
                    "doe_version": result.get("doe_version"),
                    "local_bump_class": result.get("local_bump_class"),
                    "doe_bump_class": result.get("doe_bump_class"),
                    "doe_bump_note": result.get("doe_bump_note"),
                })
            else:
                matched.append(name)

    checked = len(schema_paths) + len(source_paths)

    if drifted:
        # Named + directioned, one segment per drifted file — the skimmable line a
        # daily cadence surface reads without opening the doctor-probe dump: WHAT
        # drifted, WHICH DIRECTION (we-are-ahead / we-are-behind / both), and DoE's
        # declared bump class alongside it (verbatim surface only — no hold/no-hold
        # verdict is derived here; see the docstring's negative-spec). Cockpit-source
        # entries never carry a direction/bump-class (None on both), so they render
        # as "[direction unknown]" — accurate, not a defect.
        named = ", ".join(
            f"{d['schema']} [{d.get('direction') or 'direction unknown'}"
            f"{', bump-class ' + d['doe_bump_class'] if d.get('doe_bump_class') else ''}]"
            for d in drifted
        )
        extra = (
            f" ({len(indeterminate)} further indeterminate)"
            if indeterminate
            else ""
        )
        status, summary = STATUS_DRIFT, (
            f"{len(drifted)}/{checked} vendored file(s) diverge from their upstream HEAD: "
            f"{named}{extra}. Upstream has moved since the pin — re-vendor."
        )
    elif indeterminate:
        names = ", ".join(d["schema"] for d in indeterminate)
        status, summary = STATUS_INDETERMINATE, (
            f"INDETERMINATE — could not compare {len(indeterminate)}/{checked} vendored "
            f"file(s) against upstream HEAD: {names}. This is NOT a drift finding and "
            "NOT a clean bill of health; the check did not run."
        )
    else:
        status, summary = STATUS_MATCH, (
            f"All {checked} vendored file(s) match their upstream HEAD."
        )

    return {
        "status": status,
        "doe_repo_path": str(resolved_doe) if resolved_doe else None,
        "cockpit_repo_path": str(resolved_cockpit) if resolved_cockpit else None,
        "checked": checked,
        "matched": matched,
        "drifted": drifted,
        "indeterminate": indeterminate,
        "summary": summary,
    }
