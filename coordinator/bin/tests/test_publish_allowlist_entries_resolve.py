"""test_publish_allowlist_entries_resolve.py — asserts every positive allowlist
entry in `setup/publish-targets.portable` still resolves to a path on disk.

Why this exists: `build_allowlisted_source`
(`coordinator/lib/percolate/allowlist.py`) is fail-closed by design — AC18(c),
an entry that does not resolve is indistinguishable from a typo'd or
wrong-rooted one, so it ABORTS the row rather than silently narrowing the
publish set. That is the correct behaviour at publish time and it is not what
this test challenges.

What it closes is the blast radius. Nothing else in the repo ties an allowlist
entry to the file it names, so ANY deletion of an allowlisted path arms a
publish refusal that only surfaces the next time a round runs — and a refused
row does not affect the round's exit status, so the round exits 0 with that row
silently unpublished (state/audits/2026-08-16-percolate-round-open-issue-
register.md, issue 7). The failure therefore lands on whoever next reads the
published mirror, not on whoever deleted the file.

That has now happened three times against the `claude_klabauter` mirror:

  - `coordinator_core/chain_ancestry_waivers.py`, deleted in `9d16080dc`, left
    in the `claude-klabauter` row — fixed in `2e3a3b3db`.
  - `coordinator/bin/review-coverage-gate.py`, deleted by K-001 (`55e64be13`),
    left in the `claude-klabauter-coordinator-bin` row (along with an orphaned
    `.cmd` launcher for it).
  - The mirror those two rows feed went SPLIT-VERSION as a result: its
    `wsc-coverage-gate-runner.py` landed from a round where the
    `coordinator_core` row had already started refusing, so the published
    runner imported `ChainAttributionWindow` from a `directives_review.py`
    three commits older than itself. Every repo on the box resolving the
    published engine (`claude_klabauter_root.py :: repos.claude_klabauter`) lost the
    plan-execution lock, the coverage gate, and the chain brightline gate to
    an `ImportError`.

Deletions are cheap and frequent; publish rounds are expensive and rare. This
test moves the signal to the deletion.

Only POSITIVE entries are checked. A negation entry (`!path`) naming a path
that no longer exists is stale but harmless — it removes nothing from a set
that no longer contains it, the same reasoning
`state/lessons/2026-07-05-git-stash-push-pathspec-silently-no-ops.yaml` records
for a pathspec that matches nothing.
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TARGETS_FILE = _REPO_ROOT / "setup" / "publish-targets.portable"

#: 0-indexed field positions in a `publish-targets.portable` row. Field 4 is the
#: source root the allowlist entries in field 7 are resolved against, exactly as
#: `build_allowlisted_source` resolves them (`real_src` + `source_map`).
_FIELD_NAME = 0
_FIELD_SOURCE_REL = 3
_FIELD_ALLOWLIST = 6


def _rows() -> "list[tuple[str, str, list[str]]]":
    """Every row that declares a non-empty allowlist, as
    `(row_name, source_rel, positive_entries)`."""
    rows = []
    for line in _TARGETS_FILE.read_text(encoding="utf8").splitlines():
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
        rows.append((fields[_FIELD_NAME], fields[_FIELD_SOURCE_REL], entries))
    return rows


def test_targets_file_is_present_and_declares_allowlists() -> None:
    """Guards the guard: a parse that silently yields zero rows would make
    every assertion below vacuously true."""
    assert _TARGETS_FILE.is_file(), f"{_TARGETS_FILE} is missing"
    assert _rows(), "parsed no allowlist-bearing rows — the field layout drifted"


@pytest.mark.parametrize("row_name,source_rel,entries", _rows(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_positive_allowlist_entry_resolves(
    row_name: str, source_rel: str, entries: "list[str]"
) -> None:
    source_root = _REPO_ROOT / Path(source_rel)
    missing = [e for e in entries if not (source_root / Path(e)).exists()]
    assert not missing, (
        f"publish row {row_name!r} allowlists {len(missing)} path(s) that no longer "
        f"exist under {source_rel}: {', '.join(sorted(missing))}. "
        f"build_allowlisted_source is fail-closed, so this row will REFUSE on the "
        f"next publish round and the round will still exit 0 — leaving the mirror "
        f"silently stale, or split-version if a sibling row into the same mirror "
        f"still publishes. Either restore the path or drop the entry."
    )
