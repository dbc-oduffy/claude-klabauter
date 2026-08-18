"""Tests for coordinator_core.ceremony_common.json_payload_flag — the shared
`resolve_json_payload_flag` / `detect_conflicting_payload_channels` pair all
eleven `--decisions` parse sites route through.

Spec backlink:
docs/plans/2026-08-18-quote-safe-payloads-through-the-cmd-forw.md, chunk C1
"""

from __future__ import annotations

import codecs
import json

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)

_HOSTILE_PAYLOAD = '{"a": "has a \\"quote\\" and a space"}'


def test_inline_happy_path():
    tokens = ["apply", "--decisions", '{"jp-1": {"disposition": "accept"}}']
    result = resolve_json_payload_flag(tokens, 1)
    assert result.consumed == 2
    assert result.error is None
    assert result.value == {"jp-1": {"disposition": "accept"}}


def test_file_happy_path(tmp_path):
    payload_path = tmp_path / "decisions.json"
    payload_path.write_text('{"jp-1": {"disposition": "accept"}}', encoding="utf-8")
    tokens = ["apply", "--decisions-file", str(payload_path)]
    result = resolve_json_payload_flag(tokens, 1)
    assert result.consumed == 2
    assert result.error is None
    assert result.value == {"jp-1": {"disposition": "accept"}}


def test_inline_missing_value():
    result = resolve_json_payload_flag(["apply", "--decisions"], 1)
    assert result.consumed == 1
    assert result.value is None
    assert result.error == "--decisions requires a value"


def test_file_missing_value():
    result = resolve_json_payload_flag(["apply", "--decisions-file"], 1)
    assert result.consumed == 1
    assert result.value is None
    assert result.error == "--decisions-file requires a value"


def test_file_unreadable_path(tmp_path):
    missing_path = tmp_path / "does-not-exist.json"
    tokens = ["apply", "--decisions-file", str(missing_path)]
    result = resolve_json_payload_flag(tokens, 1)
    assert result.consumed == 2
    assert result.value is None
    assert result.error is not None
    assert result.error.startswith(f"--decisions-file unreadable: {missing_path}: ")


def test_inline_malformed_json():
    tokens = ["apply", "--decisions", "{not valid json"]
    result = resolve_json_payload_flag(tokens, 1)
    assert result.consumed == 2
    assert result.value is None
    assert result.error is not None
    assert result.error.startswith("malformed --decisions JSON: ")


def test_file_malformed_json_shares_inline_prefix(tmp_path):
    payload_path = tmp_path / "decisions.json"
    payload_path.write_text("{not valid json", encoding="utf-8")
    tokens = ["apply", "--decisions-file", str(payload_path)]
    result = resolve_json_payload_flag(tokens, 1)
    assert result.consumed == 2
    assert result.value is None
    assert result.error is not None
    assert result.error.startswith("malformed --decisions JSON")
    assert str(payload_path) in result.error


def test_non_matching_token_is_not_consumed():
    result = resolve_json_payload_flag(["apply", "--session-id", "s-1"], 1)
    assert result == (0, None, None)


def test_detect_conflicting_payload_channels_neither_present():
    assert detect_conflicting_payload_channels(["apply", "--session-id", "s-1"]) is None


def test_detect_conflicting_payload_channels_only_inline():
    assert detect_conflicting_payload_channels(["apply", "--decisions", "{}"]) is None


def test_detect_conflicting_payload_channels_only_file():
    assert detect_conflicting_payload_channels(["apply", "--decisions-file", "p.json"]) is None


def test_detect_conflicting_payload_channels_both_present():
    tokens = ["apply", "--decisions", "{}", "--decisions-file", "p.json"]
    assert (
        detect_conflicting_payload_channels(tokens)
        == "--decisions and --decisions-file are mutually exclusive"
    )


def test_hostile_payload_round_trips_byte_identically_through_file_form(tmp_path):
    payload_path = tmp_path / "decisions.json"
    payload_path.write_text(_HOSTILE_PAYLOAD, encoding="utf-8")
    tokens = ["apply", "--decisions-file", str(payload_path)]
    result = resolve_json_payload_flag(tokens, 1)
    assert result.error is None
    assert result.value == json.loads(_HOSTILE_PAYLOAD)
    assert result.value is not None
    assert result.value["a"] == 'has a "quote" and a space'


def test_file_channel_accepts_a_bom_prefixed_payload(tmp_path):
    """Windows PowerShell 5.1's `Set-Content -Encoding utf8` emits a BOM, so
    the most obvious way to author a payload file on the platform this
    channel exists for produces one. Reading as plain utf-8 rejected it as
    `malformed --decisions JSON`, blaming the operator's payload -- exactly
    the misdirection the file channel exists to end."""
    payload = '{"j-kind": {"disposition": "ack-nil"}}'
    payload_path = tmp_path / "bom.json"
    payload_path.write_text(payload, encoding="utf-8-sig")
    assert payload_path.read_bytes().startswith(codecs.BOM_UTF8)

    result = resolve_json_payload_flag(["--decisions-file", str(payload_path)], 0)

    assert result.error is None
    assert result.consumed == 2
    assert result.value == {"j-kind": {"disposition": "ack-nil"}}
