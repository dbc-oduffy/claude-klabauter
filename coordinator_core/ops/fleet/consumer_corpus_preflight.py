"""
coordinator_core.ops.fleet.consumer_corpus_preflight — fleet-wide handoff-vocabulary
consumer-corpus pre-flight.

Purpose: report, PER REGISTERED FLEET REPO, how many ``state/handoffs/**`` +
``archive/handoffs/**`` records carry each ``kind`` frontmatter value — scanning
CONSUMER trees (example-doctrine-repo, claude-klabauter, example-retrieval-repo, example-cockpit-repo,
Example-retrieval-repo-ue-addon, example-game-workbench-repo, example-market-data-repo), never just
this repo's own corpus — AND fail loud the moment any counted LIVE value falls
outside the live ``kind`` enum, or the fleet-repo set itself has silently
fallen behind the registry. This closes the exact gap named in
``state/improvement-queue/2026-07-23-vocabulary-retirement-needs-consumer-corpus-
preflight.yaml``: DR-084 C7 retired old-vocabulary dual-read tolerance on the
strength of a test that scanned ONLY claude-klabauter's own corpus, while claude-klabauter's ops
execute INSIDE consumer repos against THEIR corpora — at retirement time
Example-retrieval-repo was 100% un-migrated and example-cockpit-repo ~96% un-migrated, and
nothing caught it before ceremony failures did. A producer-scoped oracle is the
exact bug this module exists to stop repeating; landing it here IS the notice
protocol the queue entry asked for.

2026-07-31 hardening (cross-repo/inbox/2026-07-31-example-doctrine-repo-em-consumer-
corpus-preflight-blind-to-half-the-fleet.md): the first cut of this module
COULD NOT have caught the thing it was built for. A 2026-07-29 enum narrow
(``kind: spinoff-roadmap`` retired) stranded 59 live records — 34 of them in
two repos (``example-retrieval-repo-ue-addon``, ``example-game-workbench-repo``) that were not
even in ``FLEET_REPO_KEYS``, and the other 25 (``example-cockpit-repo``, which WAS
in the set) went uncaught anyway because nothing compared counted ``kind``
values against the live enum — ``exit_code`` was ``1`` only for an
unresolvable repo, never for a bad value. This hardening closes both holes:
``FLEET_REPO_KEYS`` now names all seven real EM working trees, a reconciliation
pass (see ``run_preflight``) makes the repo set structurally unable to fall
behind the registry silently again, and every counted LIVE ``kind`` is checked
against ``handoff.schema.json``'s live enum.

2026-07-31 second-pass fix (same hardening thread, coordinator review): the
FIRST cut of this hardening judged EVERY counted record — live and archived
alike — against the live enum, which cries wolf: ``handoff-archived.schema.json``
(vendored copy, x-schema-version 2.3.0) was DELIBERATELY WIDENED in the same
C10 commit that narrowed the live schema, precisely so archived records
carrying either vocabulary keep validating (see that file's own
``properties.kind.enum``, which still admits ``spinoff-roadmap``,
``spinoff-goal``, ``spinoff-roadmap-creator``, and ``spike-result``). A record
under ``archive/handoffs/`` — or under a nested ``archive/``/``.archive/`` dir
inside ``state/handoffs/`` — on a retired kind is CORRECT, not stranded; the
first cut of this hardening would have gated a re-vendor on claude-klabauter's
own 25 correctly-archived ``spinoff-roadmap`` records (0 live), and dozens more
across example-retrieval-repo and example-market-data-repo. A gate that fires on correct data
gets acknowledged reflexively and ignored, which is no better than the
pre-flight that never fired at all. This module now loads BOTH enums,
classifies every scanned record live-or-archived (by directory shape, not by
which top-level scan base produced it — see ``_is_archived_record``), and
gates ONLY on a live record off the live enum. An archived record off the
(deliberately permissive) archived enum is still reported, but as a
non-gating warning — narrowing the archived schema is not this module's call.

Deliberately NOT a JSON-RPC-registered op (no ``@register_op``, no entry in
``coordinator_core.authz.classification``/``op_scopes``/``OP_MODULE_MAP``) —
mirrors the precedented ``ops/register_discovered_repos.py`` "direct-import
trampoline, no registered op" shape (see that module's own docstring). This op
enumerates MULTIPLE repos by walking the machine-local registry itself; there is
no single socket-selected ``repo_root`` the engine's ``_OP_KEY_SCOPE`` keying
model was built to hand a handler, and forcing a fleet-spanning read into that
single-repo-per-request shape (or into the registration-quad completeness gate
built for it, ``coordinator_core/authz/registration_quad.py``) would misrepresent
what this module actually does. CLI-only, run directly by an operator or
dispatched from a plan chunk — ``python3 -m
coordinator_core.ops.fleet.consumer_corpus_preflight``.

Repo resolution: ``coordinator_core.ops.fleet._memo_resolver.read_registry_repos()``
— the SAME machine-local ``registry.toml``/``registry.local.toml`` reader
``memo.send``/``memo.list`` already use, never a hardcoded path.

Repo-set reconciliation (why a fleet repo joining can no longer fall behind
silently)
-------------------------------------------------------------------------
The prior design kept a fixed, small ``FLEET_REPO_KEYS`` set with a note that
"a fifth fleet repo joining later is a module update, not a registry-detected
event" — mirroring ``migrate_handoff_vocabulary``'s equally-fixed
``_HEIR_EDGE_KINDS`` precedent for "small stable set, not auto-discovered".
That note is now understood to BE the defect it was written to justify: two of
the three repos the 2026-07-29 narrow stranded records in were never scanned,
because nothing forced a decision when they were registered.

The fix is NOT blind ``repos.*`` enumeration — this machine's own registry
genuinely carries non-EM junk (a `/tmp` smoke-test fixture, a UE `Saved/`
scratch-log path) that a blind sweep would wrongly treat as a fleet repo, and
the ORIGINAL ``:79-83`` rationale for excluding sandboxes/test fixtures was
sound and example-doctrine-repo-ratified; this hardening does not reverse that intent.

Instead every registered ``repos.*`` key is RECONCILED, every run, against two
explicit, checked-in sets:
  - ``FLEET_REPO_KEYS`` — real EM working trees this module scans.
  - ``NON_FLEET_EXCLUDED_KEYS`` — every OTHER currently-known registered key,
    each with a one-line reason it is not an EM working tree (a sandbox, a
    scratch path, an unrelated product repo, a release-assembly shell, ...).

A registered ``repos.*`` key that lands in NEITHER set is ``unclassified`` —
and an unclassified key trips a non-zero exit, naming the key and telling the
operator to add it to one set or the other. THAT is what makes the repo set
structurally unable to fall behind the registry silently: a new registration
forces a deliberate classification decision instead of quietly sitting outside
either set (and therefore outside this oracle's field of view) forever.

Live-vs-archived classification (why a directory-shape rule, not a scan-base
tag)
-------------------------------------------------------------------------
The discriminator that decides which enum governs a record is "is this record
archived", never "which of the two scan bases (``state/handoffs/`` vs
``archive/handoffs/``) produced it". A record physically parked under a nested
archive dir INSIDE ``state/handoffs/`` (claude-klabauter's hidden ``.archive/``,
Example-game-workbench-repo's non-hidden ``archive/`` subdir) is governed by the
SAME archived-schema enum as one under the repo-root ``archive/handoffs/``
tree — both are archived, not live, corpus. ``_is_archived_record`` implements
this as "any path component literally named ``archive`` or ``.archive``",
which covers both the repo-root scan base and the nested-under-``state/``
case in one rule.

Enum oracles: ``coordinator_core/frontmatter/schemas/handoff.schema.json``
(live) and ``coordinator_core/frontmatter/schemas/handoff-archived.schema.json``
(archived) — both vendored copies, authoritative for claude-klabauter's engine; this
module never reads across into example-doctrine-repo's tree. Both ``properties.kind.enum``
lists are loaded fresh every run; a schema file that is missing, unparseable,
or has no ``properties.kind.enum`` is a broken oracle and FAILS LOUD (raises
``PreflightOracleError``) rather than silently skipping the enum check and
reporting green — for EITHER schema, not just the live one.

Negative-spec:
  - A repo whose registry key is unset, or whose registered path does not exist
    on disk, lands in the ``unresolvable`` bucket with an explicit reason — NEVER
    silently counted as zero records. Conflating "could not resolve/scan this
    repo" with "this repo has zero old-vocabulary records" is precisely the
    producer-scoped-oracle failure mode this module exists to close; see
    ``run_preflight``'s docstring for the exact per-repo branch that keeps the
    two distinct.
  - Does NOT mutate anything — pure read/report, no ``--apply`` flag exists (unlike
    ``migrate_handoff_vocabulary``, which this module deliberately does not
    resemble beyond the shared ``iter``-then-``read_fm_field`` scan shape).
  - Does NOT interpret an absent ``kind`` as a defect — the live corpus already
    carries emitter-defaulted absent-``kind`` records (defaults to
    ``session-handoff``) and this module reports the raw ``<absent>`` bucket
    count, never a "fixed" substitution, and NEVER trips ``exit_code`` on
    ``<absent>`` alone, in EITHER the live or archived population.
  - Does NOT judge an archived record against the live enum, or vice versa —
    the exact conflation this module's 2026-07-31 second-pass fix closed. An
    archived record off the (deliberately permissive) archived enum is a
    genuine finding worth reporting, but narrowing that schema is not this
    module's call, so it is a warning, never a gate.
  - Does NOT import ``migrate_handoff_vocabulary``'s private ``_clean_scalar`` /
    ``iter_handoff_files`` helpers across the module boundary — this module owns
    its own minimal comment-aware ``kind`` reader (``_read_kind``) built directly
    on ``coordinator_core.dag._strip_inline_comment`` and
    ``coordinator_core.frontmatter.primitives``, the same primitives
    ``migrate_handoff_vocabulary`` itself is built on, so both stay in sync with
    upstream without a cross-module private-name dependency.
  - Does NOT adopt blind ``repos.*`` enumeration (see "Repo-set reconciliation"
    above) — an unrecognised key is surfaced for classification, never silently
    swept in as a fleet repo or silently ignored as noise.

Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § C5
Origin defect: state/improvement-queue/2026-07-23-vocabulary-retirement-needs-consumer-corpus-preflight.yaml
Hardening backlink: cross-repo/inbox/2026-07-31-example-doctrine-repo-em-consumer-corpus-preflight-blind-to-half-the-fleet.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.dag import _strip_inline_comment
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.fleet._memo_resolver import RegistryReadError, read_registry_repos

_PROG = "consumer-corpus-preflight"

# The vendored, authoritative copies of the two governing schemas — never example-doctrine-repo's tree.
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent.parent / "frontmatter" / "schemas"
_HANDOFF_SCHEMA_PATH = _SCHEMAS_DIR / "handoff.schema.json"
_ARCHIVED_HANDOFF_SCHEMA_PATH = _SCHEMAS_DIR / "handoff-archived.schema.json"

# Path components that mark a record as archived, wherever they appear in its
# path — the repo-root `archive/handoffs/` scan base itself, OR a nested
# `archive/`/`.archive/` dir under `state/handoffs/`. See module docstring
# "Live-vs-archived classification".
_ARCHIVE_DIR_NAMES = frozenset({"archive", ".archive"})


class PreflightOracleError(Exception):
    """Raised when this pre-flight's own oracle (either the live or the
    archived vendored `kind` enum) cannot be loaded — a missing/unparseable
    schema file, or one with no `properties.kind.enum`. A pre-flight that
    cannot load ITS oracle must not report green; callers that want a
    degrade-to-note behavior (e.g. the re-vendor major-bump gate) must catch
    this explicitly rather than letting a broken oracle look like "no
    off-enum records found".
    """


# Display name -> machine-local registry key SUFFIX (full key is "repos.<suffix>").
# All seven real EM working trees this module scans. See module docstring
# "Repo-set reconciliation" for why growing this set no longer risks silently
# stranding a repo outside this oracle's field of view (an unclassified key,
# not silence, is what a new/forgotten repo now produces).
FLEET_REPO_KEYS: Dict[str, str] = {
    "example-doctrine-repo": "example_doctrine_repo",
    "claude-klabauter": "claude_klabauter",
    "example-retrieval-repo": "example_retrieval_repo",
    "cockpit": "example_cockpit_repo",
    "example-retrieval-repo-ue-addon": "example_retrieval_repo_ue_addon",
    "example-game-workbench-repo": "example_game_workbench_repo",
    "example-market-data-repo": "example_market_data_repo",
}

# Every OTHER currently-known registered `repos.*` key that is NOT an EM
# working tree, with a one-line reason. A registered key present in neither
# this set nor FLEET_REPO_KEYS lands in the `unclassified` bucket and trips a
# non-zero exit (see run_preflight) — extend this set (or FLEET_REPO_KEYS) the
# moment that happens, rather than silently ignoring the new key.
NON_FLEET_EXCLUDED_KEYS: Dict[str, str] = {
    "repos.example-smoke-test-fixture": "per-machine smoke-test fixture (registered path is /tmp scratch), not a repo",
    "repos.example-game-repo-python-audit": "UE consumer-project Saved/ scratch dir (python audit recall log), not a git working tree",
    "repos.example-os-repo": "example-os-repo release/binary-assembly shell (packages the assembled fleet into one binary) — not an authoring EM working tree",
    "repos.example_repo": "standalone product repo (example-repo MVP), not part of the coordinator EM fleet",
    "repos.example_stats_repo": "standalone product repo (FIFA stats), not part of the coordinator EM fleet",
    "repos.example_league_data_repo": "standalone data repo, distinct from example_stats_repo, not part of the coordinator EM fleet",
    "repos.experiments": "generic per-machine scratch/experiments repo, not part of the coordinator EM fleet",
    "repos.example-sim-repo": "standalone product repo (example-sim-repo), not part of the coordinator EM fleet",
    "repos.example-voice-system": "standalone product repo (example-voice-system), not part of the coordinator EM fleet",
    "repos.example_store_repo": "standalone product repo (Example Store), not part of the coordinator EM fleet",
}


def _iter_handoff_md_files(repo_root: Path) -> List[Path]:
    """Every ``*.md`` under ``state/handoffs/`` and ``archive/handoffs/``,
    INCLUDING the hidden ``state/handoffs/.archive/`` dir — ``Path.rglob``
    already descends into dot-directories (unlike a shell glob), mirroring
    ``migrate_handoff_vocabulary.iter_handoff_files``'s identical rationale for
    the same hidden-dir requirement (that module's own docstring cites the C8
    execution note this scan shape traces back to). A repo with neither
    directory present contributes zero files, not an error — that is a
    legitimate "no handoff corpus on this machine yet" state for a resolvable
    repo, distinct from the ``unresolvable`` bucket entirely.

    Traversal itself is unchanged from the first cut of this module: nested
    archive directories under ``state/handoffs/`` — hidden (claude-klabauter's own
    ``state/handoffs/.archive/``) OR NOT (example-game-workbench-repo's non-hidden
    ``state/handoffs/archive/`` subdir) — ARE walked and counted here, by
    design. What changed (2026-07-31 second-pass fix) is what happens to a
    file found this way: ``scan_repo_kind_counts`` now classifies EVERY file
    this function returns — root ``archive/handoffs/`` and nested
    ``archive/``/``.archive/`` dirs under ``state/handoffs/`` alike — as
    ARCHIVED corpus via ``_is_archived_record``, governed by the archived
    schema's deliberately-permissive enum, not the live one. Do not "fix"
    this function to skip nested archive dirs — a record moved into a nested
    archive is still part of the corpus this pre-flight scans, it is simply
    judged against the archived vocabulary rather than the live one now.
    """
    state_dir = repo_root / "state" / "handoffs"
    archive_dir = repo_root / "archive" / "handoffs"
    files: set = set()
    for base in (state_dir, archive_dir):
        if base.is_dir():
            files.update(p.resolve() for p in base.rglob("*.md") if p.is_file())
    return sorted(files)


def _is_archived_record(path: Path) -> bool:
    """True if ``path`` is governed by the archived schema's enum, not the
    live one — ANY path component literally named ``archive`` or ``.archive``,
    covering both the repo-root ``archive/handoffs/`` scan base and a nested
    ``archive/``/``.archive/`` dir under ``state/handoffs/`` alike. See module
    docstring "Live-vs-archived classification" for why this is a directory-
    shape rule rather than "which scan base produced this path".
    """
    return any(part in _ARCHIVE_DIR_NAMES for part in path.parts)


def _read_kind(path: Path) -> Optional[str]:
    """Return the record's ``kind`` frontmatter scalar, comment-stripped and
    quote-stripped, or ``None`` when the field is absent, the file has no
    parseable frontmatter, or the file cannot be read.

    Comment-aware via ``coordinator_core.dag._strip_inline_comment`` (the same
    quote-aware primitive ``migrate_handoff_vocabulary._clean_scalar`` and
    ``dag.referenced_by``'s own frontmatter reader use) — a trailing
    ``kind: spinoff  # ...`` comment must not become part of the counted value.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    raw = read_fm_field(split.fm_text, "kind")
    if raw is None:
        return None
    cleaned = _strip_inline_comment(raw)
    if cleaned is None:
        return None
    value = cleaned.strip().strip("\"'")
    return value or None


def scan_repo_kind_counts(repo_root: Path) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Count every handoff record under ``repo_root`` by its ``kind`` value,
    split into the two governed populations.

    Returns ``(live_counts, archived_counts)`` — every scanned file is
    classified via ``_is_archived_record`` and its ``kind`` tallied into
    exactly one of the two dicts. An absent ``kind`` is bucketed under the
    literal string ``"<absent>"`` in whichever population it belongs to —
    this is a VALID, expected corpus shape (the emitter defaults an absent
    ``kind`` to ``session-handoff`` at read time), reported as its own
    bucket, never silently folded into ``"session-handoff"`` or omitted.
    """
    live: Dict[str, int] = {}
    archived: Dict[str, int] = {}
    for path in _iter_handoff_md_files(repo_root):
        kind = _read_kind(path)
        key = kind if kind is not None else "<absent>"
        bucket = archived if _is_archived_record(path) else live
        bucket[key] = bucket.get(key, 0) + 1
    return live, archived


def _load_kind_enum(schema_path: Path, label: str) -> List[str]:
    """Load ``properties.kind.enum`` from a vendored schema file.

    Fails loud (``PreflightOracleError``) rather than degrading — a pre-flight
    that cannot load one of its two oracles must never silently skip that
    enum's check and report green. Raised on: the file missing, the file
    failing to parse as JSON, the parsed document not being an object,
    ``properties.kind.enum`` being absent, or that value not being a
    non-empty list of strings. ``label`` (e.g. ``"live handoff"``,
    ``"archived handoff"``) is folded into the error message so a caller can
    tell which of the two oracles broke.
    """
    if not schema_path.is_file():
        raise PreflightOracleError(f"{label} schema not found at {schema_path}")
    try:
        with open(schema_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightOracleError(f"{label} schema at {schema_path} is unreadable/unparseable: {exc}") from exc
    if not isinstance(doc, dict):
        raise PreflightOracleError(f"{label} schema at {schema_path} did not parse to a JSON object")
    properties = doc.get("properties")
    kind_prop = properties.get("kind") if isinstance(properties, dict) else None
    enum = kind_prop.get("enum") if isinstance(kind_prop, dict) else None
    if not isinstance(enum, list) or not enum or not all(isinstance(v, str) for v in enum):
        raise PreflightOracleError(
            f"{label} schema at {schema_path} has no usable properties.kind.enum "
            f"(got {enum!r}) — this pre-flight cannot verify vocabulary without it."
        )
    return list(enum)


def load_live_kind_enum(schema_path: Path = _HANDOFF_SCHEMA_PATH) -> List[str]:
    """Load ``properties.kind.enum`` from the vendored LIVE handoff schema.

    See ``_load_kind_enum`` for the shared fail-loud contract.
    """
    return _load_kind_enum(schema_path, "live handoff")


def load_archived_kind_enum(schema_path: Path = _ARCHIVED_HANDOFF_SCHEMA_PATH) -> List[str]:
    """Load ``properties.kind.enum`` from the vendored ARCHIVED handoff schema.

    This enum is DELIBERATELY WIDER than the live one — it still admits
    retired live-vocabulary values (e.g. ``spinoff-roadmap``, ``spike-result``)
    so archived records under either vocabulary keep validating. See
    ``_load_kind_enum`` for the shared fail-loud contract.
    """
    return _load_kind_enum(schema_path, "archived handoff")


def _reconcile_repo_set(registered: Dict[str, str]) -> List[dict]:
    """Return the ``unclassified`` bucket: every registered ``repos.*`` key that
    is in NEITHER ``FLEET_REPO_KEYS`` nor ``NON_FLEET_EXCLUDED_KEYS``.

    This is the structural fix for the repo set silently falling behind the
    registry — see module docstring "Repo-set reconciliation". A non-empty
    result here means a registered repo joined without anyone deciding whether
    it is a fleet repo this pre-flight must scan, or noise it should ignore.
    """
    fleet_full_keys = {f"repos.{suffix}" for suffix in FLEET_REPO_KEYS.values()}
    unclassified: List[dict] = []
    for key in sorted(registered):
        if not key.startswith("repos."):
            continue
        if key in fleet_full_keys or key in NON_FLEET_EXCLUDED_KEYS:
            continue
        unclassified.append({
            "key": key,
            "path": registered[key],
            "reason": (
                f"{key} is registered but classified in neither FLEET_REPO_KEYS nor "
                "NON_FLEET_EXCLUDED_KEYS — classify it into one set or the other in "
                "coordinator_core/ops/fleet/consumer_corpus_preflight.py before trusting "
                "this pre-flight's result."
            ),
        })
    return unclassified


def run_preflight() -> Dict[str, object]:
    """Resolve every ``FLEET_REPO_KEYS`` entry via the machine-local registry,
    reconcile the FULL registered ``repos.*`` key space against
    ``FLEET_REPO_KEYS``/``NON_FLEET_EXCLUDED_KEYS``, report per-repo ``kind``
    counts split live-vs-archived, and check each population against ITS OWN
    governing enum.

    Raises:
        PreflightOracleError: either vendored schema's `kind` enum could not
            be loaded (see `load_live_kind_enum`/`load_archived_kind_enum`) —
            propagates uncaught; this is deliberate (a broken oracle must not
            report green).

    Per-repo branch (the negative-spec's load-bearing distinction):
      - Registry itself unreadable (``RegistryReadError`` — a PRESENT
        registry.toml/registry.local.toml that fails to parse, or ``tomllib``
        unavailable): every repo lands in ``unresolvable`` with that shared
        reason. This is a genuine environment/data defect, not "zero records
        anywhere".
      - ``repos.<suffix>`` unset on this machine: that ONE repo lands in
        ``unresolvable`` — "not registered on this machine", never counted as
        zero.
      - ``repos.<suffix>`` set but the path is not an existing directory on
        THIS machine (a registry entry for a sibling repo not cloned here):
        that ONE repo lands in ``unresolvable`` — "registered path does not
        exist on disk", never counted as zero.
      - Registered AND present on disk: scanned via ``scan_repo_kind_counts``,
        even when the scan legitimately finds zero handoff files (a resolvable
        repo with an empty/absent handoff corpus is a REAL zero, correctly
        distinct from an unresolvable repo's negative result).

    Enum check (per population, per module docstring "Live-vs-archived
    classification"):
      - Every counted LIVE ``kind`` bucket other than ``"<absent>"`` is
        compared against ``load_live_kind_enum()``. A value outside it is
        reported in ``off_enum_live`` (repo, kind, count) and trips
        ``exit_code`` — this is the gating condition.
      - Every counted ARCHIVED ``kind`` bucket other than ``"<absent>"`` is
        compared against ``load_archived_kind_enum()``. A value outside it is
        reported in ``off_enum_archived`` — a genuine finding, but NEVER
        gating: the archived schema is deliberately permissive, and narrowing
        it is not this module's call.
      - ``<absent>`` NEVER trips ``exit_code`` in either population.

    Returns:
        {"repos": {display_name: {resolved: bool, path, counts_live,
                                   counts_archived, total}},
         "unresolvable": [{"repo": display_name, "reason": str}, ...],
         "unclassified": [{"key": str, "path": str, "reason": str}, ...],
         "off_enum_live": [{"repo": display_name, "kind": str, "count": int}, ...],
         "off_enum_archived": [{"repo": display_name, "kind": str, "count": int}, ...],
         "live_kind_enum": [str, ...],
         "archived_kind_enum": [str, ...],
         "exit_code": 0|1}
        exit_code is 1 iff ANY of unresolvable/unclassified/off_enum_live is
        non-empty. off_enum_archived NEVER contributes to exit_code.
    """
    # Looked up via the module global (not a bound default) so a test/caller can
    # monkeypatch `_HANDOFF_SCHEMA_PATH`/`_ARCHIVED_HANDOFF_SCHEMA_PATH` and have
    # this pick them up at call time.
    live_kind_enum = set(load_live_kind_enum(_HANDOFF_SCHEMA_PATH))
    archived_kind_enum = set(load_archived_kind_enum(_ARCHIVED_HANDOFF_SCHEMA_PATH))

    repos_report: Dict[str, dict] = {}
    unresolvable: List[dict] = []
    off_enum_live: List[dict] = []
    off_enum_archived: List[dict] = []

    registry_error: Optional[str] = None
    registered: Dict[str, str] = {}
    try:
        registered = read_registry_repos()
    except RegistryReadError as exc:
        registry_error = exc.reason

    unclassified = _reconcile_repo_set(registered) if registry_error is None else []

    for display_name, key_suffix in FLEET_REPO_KEYS.items():
        registry_key = f"repos.{key_suffix}"

        if registry_error is not None:
            reason = f"machine-local registry unreadable: {registry_error}"
            repos_report[display_name] = {"resolved": False, "reason": reason}
            unresolvable.append({"repo": display_name, "reason": reason})
            continue

        path_str = registered.get(registry_key)
        if not path_str:
            reason = f"{registry_key} not registered on this machine"
            repos_report[display_name] = {"resolved": False, "reason": reason}
            unresolvable.append({"repo": display_name, "reason": reason})
            continue

        repo_path = Path(path_str)
        if not repo_path.is_dir():
            reason = f"registered path does not exist on disk: {repo_path}"
            repos_report[display_name] = {
                "resolved": False, "reason": reason, "path": str(repo_path),
            }
            unresolvable.append({"repo": display_name, "reason": reason})
            continue

        counts_live, counts_archived = scan_repo_kind_counts(repo_path)
        repos_report[display_name] = {
            "resolved": True,
            "path": str(repo_path),
            "counts_live": counts_live,
            "counts_archived": counts_archived,
            "total": sum(counts_live.values()) + sum(counts_archived.values()),
        }
        for kind, n in counts_live.items():
            if kind == "<absent>":
                continue
            if kind not in live_kind_enum:
                off_enum_live.append({"repo": display_name, "kind": kind, "count": n})
        for kind, n in counts_archived.items():
            if kind == "<absent>":
                continue
            if kind not in archived_kind_enum:
                off_enum_archived.append({"repo": display_name, "kind": kind, "count": n})

    return {
        "repos": repos_report,
        "unresolvable": unresolvable,
        "unclassified": unclassified,
        "off_enum_live": off_enum_live,
        "off_enum_archived": off_enum_archived,
        "live_kind_enum": sorted(live_kind_enum),
        "archived_kind_enum": sorted(archived_kind_enum),
        "exit_code": 1 if (unresolvable or unclassified or off_enum_live) else 0,
    }


def _print_report(report: Dict[str, object]) -> None:
    repos_report = report["repos"]  # type: ignore[assignment]
    for display_name, entry in repos_report.items():  # type: ignore[union-attr]
        if entry["resolved"]:
            sys.stdout.write(f"{display_name} ({entry['path']}):\n")
            sys.stdout.write("  live:\n")
            for kind, n in sorted(entry["counts_live"].items()):
                sys.stdout.write(f"    {kind}: {n}\n")
            sys.stdout.write("  archived:\n")
            for kind, n in sorted(entry["counts_archived"].items()):
                sys.stdout.write(f"    {kind}: {n}\n")
            sys.stdout.write(f"  TOTAL: {entry['total']}\n")
        else:
            sys.stdout.write(f"{display_name}: UNRESOLVABLE — {entry['reason']}\n")

    unresolvable = report["unresolvable"]  # type: ignore[assignment]
    if unresolvable:  # type: ignore[truthy-bool]
        sys.stderr.write(
            f"\n{len(unresolvable)} repo(s) unresolvable — NOT counted as zero "
            "records; a working-tree-walk negative result, not a positive "
            "'no old-vocabulary tokens here' claim:\n"
        )
        for entry in unresolvable:  # type: ignore[union-attr]
            sys.stderr.write(f"  {entry['repo']}: {entry['reason']}\n")

    off_enum_live = report["off_enum_live"]  # type: ignore[assignment]
    if off_enum_live:  # type: ignore[truthy-bool]
        sys.stderr.write(
            f"\n{len(off_enum_live)} LIVE off-enum kind value(s) found (GATING) — "
            f"outside {report['live_kind_enum']!r}:\n"
        )
        for entry in off_enum_live:  # type: ignore[union-attr]
            sys.stderr.write(f"  {entry['repo']}: kind={entry['kind']!r} count={entry['count']}\n")

    off_enum_archived = report["off_enum_archived"]  # type: ignore[assignment]
    if off_enum_archived:  # type: ignore[truthy-bool]
        sys.stderr.write(
            f"\n{len(off_enum_archived)} ARCHIVED off-enum kind value(s) found "
            f"(WARNING ONLY, does not gate) — outside {report['archived_kind_enum']!r}:\n"
        )
        for entry in off_enum_archived:  # type: ignore[union-attr]
            sys.stderr.write(f"  {entry['repo']}: kind={entry['kind']!r} count={entry['count']}\n")

    unclassified = report["unclassified"]  # type: ignore[assignment]
    if unclassified:  # type: ignore[truthy-bool]
        sys.stderr.write(
            f"\n{len(unclassified)} registered repos.* key(s) unclassified — "
            "in neither FLEET_REPO_KEYS nor NON_FLEET_EXCLUDED_KEYS:\n"
        )
        for entry in unclassified:  # type: ignore[union-attr]
            sys.stderr.write(f"  {entry['key']} ({entry['path']}): {entry['reason']}\n")


def main(argv: List[str]) -> int:
    """CLI entry: ``consumer-corpus-preflight`` (no arguments — enumerates the
    fixed ``FLEET_REPO_KEYS`` set via the machine-local registry).

    Exit code is non-zero whenever ANY fleet repo is unresolvable (registry
    unreadable, key unset, or registered path absent on disk), any counted
    LIVE ``kind`` value falls outside the live enum, or any registered
    ``repos.*`` key is unclassified — see ``run_preflight``'s docstring for
    the exact per-repo/per-condition branch. An ARCHIVED off-enum value is
    reported but never contributes to the exit code.

    A ``PreflightOracleError`` (either vendored schema's enum could not be
    loaded) is caught here and printed as a fail-loud error — the CLI must
    never report green when one of its oracles is broken.
    """
    if argv:
        sys.stderr.write(f"{_PROG}: no arguments expected, got {argv!r}\n")
        return 1
    try:
        report = run_preflight()
    except PreflightOracleError as exc:
        sys.stderr.write(f"{_PROG}: ORACLE FAILURE — {exc}\n")
        return 1
    _print_report(report)
    return report["exit_code"]  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
