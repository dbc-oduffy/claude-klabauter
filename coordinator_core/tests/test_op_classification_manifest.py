"""AC13 enforcement — `op-classification.tsv` stays a projection of the audit oracle.

Wave 0 (C0a, plan `docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md`
DEC-5) re-derives the 64-row EXTEND/NEW classification into a checked-in manifest at
`state/audits/2026-07-22-command-payload-inventory/op-classification.tsv`. That manifest is
the coverage oracle every build wave consumes — a hand-verified "we checked it once" claim is
exactly the failure mode that produced the plan's original 18-row hole (the plan-coverage
check found 18 of the 64 in-scope rows silently absent from the plan body). This module is the
red-on-drift proof the plan's own AC13 mandates: the manifest is continuously re-derived from
`distinct-ops-new.tsv` (155 rows: 79 DELETE + 76 NEW-CLAUDE-KLABAUTER) minus the 12 named ALREADY-EXISTS
ops, and any manifest edit that drops a row, adds a stray row, blanks a required cell, or
collides two op-keys fails this test rather than silently drifting from the oracle.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § AC13
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_DIR = _REPO_ROOT / "state" / "audits" / "2026-07-22-command-payload-inventory"
_DISTINCT_OPS_TSV = _AUDIT_DIR / "distinct-ops-new.tsv"
_MANIFEST_TSV = _AUDIT_DIR / "op-classification.tsv"

# The 12 ops the 2026-07-22 cross-reference against the existing 309-module
# `coordinator_core/ops/` tree confirmed ALREADY-EXISTS — out of scope for this plan's
# 64-row build. Named here (audit's own kebab names) rather than derived, because their
# whole point is that they are NOT present in either the NEW-CLAUDE-KLABAUTER verdict rows this test
# reads or the in-scope manifest — there is nothing in either file to derive them from.
_ALREADY_EXISTS_12 = frozenset(
    {
        "resolve-registry-clone-path",
        "invoke-claude-klabauter-install-module",
        "probe-bash-version",
        "offer-brew-bash-install-macos",
        "enable-git-lfs",
        "probe-pwsh-version",
        "install-python-package-idempotent",
        "init-chain-walk-visited-set",
        "validate-paired-override-flags",
        "probe-plugin-enabled-in-settings",
        "probe-hooks-present-vs-manifest",
        "seed-machine-local-registry-from-example",
    }
)

_MANIFEST_HEADER = (
    "op-name",
    "verdict",
    "target",
    "wave",
    "idempotency-hazard",
    "platform-hazard",
    "scope-verdict",
    "op-key",
    "contract",
)

_VALID_WAVES = frozenset({"1", "2", "3"})


def _read_distinct_ops_new_op_names() -> list[str]:
    """Parse `distinct-ops-new.tsv` (no header row) and return op-name column values
    filtered to verdict column == NEW-CLAUDE-KLABAUTER. Column layout (1-indexed):
    1=op-name, 2=description, 3=fence-count, 4=locations, 5=verdict, 6=idempotency-risk,
    7=platform-risk, 8=notes.
    """
    assert _DISTINCT_OPS_TSV.is_file(), f"missing oracle file: {_DISTINCT_OPS_TSV}"
    op_names: list[str] = []
    with open(_DISTINCT_OPS_TSV, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            cols = line.split("\t")
            assert len(cols) == 8, (
                f"{_DISTINCT_OPS_TSV.name}:{lineno}: expected 8 tab-separated columns, "
                f"found {len(cols)}"
            )
            op_name, verdict = cols[0], cols[4]
            if verdict == "NEW-CLAUDE-KLABAUTER":
                op_names.append(op_name)
    return op_names


def _derived_in_scope_op_names() -> set[str]:
    """The 76 NEW-CLAUDE-KLABAUTER op-names minus the 12 named ALREADY-EXISTS ops."""
    new_claude_klabauter = set(_read_distinct_ops_new_op_names())
    assert len(new_claude_klabauter) == 76, (
        f"expected exactly 76 distinct NEW-CLAUDE-KLABAUTER op-names in {_DISTINCT_OPS_TSV.name}, "
        f"found {len(new_claude_klabauter)} — the oracle itself has drifted from the plan's stated "
        f"counts (79 DELETE + 76 NEW-CLAUDE-KLABAUTER = 155)"
    )
    missing_already_exists = _ALREADY_EXISTS_12 - new_claude_klabauter
    assert not missing_already_exists, (
        "the following named ALREADY-EXISTS ops are not present among the oracle's "
        f"NEW-CLAUDE-KLABAUTER rows (renamed/removed upstream?): {sorted(missing_already_exists)}"
    )
    return new_claude_klabauter - _ALREADY_EXISTS_12


def _load_manifest_rows() -> list[dict[str, str]]:
    assert _MANIFEST_TSV.is_file(), f"missing manifest: {_MANIFEST_TSV}"
    with open(_MANIFEST_TSV, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n").rstrip("\r") for ln in f if ln.strip("\n\r") != ""]

    assert lines, f"{_MANIFEST_TSV.name} is empty"
    header = tuple(lines[0].split("\t"))
    assert header == _MANIFEST_HEADER, (
        f"{_MANIFEST_TSV.name} header row mismatch.\n"
        f"  expected: {_MANIFEST_HEADER}\n"
        f"  found:    {header}"
    )

    rows: list[dict[str, str]] = []
    for lineno, line in enumerate(lines[1:], start=2):
        cols = line.split("\t")
        assert len(cols) == len(_MANIFEST_HEADER), (
            f"{_MANIFEST_TSV.name}:{lineno}: expected {len(_MANIFEST_HEADER)} "
            f"tab-separated columns, found {len(cols)}"
        )
        rows.append(dict(zip(_MANIFEST_HEADER, cols)))
    return rows


def test_derived_in_scope_count_is_exactly_64():
    """76 NEW-CLAUDE-KLABAUTER rows minus the 12 named ALREADY-EXISTS ops must be exactly 64 —
    this is the plan's own headline number (AC1/AC13), re-derived rather than trusted."""
    derived = _derived_in_scope_op_names()
    assert len(derived) == 64, (
        f"expected 76 - 12 = 64 in-scope ops, derived {len(derived)}. "
        f"Either the oracle's NEW-CLAUDE-KLABAUTER count moved, or an ALREADY-EXISTS op-name in "
        f"_ALREADY_EXISTS_12 no longer matches a row in {_DISTINCT_OPS_TSV.name}."
    )


