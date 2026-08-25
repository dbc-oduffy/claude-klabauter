"""
coordinator_core.ops.tests.test_completion_ops_chain_widening_normalization —
coverage for the two chain-widening normalization fixes in
``_collect_chain_session_ids``.

BUG: ``chain:`` frontmatter values carry a ``YYYY-MM-DD-`` prefix; sibling
``workstream:`` values on handoffs never do. A literal-equality comparison
between the two therefore found zero siblings on every entry (confirmed
empirically on the entries a 2026-07-26 repair session touched).

FIX HALF 1: strip the leading date prefix from ``chain:``/``chain_slug``
before comparing (strict, invertible — a no-op when no prefix is present).

FIX HALF 2: a SEPARATE mismatch shape — an umbrella slug vs a sub-slug — is
resolved ONLY via an entry's own explicitly declared ``chain_aliases:``
frontmatter list, never via prefix/substring inference on slug shape. The
negative case (a same-shaped but undeclared slug does NOT widen) is the
important assertion: it is what stops a substring-matching regression from
creeping back in.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.completion_ops import _collect_chain_session_ids


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_date_prefixed_chain_slug_widens_via_handoff_workstream(tmp_path):
    root = tmp_path / "repo"

    # A handoff whose workstream: carries no date prefix — the shape that
    # previously matched nothing against a date-prefixed chain_slug.
    _write(
        root / "state" / "handoffs" / "sibling.md",
        "---\nworkstream: my-chain\nclaimed_by: sibling-session\n---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(
        root, "2026-07-26-my-chain", "seed-session"
    )

    assert "sibling-session" in chain_sids
    assert warnings == []


def test_date_prefixed_chain_slug_widens_via_completed_entry(tmp_path):
    root = tmp_path / "repo"

    # A completed entry whose chain: is ALSO date-prefixed (same date) — the
    # pre-existing exact-match shape must keep working unchanged.
    _write(
        root / "archive" / "completed" / "2026-07" / "sibling.md",
        "---\nchain: 2026-07-26-my-chain\nauthored_by: sibling-session\n---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(
        root, "2026-07-26-my-chain", "seed-session"
    )

    assert "sibling-session" in chain_sids
    assert warnings == []


def test_exact_match_behaviour_unchanged_when_no_prefix_or_aliases(tmp_path):
    # Invariant: the pre-fix exact-match path (no date prefix on either side,
    # no chain_aliases involved at all) must still widen exactly as before.
    root = tmp_path / "repo"

    _write(
        root / "archive" / "completed" / "2026-07" / "sibling.md",
        "---\nchain: my-chain\nauthored_by: sibling-session\n---\nbody\n",
    )
    _write(
        root / "archive" / "completed" / "2026-07" / "unrelated.md",
        "---\nchain: other-chain\nauthored_by: unrelated-session\n---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(root, "my-chain", "seed-session")

    assert chain_sids == ["seed-session", "sibling-session"]
    assert warnings == []


def test_umbrella_alias_widens_only_when_declared(tmp_path):
    root = tmp_path / "repo"

    # Sub-slug entry that explicitly declares membership in the umbrella chain.
    _write(
        root / "archive" / "completed" / "2026-07" / "declared.md",
        "---\n"
        "chain: umbrella-sub-alpha\n"
        "chain_aliases:\n"
        "  - umbrella\n"
        "authored_by: declared-session\n"
        "---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(root, "umbrella", "seed-session")

    assert "declared-session" in chain_sids
    assert warnings == []


def test_same_shaped_undeclared_slug_does_not_widen(tmp_path):
    # THE IMPORTANT NEGATIVE CASE: a slug that LOOKS like a sub-slug of the
    # umbrella (shares the "umbrella-sub-" shape a substring/prefix matcher
    # would catch) but carries no chain_aliases: declaration must NOT widen.
    # This is what stops the rejected substring-matching approach from
    # creeping back in.
    root = tmp_path / "repo"

    _write(
        root / "archive" / "completed" / "2026-07" / "undeclared.md",
        "---\nchain: umbrella-sub-beta\nauthored_by: undeclared-session\n---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(root, "umbrella", "seed-session")

    assert "undeclared-session" not in chain_sids
    assert chain_sids == ["seed-session"]
    assert warnings == []


def test_umbrella_alias_widens_via_handoff_workstream(tmp_path):
    root = tmp_path / "repo"

    _write(
        root / "state" / "handoffs" / "declared.md",
        "---\n"
        "workstream: sub-workstream\n"
        "chain_aliases: [umbrella]\n"
        "claimed_by: declared-session\n"
        "---\nbody\n",
    )
    _write(
        root / "state" / "handoffs" / "undeclared.md",
        "---\nworkstream: sub-workstream-2\nclaimed_by: undeclared-session\n---\nbody\n",
    )

    chain_sids, warnings = _collect_chain_session_ids(root, "umbrella", "seed-session")

    assert "declared-session" in chain_sids
    assert "undeclared-session" not in chain_sids
    assert warnings == []
