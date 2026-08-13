"""Tests for coordinator_core.ops.check_competitor_positioning_nudge.

Port of: check-competitor-positioning-nudge.sh (coordinator-claude b5a4192c, 2026-07-20)
Golden-oracle parity cases captured against the bash original before porting;
each test below mirrors one oracle case 1:1.
"""

from __future__ import annotations

import datetime

import pytest

from coordinator_core.ops.check_competitor_positioning_nudge import (
    _NUDGE_TEXT,
    check,
    main,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_absent_yaml_nudges(tmp_path):
    assert check(str(tmp_path)) == _NUDGE_TEXT


def test_empty_competitors_nudges(tmp_path):
    _write(tmp_path / "state/strategic/self-description.yaml", "competitors: []\n")
    assert check(str(tmp_path)) == _NUDGE_TEXT


def test_nonempty_competitors_no_nudge(tmp_path):
    _write(
        tmp_path / "state/strategic/self-description.yaml",
        "competitors:\n  - name: Foo\n",
    )
    assert check(str(tmp_path)) == ""


def test_record_decline_writes_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["--record-decline"])
    assert rc == 0
    marker = tmp_path / "state/strategic/.positioning-nudge-declined-at"
    assert marker.is_file()
    ts = marker.read_text(encoding="utf-8").strip()
    # Parses cleanly as the expected UTC ISO-8601 format.
    datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def test_within_cooldown_suppresses_nudge(tmp_path):
    _write(tmp_path / "state/strategic/self-description.yaml", "competitors: []\n")
    now = datetime.datetime.now(datetime.timezone.utc)
    _write(
        tmp_path / "state/strategic/.positioning-nudge-declined-at",
        now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
    )
    assert check(str(tmp_path)) == ""


def test_malformed_decline_marker_fails_safe_to_nudge(tmp_path):
    _write(tmp_path / "state/strategic/self-description.yaml", "competitors: []\n")
    _write(tmp_path / "state/strategic/.positioning-nudge-declined-at", "not-a-date\n")
    assert check(str(tmp_path)) == _NUDGE_TEXT


def test_expired_cooldown_resumes_nudge(tmp_path):
    _write(tmp_path / "state/strategic/self-description.yaml", "competitors: []\n")
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    _write(
        tmp_path / "state/strategic/.positioning-nudge-declined-at",
        old.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
    )
    assert check(str(tmp_path)) == _NUDGE_TEXT


def test_malformed_yaml_propagates_uncaught(tmp_path):
    """Negative-spec parity: an unparseable YAML file raises, matching the
    bash oracle's `set -euo pipefail` non-zero-exit-with-traceback failure
    mode (deliberately NOT "fixed" mid-port)."""
    _write(tmp_path / "state/strategic/self-description.yaml", "competitors: [\n")
    with pytest.raises(Exception):
        check(str(tmp_path))


def test_main_no_args_prints_nudge_on_absent_yaml(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert _NUDGE_TEXT in captured.out


def test_main_no_args_silent_when_no_trigger(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "state/strategic/self-description.yaml",
        "competitors:\n  - name: Foo\n",
    )
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
