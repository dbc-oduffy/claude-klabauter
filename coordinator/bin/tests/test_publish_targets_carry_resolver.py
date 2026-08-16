"""test_publish_targets_carry_resolver.py — C1
(docs/plans/2026-08-16-one-engine-for-the-whole-box.md): asserts the publish
allowlist in `setup/publish-targets.portable` carries `coordinator_core/
claude_klabauter_root.py` and every file under `coordinator/lib/resolve-claude-klabauter/` — the
two paths `_load_shim()` needs on a repointed session, whose absence raises
its "broken or partial claude-klabauter checkout" RuntimeError.

Verified against the LIVE file at dispatch time (2026-08-16): both paths are
already carried, and have been since `3897f3076` / `2b9e319aa` (2026-08-10) —
`coordinator_core/claude_klabauter_root.py` as a top-level entry on the `claude-klabauter`
row, `resolve-claude-klabauter` as a top-level directory entry on both `claude-klabauter-lib`
and `claude-klabauter-coordinator-lib`. This test parses the manifest fresh on
every run (never string-matches the file, so a future edit that drops either
entry fails loud here) rather than asserting a snapshot.

The plan's own AC1 draws the line this test does NOT cross: "a publish round
lands them in the mirror. Verified against the mirror's tracked file set, not
against the allowlist text." Confirmed live against the mirror
(`machine_resolver.registry_get("publish.mirrors.claude_klabauter.path")`,
2026-08-16): `resolve-claude-klabauter/` is already live there under its rename
(`resolve-claude-klabauter/`), but `claude_klabauter_root.py` is not — the allowlist
entry predates the last publish round that would carry it. That gap is a
publish-round execution gap, not an allowlist gap, and is out of this test's
(and this chunk's) scope — see C2.

Field-layout convention and row-parsing shape follow
`test_publish_allowlist_entries_resolve.py`'s own `_rows()` exactly (field
indices 0/3/6, comma-split allowlist, `!`-prefixed exclusions dropped) rather
than reimplementing a second parser with its own drift risk.

Run: python -m pytest coordinator/bin/tests/test_publish_targets_carry_resolver.py -q -p no:cacheprovider --maxfail=1
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TARGETS_FILE = _REPO_ROOT / "setup" / "publish-targets.portable"

#: 0-indexed field positions in a `publish-targets.portable` row — see that
#: file's own header comment for the full tuple-shape grammar.
_FIELD_NAME = 0
_FIELD_SOURCE_REL = 3
_FIELD_ALLOWLIST = 6

#: The two paths C1 must find covered, repo-root-relative, POSIX-separated.
_MUST_BE_CARRIED = (
    "coordinator_core/claude_klabauter_root.py",
    "coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py",
)


def _rows() -> "list[tuple[str, str, list[str]]]":
    """Every row that declares a non-empty allowlist, as
    `(row_name, source_rel, positive_entries)` — positive entries only,
    `!`-prefixed exclusions dropped (they narrow a *different* row's
    coverage, not this one's)."""
    rows = []
    for line in _TARGETS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) <= _FIELD_ALLOWLIST:
            continue
        allowlist = fields[_FIELD_ALLOWLIST].strip()
        if not allowlist:
            continue
        entries = [
            e.strip()
            for e in allowlist.split(",")
            if e.strip() and not e.strip().startswith("!")
        ]
        rows.append((fields[_FIELD_NAME], fields[_FIELD_SOURCE_REL].strip(), entries))
    return rows


def _is_carried(target_rel: str, rows: "list[tuple[str, str, list[str]]]") -> bool:
    """True if some row's `source_rel` is a prefix of `target_rel` AND the
    remainder's top-level path segment (file or directory) is one of that
    row's positive allowlist entries.

    A directory entry (e.g. `resolve-claude-klabauter`) is treated as covering
    everything beneath it — matching the engine's own mirror-mode semantics
    (`_sync_mirror_top_level_files` plus the main subdirectory copy loop),
    not merely the bare directory name."""
    for _, source_rel, entries in rows:
        prefix = source_rel.rstrip("/") + "/"
        if not target_rel.startswith(prefix):
            continue
        remainder = target_rel[len(prefix):]
        top_level = remainder.split("/", 1)[0]
        if top_level in entries:
            return True
    return False


def test_targets_file_is_present_and_declares_allowlists() -> None:
    """Guards the guard: a parse that silently yields zero rows would make
    the coverage assertion below vacuously true."""
    assert _TARGETS_FILE.is_file(), f"{_TARGETS_FILE} is missing"
    assert _rows(), "parsed no allowlist-bearing rows — the field layout drifted"


def test_claude_klabauter_root_and_resolve_claude_klabauter_are_allowlisted() -> None:
    rows = _rows()
    missing = [path for path in _MUST_BE_CARRIED if not _is_carried(path, rows)]
    assert not missing, (
        f"the following path(s) needed by a repointed session's `_load_shim()` are "
        f"not covered by any allowlist row in {_TARGETS_FILE}: {', '.join(missing)}. "
        f"A repointed session hits _load_shim()'s \"broken or partial claude-klabauter "
        f"checkout\" RuntimeError until every one of these is admitted by the row "
        f"governing its path."
    )
