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
from pathlib import Path
from typing import Any, Optional

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory

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
) -> dict[str, Any]:
    """Run the advisory over every vendored schema and reduce to one non-gating verdict.

    Args:
        doe_repo_path: DoE clone root. None → resolve_doe_repo_path().
        schemas_dir: vendored-schema directory. None → VENDORED_SCHEMAS_DIR.

    Returns a dict with keys:
        status (str): one of UNRESOLVED / DRIFT / INDETERMINATE / MATCH.
        doe_repo_path (str | None): the clone root actually used.
        checked (int): number of vendored schemas compared.
        matched (list[str]): filenames confirmed byte-identical to DoE HEAD.
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
        return _scan(doe_repo_path, schemas_dir)
    except Exception as exc:  # noqa: BLE001 — total containment is this module's contract
        return {
            "status": STATUS_INDETERMINATE,
            "doe_repo_path": str(doe_repo_path) if doe_repo_path else None,
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
) -> dict[str, Any]:
    """Inner scan body — see scan_vendored_schema_drift for the contract it satisfies."""
    resolved_doe = (
        Path(doe_repo_path) if doe_repo_path is not None else resolve_doe_repo_path()
    )

    if resolved_doe is None:
        return {
            "status": STATUS_UNRESOLVED,
            "doe_repo_path": None,
            "checked": 0,
            "matched": [],
            "drifted": [],
            "indeterminate": [],
            "summary": (
                "No sibling schema-source clone resolved on this machine (checked "
                "REPO_DOE_CLAUDE, the .doe-root pointer, and the sibling-checkout layout) "
                "— vendored-schema drift is not determinable here."
            ),
        }

    paths = vendored_schema_paths(Path(schemas_dir) if schemas_dir is not None else None)
    if not paths:
        directory = (
            Path(schemas_dir) if schemas_dir is not None else VENDORED_SCHEMAS_DIR
        )
        return {
            "status": STATUS_INDETERMINATE,
            "doe_repo_path": str(resolved_doe),
            "checked": 0,
            "matched": [],
            "drifted": [],
            "indeterminate": [],
            "summary": (
                f"No vendored schemas found under {directory} — nothing to compare; "
                "treating as indeterminate rather than clean (an empty coverage set is "
                "not evidence of no drift)."
            ),
        }

    matched: list[str] = []
    drifted: list[dict[str, str]] = []
    indeterminate: list[dict[str, str]] = []

    for schema_path in paths:
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

    checked = len(paths)

    if drifted:
        # Named + directioned, one segment per drifted file — the skimmable line a
        # daily cadence surface reads without opening the doctor-probe dump: WHAT
        # drifted, WHICH DIRECTION (we-are-ahead / we-are-behind / both), and DoE's
        # declared bump class alongside it (verbatim surface only — no hold/no-hold
        # verdict is derived here; see the docstring's negative-spec).
        named = ", ".join(
            f"{d['schema']} [{d.get('direction') or 'direction unknown'}"
            f"{', bump-class ' + d['doe_bump_class'] if d.get('doe_bump_class') else ''}]"
            for d in drifted
        )
        extra = (
            f" ({len(indeterminate)} further schema(s) indeterminate)"
            if indeterminate
            else ""
        )
        status, summary = STATUS_DRIFT, (
            f"{len(drifted)}/{checked} vendored schema(s) diverge from DoE HEAD: "
            f"{named}{extra}. DoE has moved since the pin — re-vendor."
        )
    elif indeterminate:
        names = ", ".join(d["schema"] for d in indeterminate)
        status, summary = STATUS_INDETERMINATE, (
            f"INDETERMINATE — could not compare {len(indeterminate)}/{checked} vendored "
            f"schema(s) against DoE HEAD at {resolved_doe}: {names}. This is NOT a "
            "drift finding and NOT a clean bill of health; the check did not run."
        )
    else:
        status, summary = STATUS_MATCH, (
            f"All {checked} vendored schema(s) match DoE HEAD at {resolved_doe}."
        )

    return {
        "status": status,
        "doe_repo_path": str(resolved_doe),
        "checked": checked,
        "matched": matched,
        "drifted": drifted,
        "indeterminate": indeterminate,
        "summary": summary,
    }
