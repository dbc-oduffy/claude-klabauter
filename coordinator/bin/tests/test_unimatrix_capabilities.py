"""unimatrix_capabilities: the relay verb set, the skew line, and the digest ack."""
import hashlib
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "unimatrix_capabilities_c0", Path(__file__).resolve().parents[1] / "lib" / "unimatrix_capabilities.py")
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)


def _d(line):
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


def test_capabilities_name_the_verbs_the_current_relay_serves():
    assert cap.CAPABILITIES == frozenset({"send-session", "receipts", "capabilities"})
    assert isinstance(cap.CAPABILITIES, frozenset)


def test_skew_reason_is_empty_when_every_needed_verb_is_served():
    assert cap.skew_reason({"send-session", "capabilities"}, "send-session") == ""
    assert cap.skew_reason(cap.CAPABILITIES, {"receipts", "send-session"}) == ""


def test_skew_reason_names_the_missing_verb():
    assert cap.skew_reason({"send-session"}, "capabilities") == "far relay lacks capabilities; upgrade it (version skew)"
    assert cap.skew_reason(set(), ["send-session"]) == "far relay lacks send-session; upgrade it (version skew)"
    assert cap.skew_reason(None, "receipts").startswith("far relay lacks receipts")


def test_a_matching_digest_acks_its_line():
    lines = [b"hello\n", b"world\n"]
    rows = [{"ok": True, "digest": _d(b"hello")}, {"ok": True, "digest": _d(b"world")}]
    assert cap.ingest_acked(lines, rows) == [(True, ""), (True, "")]


def test_digest_ignores_the_trailing_newline_and_reply_order():
    rows = [{"ok": True, "digest": _d(b"b")}, {"ok": True, "digest": _d(b"a")}]
    assert cap.ingest_acked([b"a", b"b\n"], rows) == [(True, ""), (True, "")]


def test_an_ok_reply_with_no_digest_is_unacked():
    assert cap.ingest_acked([b"x\n"], [{"ok": True}]) == [(False, "unconfirmed: no far receipt for this line")]


def test_a_digest_that_does_not_match_is_unacked():
    assert cap.ingest_acked([b"x\n"], [{"ok": True, "digest": _d(b"other")}]) == [(False, "unconfirmed: no far receipt for this line")]


def test_no_reply_at_all_leaves_every_line_unacked():
    assert cap.ingest_acked([b"x\n", b"y\n"], []) == [(False, cap.UNACKED)] * 2


def test_a_refused_reply_never_acks_and_reports_its_error():
    rows = [{"ok": False, "error": "OSError: disk full", "digest": _d(b"x")}]
    assert cap.ingest_acked([b"x\n"], rows) == [(False, "OSError: disk full")]


def test_a_receipts_row_without_an_ok_key_acks():
    assert cap.ingest_acked([b"x\n"], [{"session": "s", "digest": _d(b"x")}]) == [(True, "")]


def test_one_row_acks_one_line_so_a_repeated_line_needs_two_rows():
    rows = [{"ok": True, "digest": _d(b"x")}]
    assert cap.ingest_acked([b"x\n", b"x\n"], rows) == [(True, ""), (False, cap.UNACKED)]


def test_malformed_rows_are_skipped():
    assert cap.ingest_acked([b"x\n"], ["junk", None, {"ok": True, "digest": 5}]) == [(False, cap.UNACKED)]
