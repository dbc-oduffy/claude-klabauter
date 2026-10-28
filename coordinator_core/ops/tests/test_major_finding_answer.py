"""Pins the major-finding answer vocabulary: disclosure and PM waiver never clear."""

from coordinator_core.ops._major_finding_answer import CLEARING_KINDS, MAJOR_SCOPES, unanswered

SPINE = [
    {"id": "C1", "disposition": "open"},
    {"id": "C2", "disposition": "coded"},
    {"id": "C3", "disposition": "ruled_out"},
]


def _row(answered_by=None, **kw):
    row = {"id": "f1", "severity": "major", "scope": "default-path"}
    row.update(kw)
    if answered_by is not None:
        row["answered_by"] = answered_by
    return row


def test_constants():
    assert MAJOR_SCOPES == frozenset({"default-path", "user-visible"})
    assert CLEARING_KINDS == ("fix-row", "gem-scope-ruling")


def test_absent_and_disclosure_stay_unanswered(tmp_path):
    for scope in MAJOR_SCOPES:
        absent = _row(scope=scope)
        disclosure = _row({"kind": "disclosure"}, scope=scope)
        assert unanswered([absent, disclosure], SPINE, tmp_path) == [absent, disclosure]


def test_pm_waiver_and_malformed_stay_unanswered(tmp_path):
    rows = [_row({"kind": "pm-waiver"}), _row("fix-row"), _row({"kind": "fix-row"})]
    assert unanswered(rows, SPINE, tmp_path) == rows


def test_fix_row_clears_only_for_live_spine_row(tmp_path):
    assert unanswered([_row({"kind": "fix-row", "row": "C1"})], SPINE, tmp_path) == []
    assert unanswered([_row({"kind": "fix-row", "row": "C2"})], SPINE, tmp_path) == []
    for ref in ("C3", "C9"):
        row = _row({"kind": "fix-row", "row": ref})
        assert unanswered([row], SPINE, tmp_path) == [row]
    row = _row({"kind": "fix-row", "row": "C1"})
    assert unanswered([row], None, tmp_path) == [row]


def test_gem_ruling_clears_only_for_existing_repo_relative_file(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ruling.md").write_text("ruling")
    ok = _row({"kind": "gem-scope-ruling", "ref": "state/ruling.md"})
    assert unanswered([ok], SPINE, tmp_path) == []
    bad = [
        _row({"kind": "gem-scope-ruling", "ref": "state/missing.md"}),
        _row({"kind": "gem-scope-ruling", "ref": str(tmp_path / "state" / "ruling.md")}),
        _row({"kind": "gem-scope-ruling", "ref": "state/../state/ruling.md"}),
        _row({"kind": "gem-scope-ruling", "ref": "state"}),
    ]
    assert unanswered(bad, SPINE, tmp_path) == bad
    assert unanswered([ok], SPINE, None) == [ok]


def test_non_major_rows_untouched(tmp_path):
    rows = [
        {"id": "a"},
        _row(severity="minor"),
        _row(scope="internal"),
        {"id": "b", "severity": "major"},
        "not-a-dict",
    ]
    assert unanswered(rows, SPINE, tmp_path) == []
