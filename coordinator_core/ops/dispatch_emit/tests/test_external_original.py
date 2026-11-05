from coordinator_core.ops.dispatch_emit import external_original as eo

ART = "https://claude.ai/artifact/abc-123"


def test_cited_originals_finds_artifact_and_board():
    text = f"See {ART}.\nBoard: https://example.com/board/9\nand claude.ai/artifact/abc-123 again"
    assert eo.cited_originals(text) == [ART, "https://example.com/board/9"]


def test_cited_originals_none():
    assert eo.cited_originals("plain row, a file a/b.py") == []


def test_unread_original_is_blocked_with_age_unknowable():
    out = eo.read_refusal("R1", f"matches {ART}", {"status": "met"})
    assert out["status"] == "BLOCKED"
    assert out["reason"].startswith("BLOCKED: unreachable-original " + ART)
    assert "capture age unknowable" in out["reason"]
    assert out["capture_age_s"] is None


def test_read_original_passes_across_spellings():
    for read in (ART, "claude.ai/artifact/abc-123/", ["http://claude.ai/artifact/ABC-123"]):
        assert eo.read_refusal("R1", ART, {"originals_read": read}) is None


def test_other_original_read_still_refuses():
    out = eo.read_refusal("R1", ART, {"originals_read": "claude.ai/artifact/zzz"})
    assert out is not None


def test_no_citation_never_refuses():
    assert eo.read_refusal("R1", "no url here", {}) is None


def test_capture_age_computed_when_both_stamps():
    item = {"capture_harvested_at": "2026-11-05T10:00:00Z", "original_last_edited_at": "2026-11-05T10:01:30Z"}
    assert eo.capture_age_s(item) == 90
    out = eo.read_refusal("R1", ART, item)
    assert out["capture_age_s"] == 90 and "capture age 90s" in out["reason"]


def test_capture_age_none_when_missing_or_unparseable():
    assert eo.capture_age_s({"capture_harvested_at": "2026-11-05T10:00:00Z"}) is None
    assert eo.capture_age_s({"capture_harvested_at": "x", "original_last_edited_at": "y"}) is None
