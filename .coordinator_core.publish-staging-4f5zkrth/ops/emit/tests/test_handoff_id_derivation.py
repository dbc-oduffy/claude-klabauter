"""Regression tests — wire-level ``handoff_id`` derivation (C4) in the ``handoffs``
emit section.

Every emitted ``HandoffSummary`` record now carries a non-null ``handoff_id`` plus a
``handoff_id_derivation`` discriminator (``"authored"`` | ``"derived"``). An authored
frontmatter ``handoff_id`` (shape ``hnd-<slug>-<6hex>``) passes through as-is; anything
else (absent, blank, malformed) is synthesized deterministically from ``(repo, basename)``
— see ``sections/handoffs.py``'s ``_derive_handoff_id``/``_resolve_handoff_id`` docstrings
for the full basename-not-provenance.path rationale.

Spec backlink: DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C4
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section


def _make_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-22T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-07-22",
        "status": "open",
        "deployment_state": "ready_to_fire",
    }
    fm.update(overrides)
    return fm


def _collect_with_records(mock_qr, tmp_path: Path, records: list[dict], repo_name: str = "test-org/test-repo"):
    ctx = _make_ctx(tmp_path, repo_name=repo_name)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return records
        return []

    mock_qr.side_effect = query_records
    return handoffs_section.collect(ctx)


# ---------------------------------------------------------------------------
# Unit-level: _resolve_handoff_id / _derive_handoff_id
# ---------------------------------------------------------------------------

def test_derive_is_deterministic_for_same_repo_and_basename() -> None:
    a = handoffs_section._derive_handoff_id("test-org/test-repo", "state/handoffs/x.md")
    b = handoffs_section._derive_handoff_id("test-org/test-repo", "archive/handoffs/2026-07/x.md")
    assert a == b, "basename-keyed derivation must survive a state/->archive/ move"


def test_derive_differs_across_repos_for_same_basename() -> None:
    a = handoffs_section._derive_handoff_id("test-org/repo-a", "state/handoffs/x.md")
    b = handoffs_section._derive_handoff_id("test-org/repo-b", "state/handoffs/x.md")
    assert a != b, "repo must qualify the key to avoid cross-repo basename collision"


def test_derive_slug_is_basename_derived_not_literal_derived() -> None:
    hid, derivation = handoffs_section._derive_handoff_id(
        "test-org/test-repo", "state/handoffs/2026-07-01_100000_sibling-notification-duty.md",
    )
    assert derivation == "derived"
    assert hid.startswith("hnd-sibling-notification-duty-")
    assert handoffs_section._HANDOFF_ID_RE.match(hid)


def test_derive_slug_strips_leading_timestamp_prefix() -> None:
    hid, _ = handoffs_section._derive_handoff_id(
        "test-org/test-repo", "state/handoffs/2026-07-19_140030_foo.md",
    )
    assert hid.startswith("hnd-foo-")


def test_derive_slug_falls_back_to_literal_derived_when_empty() -> None:
    hid, _ = handoffs_section._derive_handoff_id("test-org/test-repo", "state/handoffs/___.md")
    assert hid.startswith("hnd-derived-")
    assert handoffs_section._HANDOFF_ID_RE.match(hid)


def test_derive_hash_suffix_unchanged_by_slug_widening() -> None:
    # The 6-hex suffix is still sha1(f"{repo}:{basename}")[:6] — only the slug prefix widened.
    import hashlib

    basename = "2026-07-01_100000_sibling-notification-duty.md"
    expected_digest = hashlib.sha1(f"test-org/test-repo:{basename}".encode("utf-8")).hexdigest()[:6]
    hid, _ = handoffs_section._derive_handoff_id("test-org/test-repo", f"state/handoffs/{basename}")
    assert hid.endswith(f"-{expected_digest}")


def test_resolve_prefers_valid_authored_id() -> None:
    hid, derivation = handoffs_section._resolve_handoff_id(
        "test-org/test-repo", "state/handoffs/x.md",
        {"handoff_id": "hnd-my-slug-abc123"},
    )
    assert hid == "hnd-my-slug-abc123"
    assert derivation == "authored"


def test_resolve_falls_through_on_malformed_authored_id() -> None:
    hid, derivation = handoffs_section._resolve_handoff_id(
        "test-org/test-repo", "state/handoffs/x.md",
        {"handoff_id": "not-a-real-id"},
    )
    assert derivation == "derived"
    assert hid != "not-a-real-id"


def test_resolve_falls_through_on_absent_authored_id() -> None:
    hid, derivation = handoffs_section._resolve_handoff_id(
        "test-org/test-repo", "state/handoffs/x.md", {},
    )
    assert derivation == "derived"
    assert hid


# ---------------------------------------------------------------------------
# collect()-level: every emitted record carries both fields, never null.
# ---------------------------------------------------------------------------

@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_collect_emits_authored_id_when_present(mock_qr, tmp_path: Path) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{
            "path": "state/handoffs/x.md",
            "frontmatter": _base_handoff_fm(handoff_id="hnd-my-slug-abc123"),
        }],
    )
    assert malformed == []
    assert len(records) == 1
    assert records[0]["handoff_id"] == "hnd-my-slug-abc123"
    assert records[0]["handoff_id_derivation"] == "authored"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_collect_derives_id_when_absent(mock_qr, tmp_path: Path) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [{"path": "state/handoffs/x.md", "frontmatter": _base_handoff_fm()}],
    )
    assert malformed == []
    assert len(records) == 1
    expected_hid, expected_derivation = handoffs_section._derive_handoff_id(
        "test-org/test-repo", "state/handoffs/x.md",
    )
    assert records[0]["handoff_id"] == expected_hid
    assert records[0]["handoff_id_derivation"] == expected_derivation == "derived"


@patch("coordinator_core.ops.emit.sections.handoffs._query_records")
def test_collect_never_emits_null_handoff_id(mock_qr, tmp_path: Path) -> None:
    records, malformed = _collect_with_records(
        mock_qr, tmp_path,
        [
            {"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm()},
            {
                "path": "state/handoffs/b.md",
                "frontmatter": _base_handoff_fm(handoff_id="hnd-b-slug-def456"),
            },
        ],
    )
    assert malformed == []
    assert len(records) == 2
    by_path = {r["provenance"]["path"]: r for r in records}
    expected_a_hid, expected_a_derivation = handoffs_section._derive_handoff_id(
        "test-org/test-repo", "state/handoffs/a.md",
    )
    assert by_path["state/handoffs/a.md"]["handoff_id"] == expected_a_hid
    assert by_path["state/handoffs/a.md"]["handoff_id_derivation"] == expected_a_derivation == "derived"
    assert by_path["state/handoffs/b.md"]["handoff_id"] == "hnd-b-slug-def456"
    assert by_path["state/handoffs/b.md"]["handoff_id_derivation"] == "authored"
    assert by_path["state/handoffs/a.md"]["handoff_id"] != by_path["state/handoffs/b.md"]["handoff_id"]
