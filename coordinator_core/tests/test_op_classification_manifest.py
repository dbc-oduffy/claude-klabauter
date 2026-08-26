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

Spec backlink: pln-coordinator-ops-buildout-from--903224 § AC13
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KILL_LEDGER_PATH = _REPO_ROOT / "state" / "kill-ledger.md"

#: Every kill-ledger entry from K-017 onward titles itself `## K-NNN — \`<op.key>\``
#: (an optional parenthetical may trail the backticked key) -- the convention this
#: parser relies on. Entries that predate it (K-001..K-016) or that kill something
#: other than a single named op (prose titles, multi-op batches like K-056) are NOT
#: recognised here and do not exempt anything; a kill logged outside this exact shape
#: needs its own manifest-row handling, not a silent match.
_KILL_LEDGER_HEADING_RE = re.compile(r"(?m)^## (K-\d+) — `([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)`")
_KILL_LEDGER_STATUS_LANDED_RE = re.compile(r"\*\*Status:\*\*\s+\*\*LANDED\*\*")


def _killed_op_keys() -> frozenset[str]:
    """Op-keys with a LANDED entry in `state/kill-ledger.md` -- evidence-driven, not a
    hardcoded skip-list, so the next kill doesn't need a matching edit here.

    `ceremony.scoped_git_commit` (K-045, DR-344 kill-bar cut, landed `c07062c99`) is
    the case this exists for: `op-classification.tsv` is a frozen 2026-07-22 audit
    record of what was inventoried, correctly never edited to drop a row just because
    its op died later -- but `_OP_KEY_SCOPE` correctly has no entry for a killed op
    either. Both are right; the gate pinning "every manifest key resolves in scope"
    was pinning the wrong property. The live property is: a manifest key resolves in
    scope UNLESS the op it names has a landed kill-ledger entry.
    """
    text = _KILL_LEDGER_PATH.read_text(encoding="utf-8")
    killed: set[str] = set()
    matches = list(_KILL_LEDGER_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entry_body = text[body_start:body_end]
        if _KILL_LEDGER_STATUS_LANDED_RE.search(entry_body):
            killed.add(m.group(2))
    return frozenset(killed)
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


def _manifest_scope_verdict(cell: str) -> str:
    """The scope-verdict column is `<verdict> — <justification>`; the verdict is the
    leading token."""
    return cell.split(" ")[0].strip()


def test_manifest_op_keys_resolve_in_the_scope_table():
    """A manifest op-key that no longer names a registered op ships a dead dotted key to
    claude-central-em's relink map — the fence resolves to nothing and
    `coordinator_core.invoke <key>` fails at the call site, in their repo, after the
    contract was declared frozen. AC13's set-equality is over the audit's kebab op-*names*
    and structurally cannot see this: the key column can drift to a name that was never
    minted (`branch.list_unmerged_work` for the shipped `git_branch.list_unmerged_work`)
    while every op-name still matches the oracle.

    A key absent from `_OP_KEY_SCOPE` is exempted ONLY when `state/kill-ledger.md` carries
    a LANDED entry for it (`_killed_op_keys`) — the manifest itself stays untouched (it is
    a frozen 2026-07-22 audit record, not a live registry projection), so a later kill of
    an inventoried op must not re-break this test, and an unexplained drift still fails it.

    Spec backlink: pln-coordinator-ops-buildout-from--903224 § AC5, § AC13
    """
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    killed = _killed_op_keys()
    unresolved = [
        (row["op-name"], row["op-key"])
        for row in _load_manifest_rows()
        if row["op-key"].strip() not in _OP_KEY_SCOPE and row["op-key"].strip() not in killed
    ]
    assert not unresolved, (
        "manifest op-key(s) absent from _OP_KEY_SCOPE and uncovered by any LANDED "
        "kill-ledger entry (dead cross-repo contract):\n"
        + "\n".join(f"  {name}: {key}" for name, key in unresolved)
    )


def test_killed_op_keys_finds_the_scoped_git_commit_kill():
    """Narrow proof the kill-ledger parser actually works, not just that it exists:
    K-045 (`ceremony.scoped_git_commit`, landed `c07062c99`) must resolve as killed —
    this is the exact case `_killed_op_keys` was built for (see
    `test_manifest_op_keys_resolve_in_the_scope_table`'s docstring)."""
    assert "ceremony.scoped_git_commit" in _killed_op_keys()


def test_manifest_scope_verdicts_match_the_scope_table():
    """The manifest's scope-verdict column is the justification of record for AC5's
    `_OP_KEY_SCOPE` entry. A row justifying `common_dir` against a table entry reading
    `none` means the op is silently told it needs no worktree — the double fail-open the
    plan's § Registration is four surfaces, not one names.

    Spec backlink: pln-coordinator-ops-buildout-from--903224 § AC5
    """
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    mismatches = [
        (row["op-name"], key, declared, _OP_KEY_SCOPE[key])
        for row in _load_manifest_rows()
        for key in (row["op-key"].strip(),)
        if key in _OP_KEY_SCOPE
        for declared in (_manifest_scope_verdict(row["scope-verdict"]),)
        if declared != _OP_KEY_SCOPE[key]
    ]
    assert not mismatches, (
        "manifest scope-verdict disagrees with _OP_KEY_SCOPE:\n"
        + "\n".join(
            f"  {name} ({key}): manifest={declared!r} table={actual!r}"
            for name, key, declared, actual in mismatches
        )
    )