def test_manifest_set_equals_derived_oracle():
    """op-classification.tsv's op-name column must be exactly the derived 64-row set —
    no manifest row absent from the oracle, no oracle row absent from the manifest."""
    derived = _derived_in_scope_op_names()
    manifest_rows = _load_manifest_rows()
    manifest_op_names = [row["op-name"] for row in manifest_rows]

    manifest_op_name_set = set(manifest_op_names)
    missing_from_manifest = derived - manifest_op_name_set
    extra_in_manifest = manifest_op_name_set - derived

    assert not missing_from_manifest, (
        f"{len(missing_from_manifest)} oracle-derived op(s) are absent from "
        f"{_MANIFEST_TSV.name}: {sorted(missing_from_manifest)}"
    )
    assert not extra_in_manifest, (
        f"{len(extra_in_manifest)} manifest op(s) are not in the oracle-derived 64-row "
        f"set: {sorted(extra_in_manifest)}"
    )

    duplicates = sorted(
        {name for name in manifest_op_names if manifest_op_names.count(name) > 1}
    )
    assert not duplicates, f"duplicate op-name row(s) in {_MANIFEST_TSV.name}: {duplicates}"


def test_manifest_row_count_is_exactly_64():
    rows = _load_manifest_rows()
    assert len(rows) == 64, (
        f"{_MANIFEST_TSV.name} has {len(rows)} data row(s), expected exactly 64"
    )


def test_manifest_required_cells_nonempty():
    """Every row must carry a non-empty verdict/target/wave/op-key — an empty cell in any
    of these is a hole in Wave 1-3's coverage oracle, not a cosmetic gap."""
    rows = _load_manifest_rows()
    required = ("verdict", "target", "wave", "op-key")
    violations = []
    for row in rows:
        for field in required:
            if not row[field].strip():
                violations.append(f"{row.get('op-name', '<unknown>')}: empty '{field}'")
    assert not violations, "manifest rows with empty required cells:\n" + "\n".join(
        violations
    )


def test_manifest_verdicts_and_waves_are_valid():
    rows = _load_manifest_rows()
    bad_verdicts = [
        (row["op-name"], row["verdict"])
        for row in rows
        if row["verdict"] not in ("EXTEND", "NEW")
    ]
    assert not bad_verdicts, f"rows with an invalid verdict (not EXTEND/NEW): {bad_verdicts}"

    bad_waves = [
        (row["op-name"], row["wave"]) for row in rows if row["wave"] not in _VALID_WAVES
    ]
    assert not bad_waves, f"rows with an invalid wave (not 1/2/3): {bad_waves}"


def test_manifest_op_keys_are_unique():
    """A colliding op-key ships a broken cross-repo contract to claude-central-em's relink
    map — two fences resolving to the same dotted key is a silent last-registration-wins
    dispatch bug, not a cosmetic duplicate."""
    rows = _load_manifest_rows()
    op_keys = [row["op-key"] for row in rows]
    seen: dict[str, str] = {}
    collisions = []
    for row in rows:
        key = row["op-key"]
        if key in seen and seen[key] != row["op-name"]:
            collisions.append((key, seen[key], row["op-name"]))
        else:
            seen[key] = row["op-name"]
    assert not collisions, f"duplicate op-key(s) across distinct rows: {collisions}"
    assert len(set(op_keys)) == len(op_keys), (
        "op-key column is not unique across all manifest rows"
    )
