"""Coverage pin for the cloud guard party-screen audit against the LIVE registration.

Spec: docs/plans/2026-09-06-the-party-screen-over-93-guard-bodies.md, chunk C6.

Reads `bash_guards.roster.guard_roster()` and `write_guards.engine.discover_guard_names()`
directly (never a committed intermediary — the shard files under
`state/audits/2026-09-06-cloud-guard-party-screen/` are inputs to the fold, not the source of
truth this test checks against) and asserts, against the committed fold
`state/audits/2026-09-06-cloud-guard-party-screen.md`:

  1. Every id in the live union appears exactly once in the audit's table.
  2. Every row's provenance bucket is one of the three sanctioned buckets.
  3. Every bucket-1 (`enforced-here-and-stated-here`) row's `stated_at` citation exists in this
     repo and does not resolve under a sibling `DoE-claude` checkout.
  4. Every classified row's `binds_harder_in_cloud` is `yes` or `no`, never `n/a`.
  5. Every premise-false row (`cloud verdict` containing "premise-false") has a populated
     `safe_direction`, and no OTHER row has one populated.
  6. Grouping rows by their declared `rule_family` key, every family resolves to exactly ONE
     provenance bucket — failing loudly on a split rather than silently averaging it away.

Negative-spec: this test does NOT re-derive party/verdict judgments itself (that is the fold's
own authored content, not a mechanically-checkable property) — it pins STRUCTURE: coverage,
bucket well-formedness, citation resolvability, and the rule_family/bucket partition.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath

import pytest

from coordinator_core.bash_guards.roster import guard_roster
from coordinator_core.write_guards.engine import discover_guard_names

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = REPO_ROOT / "state" / "audits" / "2026-09-06-cloud-guard-party-screen.md"

BUCKETS = frozenset(
    {
        "enforced-here-and-stated-here",
        "enforced-here-stated-in-DoE-doctrine",
        "enforced-here-stated-by-PM-ruling",
    }
)

_HEADER_PREFIX = "| guard id |"


def _split_row(line: str) -> list[str]:
    """Split one markdown table row into cells, respecting backslash-escaped pipes."""
    safe = line.replace("\\|", "\x00PIPE\x00")
    cells = [c.replace("\x00PIPE\x00", "\\|").strip() for c in safe.strip().strip("|").split("|")]
    return cells


def _load_audit_rows() -> list[dict[str, str]]:
    text = AUDIT_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith(_HEADER_PREFIX))
    header = [c.lower() for c in _split_row(lines[header_idx])]
    rows = []
    for line in lines[header_idx + 2 :]:
        if not line.startswith("|"):
            if rows:
                # table ended
                break
            continue
        if "---" in line:
            continue
        cells = _split_row(line)
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _bare(cell: str) -> str:
    return cell.strip().strip("`").strip()


@pytest.fixture(scope="module")
def live_guard_ids() -> set[str]:
    bash_ids = {g.id for g in guard_roster()}
    write_ids, errors = discover_guard_names()
    assert not errors, "write_guards.engine.discover_guard_names() reported errors: %r" % (errors,)
    return bash_ids | set(write_ids)


@pytest.fixture(scope="module")
def audit_rows() -> list[dict[str, str]]:
    assert AUDIT_PATH.exists(), "committed audit missing: %s" % AUDIT_PATH
    rows = _load_audit_rows()
    assert rows, "parsed zero rows out of the audit table at %s" % AUDIT_PATH
    return rows


def test_audit_covers_every_live_guard_exactly_once(live_guard_ids, audit_rows):
    audit_ids = [_bare(r["guard id"]) for r in audit_rows]
    audit_id_set = set(audit_ids)

    missing = live_guard_ids - audit_id_set
    assert not missing, "guards registered live but absent from the audit: %r" % sorted(missing)

    extra = audit_id_set - live_guard_ids
    assert not extra, "audit rows for guard ids not in the live registration: %r" % sorted(extra)

    dupes = {gid for gid in audit_ids if audit_ids.count(gid) > 1}
    assert not dupes, "guard id(s) appear more than once in the audit table: %r" % sorted(dupes)


def test_every_row_provenance_bucket_is_sanctioned(audit_rows):
    bad = []
    for r in audit_rows:
        bucket = _bare(r["provenance bucket"])
        if bucket not in BUCKETS:
            bad.append((r["guard id"], bucket))
    assert not bad, "row(s) with an unsanctioned provenance bucket: %r" % bad


def test_bucket1_rows_carry_a_resolvable_in_repo_citation(audit_rows):
    bad = []
    for r in audit_rows:
        bucket = _bare(r["provenance bucket"])
        if bucket != "enforced-here-and-stated-here":
            continue
        stated_at = _bare(r.get("stated_at", ""))
        gid = _bare(r["guard id"])
        if not stated_at or stated_at == "-":
            bad.append((gid, "no stated_at citation"))
            continue
        if "DoE-claude" in stated_at:
            bad.append((gid, "stated_at resolves under DoE-claude: %s" % stated_at))
            continue
        # An ABSOLUTE stated_at silently discards REPO_ROOT under
        # `Path.__truediv__`, so a citation could resolve anywhere on the box
        # -- including inside a DoE-claude checkout whose path does not contain
        # the literal string checked above (Review: code-reviewer, slice 1).
        if PurePath(stated_at).is_absolute() or (len(stated_at) > 1 and stated_at[1] == ":"):
            bad.append((gid, "stated_at must be repo-relative, not absolute: %s" % stated_at))
            continue
        candidate = REPO_ROOT / stated_at
        # `.is_file()`, not `.exists()`: a citation naming a DIRECTORY states no
        # rule and would otherwise pass (Review: code-reviewer, slice 1).
        if not candidate.is_file():
            bad.append((gid, "stated_at does not name a file in this repo: %s" % stated_at))
    assert not bad, "bucket-1 citation defects: %r" % bad


def test_binds_harder_in_cloud_is_always_yes_or_no(audit_rows):
    bad = []
    for r in audit_rows:
        val = _bare(r["binds_harder_in_cloud"]).lower()
        if val not in ("yes", "no"):
            bad.append((r["guard id"], val))
    assert not bad, "row(s) with a non yes/no binds_harder_in_cloud: %r" % bad


def test_safe_direction_populated_iff_premise_false(audit_rows):
    missing_safe_dir = []
    unexpected_safe_dir = []
    for r in audit_rows:
        verdict = r.get("cloud verdict", "").lower()
        is_premise_false = "premise-false" in verdict or "premise-narrows" in verdict
        safe_dir = _bare(r.get("safe_direction", ""))
        has_safe_dir = bool(safe_dir) and safe_dir != "-"
        if is_premise_false and not has_safe_dir:
            missing_safe_dir.append(r["guard id"])
        if not is_premise_false and has_safe_dir:
            unexpected_safe_dir.append((r["guard id"], safe_dir))
    assert not missing_safe_dir, (
        "premise-false/premise-narrows row(s) missing safe_direction: %r" % missing_safe_dir
    )
    assert not unexpected_safe_dir, (
        "row(s) with a populated safe_direction despite a non premise-false verdict: %r"
        % unexpected_safe_dir
    )


def test_rule_family_partitions_into_exactly_one_provenance_bucket(audit_rows):
    family_buckets: dict[str, set[str]] = {}
    for r in audit_rows:
        fam = _bare(r["rule_family"])
        bucket = _bare(r["provenance bucket"])
        family_buckets.setdefault(fam, set()).add(bucket)

    splits = {fam: sorted(buckets) for fam, buckets in family_buckets.items() if len(buckets) > 1}
    assert not splits, "rule_family key(s) that split across more than one provenance bucket: %r" % splits


def test_pm_gates_roadmap_covers_every_bucket3_row():
    pm_gates_path = REPO_ROOT / "state" / "roadmap" / "cloud-em-2026-09-06" / "pm-gates.md"
    assert pm_gates_path.exists(), "pm-gates.md projection target missing: %s" % pm_gates_path
    pm_gates_text = pm_gates_path.read_text(encoding="utf-8")

    rows = _load_audit_rows()
    bucket3_ids = [
        _bare(r["guard id"]) for r in rows if _bare(r["provenance bucket"]) == "enforced-here-stated-by-PM-ruling"
    ]
    assert bucket3_ids, "expected at least one bucket-3 row in the audit to project"

    missing = [gid for gid in bucket3_ids if ("`%s`" % gid) not in pm_gates_text]
    assert not missing, "bucket-3 guard id(s) absent from pm-gates.md: %r" % missing

    assert re.search(r"DR-344", pm_gates_text), "pm-gates.md missing the DR-344 headline row"

    # The REVERSE implication, which the forward check above cannot see: a row
    # rebucketed AWAY from bucket-3 leaves a stale PM-ruling entry in the
    # projection, still telling a PM they have a decision to make about a rule
    # the audit no longer says is theirs. Not hypothetical -- one row WAS
    # rebucketed this session (`nudge_windows_subprocess_popup`, 253d973d6e),
    # in a direction this leg would have caught had it gone the other way
    # (Review: code-reviewer, slice 1).
    all_ids = {_bare(r["guard id"]) for r in rows}
    bucket3 = set(bucket3_ids)
    stale = [
        gid
        for gid in all_ids - bucket3
        if ("`%s`" % gid) in pm_gates_text
    ]
    assert not stale, (
        "pm-gates.md names guard id(s) the audit no longer records as bucket-3 "
        "(enforced-here-stated-by-PM-ruling): %r" % sorted(stale)
    )

def test_both_party_columns_are_populated_on_every_row(audit_rows):
    """The two party columns are the audit's whole point, and until this test
    existed nothing pinned them: bucket, citation, verdict and
    `binds_harder_in_cloud` were all asserted while a row could ship with both
    party cells empty and keep the suite green (found by the close-out
    criterion-only read). A row that can name no party is a FINDING, recorded
    in the audit's party-less section as `NONE-FOUND` -- which is a value, not
    a blank, so it satisfies this test while staying visible as unclassified."""
    bad = []
    for r in audit_rows:
        gid = _bare(r["guard id"])
        for col in ("party premise states", "party that bears harm"):
            cell = _bare(r.get(col, ""))
            if col not in r:
                bad.append((gid, col, "COLUMN ABSENT"))
                continue
            if not cell or cell in ("-", "n/a", "n-a", "tbd"):
                bad.append((gid, col, cell))
    assert not bad, "row(s) with an unpopulated party column: %r" % bad
