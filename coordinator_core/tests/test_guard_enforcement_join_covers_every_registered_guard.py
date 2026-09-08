"""coordinator_core/tests/test_guard_enforcement_join_covers_every_registered_guard.py

The join this repo emits for `DoE-claude` is the artifact
`DR-an-omission-is-ratified-by-the-plane-that-enforces-the-rule` named as the
one thing standing between an accepted decision and a story that can omit a
rule. Its whole load-bearing claim is `complete_over_guards: true`.

WHY COMPLETENESS AND NOT ACCURACY IS WHAT THIS FILE PINS
    A positive -- "guard G enforces rule X" -- rests on one row and is
    checkable by reading that row. A NEGATIVE -- "no guard enforces rule X"
    -- rests on having asked every guard, and is what the consumer turns
    into an omission. So a guard missing from the join does not degrade the
    answer, it inverts it: every rule that guard enforces silently reads as
    unenforced and becomes omittable. That is the doctrine-that-lies-to-its-
    reader failure the DR exists to prevent, arriving through an omission in
    an audit rather than through a wrong verdict.

    Mirrors `test_party_screen_audit_covers_every_registered_guard.py`,
    which makes the same coverage claim over the same population for the
    audit this join's verdicts are copied from.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
JOIN_PATH = (
    REPO_ROOT
    / "state"
    / "audits"
    / "2026-09-07-guard-enforcement-join"
    / "guard-enforcement-join.yaml"
)
EMITTER = REPO_ROOT / "coordinator" / "bin" / "emit-guard-enforcement-join.py"

from coordinator_core.bash_guards.roster import guard_roster  # noqa: E402
from coordinator_core.write_guards.engine import discover_guard_names  # noqa: E402


def _join() -> dict:
    return yaml.safe_load(JOIN_PATH.read_text(encoding="utf-8"))


def _live_guard_ids() -> "set[str]":
    write_ids, errors = discover_guard_names()
    assert not errors, f"discover_guard_names() reported errors: {errors!r}"
    return {guard.id for guard in guard_roster()} | set(write_ids)


def test_join_covers_the_live_registered_population_in_both_directions():
    join_ids = {row["guard_id"] for row in _join()["guards"]}
    live = _live_guard_ids()
    assert not (live - join_ids), (
        "registered guard(s) absent from the join -- every rule they enforce would "
        f"read as unenforced and become omittable: {sorted(live - join_ids)}"
    )
    assert not (join_ids - live), (
        "join names guard(s) that are not registered -- a verdict about something "
        f"that does not fire: {sorted(join_ids - live)}"
    )


def test_completeness_flag_is_set_and_is_what_licenses_a_negative():
    document = _join()
    assert document["complete_over_guards"] is True
    assert document["source_repo"] == "claude-klabauter"
    assert document["source_sha"], "a delivered copy must say what it is a copy of"


def test_every_row_distinguishes_examined_from_unexamined():
    """Both rule lists are required on every row, empty where there is
    nothing. An absent key and an empty list would otherwise read alike in
    the file that licenses every negative -- and one of them means nobody
    looked at that guard."""
    for row in _join()["guards"]:
        for field in ("enforces_rule_ids", "uncertain_rule_ids"):
            assert isinstance(row.get(field), list), f"{row['guard_id']}: {field}"
        assert row.get("cloud_verdict"), row["guard_id"]
        assert row.get("basis"), f"{row['guard_id']}: no basis for its claim"


def test_no_guard_is_named_twice():
    ids = [row["guard_id"] for row in _join()["guards"]]
    assert len(ids) == len(set(ids)), "a guard appears in more than one shard"


def test_committed_join_is_not_stale():
    result = subprocess.run(
        [sys.executable, str(EMITTER), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
