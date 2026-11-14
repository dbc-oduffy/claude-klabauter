"""A halt commit flags a run whose sizing_rel is absent on disk and still lands the record; a present
or archived sizing raises no flag."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _landed,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/2026-11/2026-11-14-lost-aaaaaa.yaml"


def _record_naming_sizing(repo) -> None:
    rec = _write_record(repo, RUN)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")


def _put(repo, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ask: x\n", encoding="utf-8")


def test_a_missing_sizing_is_flagged_not_declared_and_the_halt_still_lands(repo):
    _record_naming_sizing(repo)

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert out["committed"] is True
    assert out["sizing_missing"] == SIZING
    assert SIZING not in _landed(repo, out, "--name-only")
    assert any(p.endswith("/run.json") for p in _landed(repo, out, "--name-only"))
    message = _git(["log", "-1", "--format=%B", out["sha"]], repo)
    assert f"run sizing {SIZING} is absent on disk" in message


def test_a_present_sizing_is_declared_and_raises_no_flag(repo):
    _record_naming_sizing(repo)
    _put(repo, SIZING)

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert "sizing_missing" not in out
    assert SIZING in _landed(repo, out, "--name-only")


def test_an_archive_only_sizing_is_not_flagged_and_not_declared_at_sizing_rel(repo):
    _record_naming_sizing(repo)
    _put(repo, "archive/sizings/2026-11/2026-11-14-lost-aaaaaa.yaml")

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert "sizing_missing" not in out
    assert SIZING not in _landed(repo, out, "--name-only")


def test_two_archive_copies_of_the_sizing_read_as_missing(repo):
    _record_naming_sizing(repo)
    _put(repo, "archive/sizings/2026-11/2026-11-14-lost-aaaaaa.yaml")
    _put(repo, "archive/sizings/2026-10/2026-11-14-lost-aaaaaa.yaml")

    out = _call(repo)

    assert out.get("sizing_missing") == SIZING, out
