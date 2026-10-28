"""Tests for coordinator_core.advisory_census and its pinned fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core import advisory_census as census

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS = FIXTURES / "pretooluse_advisory_corpus.jsonl"
BASELINE = FIXTURES / "pretooluse_advisory_baseline.json"
AUDIT = FIXTURES / "pretooluse_advisory_audit.md"
RETIRED_2026_08_21 = ("probe-spray", "branch-set-precedence", "longlived-branch-naming")


def _corpus_lines() -> list[dict]:
    return [json.loads(ln) for ln in CORPUS.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _audit_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0] not in ("site", "---") and not cells[0].startswith("-"):
            rows[cells[0]] = cells[5]
    return rows


@pytest.fixture(scope="module")
def live_replay() -> dict:
    return census.replay(CORPUS)


def test_corpus_spans_all_six_dispatchers():
    modules = {census.DISPATCH_MODULES[line["tool_name"]] for line in _corpus_lines()}
    assert modules == set(census.DISPATCH_MODULES.values())
    assert len(_corpus_lines()) >= 150


def test_corpus_carries_no_machine_path():
    text = CORPUS.read_text(encoding="utf-8")
    assert str(census.REPO_ROOT) not in text
    assert not re.search(r"/Users/|/home/|[A-Za-z]:\\\\", text)


def test_baseline_is_self_consistent():
    pin = _baseline()
    sites = pin["sites"]
    assert pin["advisory_sites"] == len(sites)
    assert pin["fires"] == sum(s["fires"] for s in sites.values())
    assert pin["total_bytes"] == sum(s["bytes"] for s in sites.values())
    assert 0 < pin["median_bytes"] <= max(s["bytes"] for s in sites.values())


def test_live_replay_never_exceeds_the_baseline(live_replay):
    pin = _baseline()
    assert live_replay["fires"] <= pin["fires"]
    assert live_replay["total_bytes"] <= pin["total_bytes"]
    assert live_replay["advisory_sites"] <= pin["advisory_sites"]
    for site, got in live_replay["sites"].items():
        assert site in pin["sites"], f"new advisory site {site}"
        assert got["bytes"] <= pin["sites"][site]["bytes"], site
        assert got["fires"] <= pin["sites"][site]["fires"], site


def test_replay_is_deterministic(live_replay):
    assert census.replay(CORPUS) == live_replay


def test_replay_of_a_repeated_payload_dedupes_on_the_bash_path(tmp_path):
    corpus = tmp_path / "c.jsonl"
    line = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    corpus.write_text(line + "\n" + line + "\n", encoding="utf-8")
    report = census.replay(corpus)
    assert report["fires"] == 1
    assert report["sites"] == {"bash:git-no-optional-locks": {"bytes": 129, "fires": 1}}


def test_replay_of_a_silent_payload_reports_zero(tmp_path):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({"tool_name": "Bash", "tool_input": {"command": "pwd"}}) + "\n", encoding="utf-8")
    assert census.replay(corpus) == {
        "advisory_sites": 0,
        "fires": 0,
        "total_bytes": 0,
        "median_bytes": 0,
        "sites": {},
    }


def test_emitted_drops_an_outer_rewrap_and_keeps_the_leg():
    calls = [("leg", "[coordinator] x"), ("outer", "[coordinator] x")]
    assert census._emitted(calls, [], "[coordinator] x") == [("leg", len("[coordinator] x"))]


def test_emitted_ignores_a_record_that_did_not_reach_the_envelope():
    calls = [("lost", "[coordinator] a"), ("won", "[coordinator] b")]
    assert census._emitted(calls, [], "[coordinator] b") == [("won", len("[coordinator] b"))]


def test_emitted_credits_a_guard_chain_fire_with_the_whole_text():
    assert census._emitted([], ["bash:g"], "hello") == [("bash:g", 5)]
    assert census._emitted([], ["bash:g"], "  ") == []


def test_path_scrub_makes_bytes_independent_of_the_sandbox_path(tmp_path):
    short = tmp_path / "a"
    long = tmp_path / "a-much-longer-directory-name"
    for repo in (short, long):
        repo.mkdir()
    texts = [census._path_scrubber(r)(f"edit {r}/CLAUDE.md now") for r in (short, long)]
    assert texts[0] == texts[1] == "edit <repo>/CLAUDE.md now"


def test_static_view_names_known_sites_and_chain_guards():
    assert "hooks.preuse_sendmessage_dispatch:_handler" in census.static_sites()
    guards = census.chain_guards()
    assert "bash:git-no-optional-locks" in guards
    assert any(k.startswith("write:") for k in guards)


def test_retired_guards_stay_retired():
    names = {k.split(":", 1)[1] for k in census.chain_guards()}
    assert not names & set(RETIRED_2026_08_21)
    assert not any(site.split(":", 1)[-1] in RETIRED_2026_08_21 for site in _baseline()["sites"])


def test_audit_has_one_row_per_baseline_site_with_a_closed_verdict():
    rows = _audit_rows()
    assert set(rows) == set(_baseline()["sites"])
    for site, verdict in rows.items():
        assert verdict.split()[0] in {"delete", "gate", "keep"}, site


def test_audit_never_deletes_the_descoped_sed_guard():
    assert _audit_rows()["bash:sed-range-read-advise"].startswith("gate")
