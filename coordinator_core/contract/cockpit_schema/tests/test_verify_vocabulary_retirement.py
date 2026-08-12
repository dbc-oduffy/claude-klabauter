"""
test_verify_vocabulary_retirement — regression guard for the DR-084 P4
handoff-lifecycle vocabulary narrow (2026-07-22, C7).

Modelled on ``test_verify_superseded_retirement.py`` (the 2026-06-26
``superseded``-status retirement guard) — same shape, different axis: this
locks the NARROWED enums in claude-klabauter's own vendored frontmatter schema pair
(``coordinator_core/frontmatter/schemas/handoff{,-archived}.schema.json``,
re-vendored at C7) and corpus-scans the live+archived handoff tree for
stragglers still carrying the retired vocabulary.

Unlike ``test_verify_superseded_retirement.py`` this is NOT example-doctrine-repo-clone-gated
(``@skip_no_doe``) — the schema pair and the handoff corpus it scans are both
local to this repo, not read from the example-doctrine-repo clone.

Spec backlink: docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md § C7

Retiring-ruling backlink: 2026-08-02 fast-tier stale-test triage
(tasks/mise-verify/triage-C-cockpit.md § test_verify_vocabulary_retirement.py).
This test's C7-day snapshot of the vocabulary state has since moved twice,
both times deliberately: commit ed2c4dd3e ("restore 11 pruned stubs...")
re-vendored a WIDENED handoff-archived.schema.json (a4a79bf7, 2cfaadc6) that
re-admits ``consumed`` as a permanently-grandfathered value for restored
legacy archive records under archive/handoffs/ — see that schema's own
"status" description for the full history — and the live/archived schema
versions have independently drifted apart since (4.0.0 live, 2.3.0 archived;
they are no longer expected to match each other or a shared 2.0.0 pin). This
test is updated to assert the current, ratified vocabulary rather than
re-litigate a superseded snapshot: ``consumed`` (and the ``consumed_by``/
``consumed_at`` field names that necessarily accompany it) remains retired
for LIVE ``state/handoffs/**`` records — that axis is UNCHANGED — but is
tolerated for ARCHIVED ``archive/handoffs/**`` records under the same
grandfather ``superseded`` already enjoyed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCHEMAS_DIR = _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas"

_HANDOFF_SCHEMA = _SCHEMAS_DIR / "handoff.schema.json"
_HANDOFF_ARCHIVED_SCHEMA = _SCHEMAS_DIR / "handoff-archived.schema.json"

# The narrowed vocabulary, as landed by C7's re-vendor. ``handoff.schema.json``
# (live records) is fully narrowed; ``handoff-archived.schema.json`` keeps
# ``superseded`` as a SEPARATE, permanently-grandfathered retirement (see
# test_verify_superseded_retirement.py) — a DO-NOT-NARROW invariant, not a
# DR-084 straggler.
_LIVE_STATUS_ENUM = ["open", "claimed"]
# 'consumed' was restored here 2026-07-27 (ed2c4dd3e / a4a79bf7 / 2cfaadc6) as
# a SECOND permanently-grandfathered value alongside 'superseded' — read-
# tolerance for the 11 (now 22, corpus scan below) archive/handoffs/ records
# written under the pre-P4 vocabulary. See handoff-archived.schema.json's own
# "status" property description for the full history.
_ARCHIVED_STATUS_ENUM = ["open", "claimed", "consumed", "superseded"]
_DEPLOYMENT_STATE_ENUM = [
    "awaiting_gate", "ready_to_fire", "in_flight", "shipped", "continued", "closed",
]
# Version FLOOR, not an equality pin. Claude-klabauter re-vendors this schema pair from
# example-doctrine-repo rather than authoring it, so an equality pin goes red on every upstream
# re-vendor whether or not the vocabulary actually regressed — a standing
# false positive by construction (it fired three times between C7 and
# 2026-08-02: 2.0.0 -> 2.1.0 -> 2.3.0 archived, 2.0.0 -> 4.0.0 live, none of
# which touched the enums this module guards). The floor still catches the one
# thing worth catching: a re-vendor that rolls the pair BACK behind the C7
# narrow. Enum drift is asserted directly by the tests above, not via version.
_MIN_SCHEMA_VERSION = (2, 0, 0)

# Old-vocabulary tokens retired by DR-084 P4.
#
# 'active' is in NEITHER schema's status enum, so it stays forbidden
# CORPUS-WIDE (live + archived). 'consumed' is forbidden for LIVE
# state/handoffs/** records only — handoff-archived.schema.json re-admits it
# as a permanently-grandfathered value (see _ARCHIVED_STATUS_ENUM above), so
# scanning the archive tree for it asserts the opposite of the ratified
# contract.
_STATUS_VALUES_FORBIDDEN_CORPUS_WIDE = {"active"}
_STATUS_VALUES_FORBIDDEN_LIVE_ONLY = {"consumed"}
_OLD_DEPLOYMENT_STATE_VALUE = "abandoned"

# Q1 (2026-08-02 fast-tier triage, tasks/mise-verify/q1-consumed-field-retirement.md):
# DR-084 P4 retired enum VALUES, not property NAMES — 'consumed_by'/'consumed_at'
# were never removed from either schema in either repo's history, so this
# assertion's premise as originally coded (absent corpus-wide) was never true.
# Its INTENT — live records carry claimed_by/claimed_at, not the consumed pair —
# is a live ratified contract, so the assertion is narrowed to live records
# rather than deleted.
_OLD_FIELD_NAMES = ("consumed_by", "consumed_at")

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_STATUS_LINE_RE = re.compile(r'^status:\s*["\']?([^"\'#\n\r]+)["\']?\s*$', re.MULTILINE)
_DEPLOYMENT_STATE_LINE_RE = re.compile(
    r'^deployment_state:\s*["\']?([^"\'#\n\r]+)["\']?\s*$', re.MULTILINE
)


def _load_schema(path: Path) -> dict:
    assert path.is_file(), f"missing vendored schema: {path}"
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Schema-pair narrowing lock
# ---------------------------------------------------------------------------


def test_live_handoff_schema_status_enum_narrowed():
    schema = _load_schema(_HANDOFF_SCHEMA)
    assert schema["properties"]["status"]["enum"] == _LIVE_STATUS_ENUM, (
        f"handoff.schema.json status enum drifted from the P4 narrow: "
        f"{schema['properties']['status']['enum']!r}"
    )


def test_archived_handoff_schema_status_enum_keeps_superseded_grandfather():
    schema = _load_schema(_HANDOFF_ARCHIVED_SCHEMA)
    assert schema["properties"]["status"]["enum"] == _ARCHIVED_STATUS_ENUM, (
        f"handoff-archived.schema.json status enum drifted: "
        f"{schema['properties']['status']['enum']!r}"
    )


def test_deployment_state_enum_narrowed_on_both_schemas():
    for path in (_HANDOFF_SCHEMA, _HANDOFF_ARCHIVED_SCHEMA):
        schema = _load_schema(path)
        enum = schema["properties"]["deployment_state"]["enum"]
        assert enum == _DEPLOYMENT_STATE_ENUM, (
            f"{path.name} deployment_state enum drifted from the P4 narrow: {enum!r}"
        )
        assert _OLD_DEPLOYMENT_STATE_VALUE not in enum, (
            f"{path.name} deployment_state enum still carries the retired "
            f"{_OLD_DEPLOYMENT_STATE_VALUE!r} value"
        )


def _parse_version(raw: object) -> tuple[int, ...]:
    assert isinstance(raw, str) and raw, f"x-schema-version missing or non-string: {raw!r}"
    parts = raw.split(".")
    assert all(p.isdigit() for p in parts), f"unparseable x-schema-version: {raw!r}"
    return tuple(int(p) for p in parts)


def test_schema_pair_versions_at_or_past_the_c7_floor():
    """Both schemas are stamped at or past the C7 narrow's 2.0.0 floor.

    Negative-spec: deliberately NOT an equality pin on either schema. The two
    versions have drifted independently since C7 (4.0.0 live, 2.3.0 archived)
    and will keep drifting — claude-klabauter re-vendors this pair from example-doctrine-repo rather than
    authoring it. See _MIN_SCHEMA_VERSION for why the floor is the assertion
    that carries signal and the equality pin was the one that did not.
    """
    for path in (_HANDOFF_SCHEMA, _HANDOFF_ARCHIVED_SCHEMA):
        schema = _load_schema(path)
        raw = schema.get("x-schema-version")
        assert _parse_version(raw) >= _MIN_SCHEMA_VERSION, (
            f"{path.name} x-schema-version is {raw!r}, behind the C7 narrow floor "
            f"{'.'.join(str(n) for n in _MIN_SCHEMA_VERSION)!r} — a re-vendor appears to "
            "have rolled the schema pair back behind the DR-084 P4 vocabulary narrow"
        )


# ---------------------------------------------------------------------------
# Corpus scan — zero old-vocabulary frontmatter, live + archived + hidden dir
# ---------------------------------------------------------------------------


def _iter_handoff_md_files() -> list[Path]:
    """Every handoff record under state/handoffs/ and archive/handoffs/,
    INCLUDING the hidden state/handoffs/.archive/ dir.

    ``Path.rglob`` already descends into dot-directories (unlike a shell
    glob), so a plain ``rglob("*.md")`` over ``state/handoffs/`` already
    picks up ``.archive/`` — this function still enumerates it separately
    and asserts non-emptiness so the corpus scan below is not silently
    vacuous if that traversal behavior ever changes (C8 execution note: 2
    stragglers were migrated there, "a hidden dir outside every glob,
    harmless under dual-read but a P4 landmine once old-vocabulary
    acceptance retires").
    """
    state_dir = _REPO_ROOT / "state" / "handoffs"
    archive_dir = _REPO_ROOT / "archive" / "handoffs"
    hidden_archive_dir = state_dir / ".archive"

    files = set(state_dir.rglob("*.md")) | set(archive_dir.rglob("*.md"))

    if hidden_archive_dir.is_dir():
        hidden_files = list(hidden_archive_dir.rglob("*.md"))
        assert hidden_files, (
            f"{hidden_archive_dir} exists but is empty — the corpus-scan "
            "assertion below needs this dir populated to actually exercise "
            "the hidden-dir coverage this test exists for"
        )
        files |= set(hidden_files)

    return sorted(files)


def _iter_live_handoff_md_files() -> list[Path]:
    """Every LIVE handoff record — everything under state/handoffs/, INCLUDING
    the hidden state/handoffs/.archive/ dir.

    The hidden dir stays IN scope here despite its name: it lives under
    state/handoffs/, it is confirmed clean of the retired vocabulary, and it is
    the exact landmine C8 flagged. Excluding it would move the two
    vocabulary assertions off the one directory this module's hidden-dir
    coverage exists to protect.

    Distinct from _iter_handoff_md_files() (live + archive/handoffs/), which
    remains the right scope for values the ARCHIVED schema also forbids.
    """
    return sorted((_REPO_ROOT / "state" / "handoffs").rglob("*.md"))


def test_hidden_archive_dir_is_covered_by_the_corpus_scan():
    """Sanity check the fixture premise itself: state/handoffs/.archive/
    exists and its files are included in _iter_handoff_md_files()'s output —
    a scan that silently skipped it would make the zero-old-vocabulary
    assertions below vacuously true for exactly the landmine C8 flagged."""
    hidden_archive_dir = _REPO_ROOT / "state" / "handoffs" / ".archive"
    assert hidden_archive_dir.is_dir(), f"missing fixture dir: {hidden_archive_dir}"
    hidden_files = set(hidden_archive_dir.rglob("*.md"))
    assert hidden_files, f"{hidden_archive_dir} has no .md files to cover"
    assert hidden_files <= set(_iter_handoff_md_files())
    # The live-only scan is what enforces the 'consumed' vocabulary post-narrow,
    # so the hidden dir must be covered by THAT scan too, not just the wide one.
    assert hidden_files <= set(_iter_live_handoff_md_files())


def _is_archived_corpus_path(rel_path: str) -> bool:
    """True for a path under archive/handoffs/ — the sole scope of the
    'consumed' grandfather restored by ed2c4dd3e / a4a79bf7 / 2cfaadc6 (see
    module docstring). state/handoffs/ (including its hidden .archive/
    subdir) is NOT in scope: that grandfather is archive/handoffs/-specific,
    not a blanket "anything under a dir named archive" rule — the 2026-08-02
    triage's corpus scan found zero consumed/consumed_by/consumed_at
    violators outside archive/handoffs/, so this stays the precise, evidenced
    scope rather than a speculatively wider one.
    """
    return rel_path.startswith("archive/handoffs/")


def test_no_handoff_record_carries_old_status_vocabulary():
    violators = []
    for path in _iter_handoff_md_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        fm_match = _FRONTMATTER_RE.match(text)
        if not fm_match:
            continue
        status_match = _STATUS_LINE_RE.search(fm_match.group(1))
        if not status_match:
            continue
        value = status_match.group(1).strip()
        rel_path = path.relative_to(_REPO_ROOT).as_posix()
        if value in _STATUS_VALUES_FORBIDDEN_CORPUS_WIDE:
            violators.append((rel_path, value))
        elif value in _STATUS_VALUES_FORBIDDEN_LIVE_ONLY and not _is_archived_corpus_path(rel_path):
            violators.append((rel_path, value))

    assert violators == [], (
        f"handoff record(s) still carry retired status vocabulary not covered "
        f"by the archive/handoffs/ 'consumed' grandfather "
        f"(active anywhere, or consumed outside archive/handoffs/): {violators}"
    )


def test_no_handoff_record_carries_old_deployment_state_vocabulary():
    violators = []
    for path in _iter_handoff_md_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        fm_match = _FRONTMATTER_RE.match(text)
        if not fm_match:
            continue
        ds_match = _DEPLOYMENT_STATE_LINE_RE.search(fm_match.group(1))
        if ds_match and ds_match.group(1).strip() == _OLD_DEPLOYMENT_STATE_VALUE:
            violators.append(path.relative_to(_REPO_ROOT).as_posix())

    assert violators == [], (
        f"handoff record(s) still carry retired deployment_state: "
        f"abandoned: {violators}"
    )


def test_no_handoff_record_carries_old_field_names():
    """consumed_by/consumed_at necessarily accompany a 'consumed' status, so
    they are grandfathered on the identical archive/handoffs/ scope as
    'consumed' itself (see _is_archived_corpus_path / module docstring) —
    otherwise this test would contradict test_no_handoff_record_carries_old_status_vocabulary
    by rejecting the very field pair a grandfathered 'consumed' record must carry.
    """
    violators = []
    for path in _iter_handoff_md_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        fm_match = _FRONTMATTER_RE.match(text)
        if not fm_match:
            continue
        fm_text = fm_match.group(1)
        rel_path = path.relative_to(_REPO_ROOT).as_posix()
        if _is_archived_corpus_path(rel_path):
            continue
        for old_field in _OLD_FIELD_NAMES:
            if re.search(rf"^{old_field}:", fm_text, re.MULTILINE):
                violators.append((rel_path, old_field))

    assert violators == [], (
        f"handoff record(s) still carry retired field name(s) outside the "
        f"archive/handoffs/ grandfather (consumed_by/consumed_at): {violators}"
    )
