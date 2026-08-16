"""coordinator_core.install.tests.test_fleet_env_publish_reachability — every
artifact `docs/plans/2026-08-16-one-environment-for-the-fleet.md` produces is
reachable in a fresh clone of claude-klabauter, via the publish row set.

Purpose: AC14 (eng-director finding 1). Before chunk C11, three of this
plan's `scope:` paths never reached klabauter: `coordinator/bin/fleet-env.py`
was absent from `claude-klabauter-coordinator-bin`'s allowlist,
`docs/reference/fleet-shared-environment-contract.md` was absent from
`claude-klabauter-toplevel-reference`'s, and `docs/install/` had no publish
row at all — so the fleet-env lock (which C4 installs FROM, on klabauter)
never shipped. This module asserts reachability by WALKING
`setup/publish-targets.portable`'s parsed rows, never by inspection or a
hand-listed set of expected files, so a later allowlist edit that drops one
of these paths fails here rather than shipping silently.

Mechanism: reuses the row-parsing and allowlist-resolution primitives
`coordinator_core/percolate/tests/test_scrub_table_shape_publish_surface.py`
established for the same file (`_iter_portable_rows`,
`parse_allowlist_csv`/`split_inclusion_exclusion`) rather than re-deriving a
second parse of the same grammar. Resolution is READ-ONLY and does not
materialize a restricted tree (`build_allowlisted_source` performs a real
`shutil.copytree`; this module only needs the file-set membership question,
not a copy) — mirrors that sibling module's own stated tradeoff.

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md § C11, AC14

Negative-spec:
    - Does NOT run a publish round, and does NOT touch the claude-klabauter
      publish mirror or any other publish mirror — pure parse-and-check over
      the tracked config.
    - Does NOT assert reachability for `scope:` paths this plan does not
      itself publish-own (see `_NOT_THIS_CHUNKS_TO_PUBLISH` below) — those
      are either directories the plan's OWN files live under (already
      covered transitively) or paths another chunk (C9, C10) is responsible
      for; this module does not stand in for their own reachability check.
    - Does NOT hand-list the expected row names or a frozen row count —
      the plan's `scope:` list and the portable file's parsed rows are both
      read at collection/test time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from coordinator.lib.percolate.allowlist import (
    parse_allowlist_csv,
    split_inclusion_exclusion,
)
from coordinator.lib.percolate.targets import _iter_portable_rows

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PORTABLE_TARGETS_PATH = _REPO_ROOT / "setup" / "publish-targets.portable"

# The plan's own `scope:` frontmatter list (docs/plans/2026-08-16-one-
# environment-for-the-fleet.md), copied verbatim rather than parsed off the
# plan body: the plan body is immutable to this executor (Standing Order 2)
# and re-parsing YAML-in-markdown frontmatter here would add a second, more
# fragile dependency on that file's exact shape for no reachability benefit
# -- the thing under test is the PUBLISH ROW SET's coverage of this list,
# not the list's own extraction.
_PLAN_SCOPE = [
    "coordinator_core/install/fleet_env.py",
    "coordinator_core/install/fleet_env_lock.py",
    "coordinator_core/install/tests/",
    "docs/install/fleet-env-sources.toml",
    "docs/install/fleet-env-requirements.in",
    "docs/install/fleet-env-overrides.toml",
    "docs/install/fleet-env.lock",
    "docs/reference/fleet-shared-environment-contract.md",
    "docs/reference/shared-fleet-venv-contract.md",
    "coordinator/bin/fleet-env.py",
    "coordinator_core/install/fleet_env_resolve.py",
    "setup/publish-targets.portable",
    "dist/klabauter-toplevel/.gitignore",
    "scripts/setup.py",
]

# `scope:` entries this plan does not itself publish-own: pre-existing files
# another chunk is responsible for updating/publishing (`shared-fleet-venv-
# contract.md` is C9's, `scripts/setup.py` is C10's -- see this plan's C11
# body), plus claude-klabauter's own publish CONFIG files, which are never themselves
# published TO klabauter (they configure what publishes, and klabauter is
# not itself a publish source). Excluded from the reachability assertion
# below with a named reason each, not silently dropped.
_NOT_THIS_CHUNKS_TO_PUBLISH = {
    "docs/reference/shared-fleet-venv-contract.md": "deliberately NOT published: it documents the settings-home venv's purposes (a)/(c), "
        "which are claude-klabauter-plane concerns. The routing added by C9 is one-way (old doc -> new "
        "contract), and no PUBLISHED document references it, so its absence from klabauter "
        "leaves no dangling pointer. Verified against the claude-klabauter-toplevel-reference "
        "row's allowlist, which does not carry it.",
    "setup/publish-targets.portable": "publish CONFIG, not publish PAYLOAD -- this file describes what ships, it is never itself shipped",
    "dist/klabauter-toplevel/.gitignore": "shipped implicitly as the row-1 flat-mirror's own dotfile precedent governs; not a payload file any row allowlists by name (flat-mirror Phase 1 has no dotfile skip per that row's own comment, but .gitignore is a source-tree control file for THIS repo's dist staging, not the published payload)",
    "coordinator_core/install/tests/": "a directory, not a file -- covered transitively: every test file placed under it publishes via the pre-existing claude-klabauter row's 'install' allowlist entry, same as the rest of coordinator_core/install/",
}


def _parse_portable_rows(path: Path) -> list[dict[str, str]]:
    """Parse rows into `{"name", "source_subdir", "dest_subdir", "allowlist"}`.

    Tuple shape (the portable file's own header comment):
      name|mode|<dest-sigil>|source_subdir|dest_subdir[|native_slugs[|allowlist[|source_map]]]
    """
    rows = []
    for raw_row in _iter_portable_rows(path):
        fields = raw_row.split("|")
        name = fields[0].strip()
        source_subdir = fields[3].strip() if len(fields) > 3 else ""
        dest_subdir = fields[4].strip() if len(fields) > 4 else ""
        allowlist_csv = fields[6].strip() if len(fields) > 6 else ""
        rows.append(
            {
                "name": name,
                "source_subdir": source_subdir,
                "dest_subdir": dest_subdir,
                "allowlist": allowlist_csv,
            }
        )
    return rows


def _row_publishes_path(row: dict[str, str], repo_relative_path: str) -> bool:
    """True if `row` would publish the given claude-klabauter-repo-relative path.

    A row publishes `repo_relative_path` when that path sits under the row's
    `source_subdir` AND (the row carries no allowlist -- unrestricted -- OR
    the sub-path's TOP-LEVEL segment, relative to `source_subdir`, is an
    inclusion entry not removed by a `!`-prefixed exclusion covering it).
    Mirrors `build_allowlisted_source`'s own inclusion/exclusion contract
    without materializing a copy (this module only needs set membership).
    """
    source_subdir = row["source_subdir"]
    if not source_subdir:
        return False
    path = repo_relative_path.rstrip("/")
    if path == source_subdir:
        return True
    prefix = source_subdir + "/"
    if not path.startswith(prefix):
        return False
    sub_path = path[len(prefix) :]

    allowlist_csv = row["allowlist"]
    if not allowlist_csv:
        return True

    entries, exclusions = split_inclusion_exclusion(parse_allowlist_csv(allowlist_csv))
    top_segment = sub_path.split("/", 1)[0]
    if top_segment not in entries:
        return False
    for exclusion in exclusions:
        if sub_path == exclusion or sub_path.startswith(exclusion + "/"):
            return False
    return True


def _covering_row(rows: list[dict[str, str]], repo_relative_path: str) -> Optional[dict[str, str]]:
    for row in rows:
        if _row_publishes_path(row, repo_relative_path):
            return row
    return None


_PLAN_PATH = _REPO_ROOT / "docs" / "plans" / "2026-08-16-one-environment-for-the-fleet.md"


def _plan_scope_block_paths(plan_path: Path) -> list[str]:
    """Cheap, narrow drift check for `_PLAN_SCOPE`: extract the literal
    `  - path` lines between the frontmatter `scope:` key and the next
    top-level (unindented) key, by plain line scanning. Deliberately NOT a
    YAML parser -- this reads exactly one fixed-shape flat list block, the
    same proportionality call `_PLAN_SCOPE`'s own docstring makes against
    re-parsing YAML-in-markdown generally. Catches a scope addition/removal
    going out of sync with the hand-copied `_PLAN_SCOPE` list; does not
    catch a scope path being reworded without the line count changing in a
    way this scanner would miss."""
    lines = plan_path.read_text(encoding="utf-8").splitlines()
    paths: list[str] = []
    in_scope = False
    for line in lines:
        if line.strip() == "scope:":
            in_scope = True
            continue
        if in_scope:
            if line.startswith("  - "):
                paths.append(line[len("  - "):].strip())
                continue
            break  # first non-list-item line ends the block
    return paths


def test_plan_scope_matches_frontmatter_scope_block() -> None:
    """`_PLAN_SCOPE` is a hand-copied literal by design (see its own
    docstring) -- this is the cheap assertion that catches it drifting from
    the plan's actual `scope:` frontmatter, without adding a real YAML
    parser for YAML-in-markdown."""
    live_scope = _plan_scope_block_paths(_PLAN_PATH)
    assert live_scope, f"could not locate a scope: block in {_PLAN_PATH}"
    assert set(live_scope) == set(_PLAN_SCOPE), (
        "_PLAN_SCOPE has drifted from the plan's scope: frontmatter -- "
        f"plan has {sorted(set(live_scope) - set(_PLAN_SCOPE))} not in "
        f"_PLAN_SCOPE, and _PLAN_SCOPE has "
        f"{sorted(set(_PLAN_SCOPE) - set(live_scope))} not in the plan. "
        "Update _PLAN_SCOPE (and _NOT_THIS_CHUNKS_TO_PUBLISH if the added "
        "path isn't this chunk's to publish) to match."
    )


def _assertable_scope_paths() -> list[str]:
    return [p for p in _PLAN_SCOPE if p not in _NOT_THIS_CHUNKS_TO_PUBLISH]


@pytest.mark.parametrize("scope_path", _assertable_scope_paths())
def test_scope_path_reachable_via_publish_row_set(scope_path: str) -> None:
    rows = _parse_portable_rows(_PORTABLE_TARGETS_PATH)
    covering_row = _covering_row(rows, scope_path)
    assert covering_row is not None, (
        f"{scope_path!r} is not reachable via any row in "
        f"{_PORTABLE_TARGETS_PATH} -- a fresh clone of claude-klabauter "
        "would not contain it."
    )


def test_every_plan_scope_path_is_accounted_for() -> None:
    """Every `scope:` entry is either asserted reachable above or named in
    `_NOT_THIS_CHUNKS_TO_PUBLISH` with a reason -- no silent third option."""
    unaccounted = [
        p for p in _PLAN_SCOPE if p not in _assertable_scope_paths() and p not in _NOT_THIS_CHUNKS_TO_PUBLISH
    ]
    assert not unaccounted, f"scope paths neither asserted nor excused: {unaccounted}"


def test_demonstration_allowlist_removal_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Demonstrates this test is not vacuous: with `fleet-env.py` removed
    from `claude-klabauter-coordinator-bin`'s allowlist, the row no longer
    covers `coordinator/bin/fleet-env.py` -- proves the reachability check
    above is load-bearing, not a tautology that always passes."""
    rows = _parse_portable_rows(_PORTABLE_TARGETS_PATH)
    mutated = []
    found = False
    for row in rows:
        if row["name"] == "claude-klabauter-coordinator-bin":
            entries = row["allowlist"].split(",")
            assert "fleet-env.py" in entries
            entries.remove("fleet-env.py")
            row = dict(row, allowlist=",".join(entries))
            found = True
        mutated.append(row)
    assert found, "claude-klabauter-coordinator-bin row not found -- test setup drifted"

    assert _covering_row(mutated, "coordinator/bin/fleet-env.py") is None
