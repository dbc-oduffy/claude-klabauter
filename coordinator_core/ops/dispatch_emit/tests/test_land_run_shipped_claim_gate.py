"""dispatch.land_run refuses a landing whose delivery note claims a shipped file the landing does not carry."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR
from coordinator_core.ops.dispatch_emit.shipped_claims import SHIPPED_CLAIM_REFUSED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LANDING = "pkg/shipped.py"
_STRAY = "pkg/tests/test_stray.py"


def _stage(repo, note: str | None, *, stray: bool = False) -> None:
    prod._author(repo, _LANDING, "X = 1\n")
    prod._ask_script(repo, _LANDING)
    if stray:
        prod._write(repo, _STRAY, "def test_x():\n    pass\n")
    if note is not None:
        prod._write(repo, f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md", note)


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


def _recorded(out: dict, code: str) -> list[dict]:
    return [e for e in out.get("provenance", []) if e["reason"] == code]


def test_an_untracked_shipped_file_lands_the_rest_and_is_dropped_foreign_not_refused(repo):  # noqa: F811
    _stage(repo, f"## What shipped\n\n- `{_LANDING}`\n- `{_STRAY}`\n", stray=True)
    token = run_token.mint(repo, prod.RUN, prod.SCRIPT, session_id=prod.SELF)
    head = prod._git(["rev-parse", "HEAD"], repo).strip()

    out = prod._land(repo, run_token=token)

    assert out["committed"] is True and out["refused"] is None, out
    assert out["shipped_claims"]["unbacked"] == [], out
    assert out["shipped_claims"]["dropped_foreign"] == [_STRAY], out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() != head
    assert _LANDING in prod._tree(repo) and _STRAY not in prod._tree(repo)


def test_the_landed_twin_lands_and_reports_checked(repo):  # noqa: F811
    _stage(repo, f"## What shipped\n\n- `{_LANDING}`\n")

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert out["shipped_claims"] == {"checked": 1, "unbacked": [], "unresolved": []}


def test_a_bare_name_lands_and_is_listed_unresolved(repo):  # noqa: F811
    _stage(repo, "## What shipped\n\n- `no_such_module.py`\n")

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert len(out["shipped_claims"]["unresolved"]) == 1 and not out["shipped_claims"]["unbacked"]


@pytest.mark.parametrize("note", [None, "no shipped heading `pkg/tests/test_stray.py`\n"], ids=["no-note", "no-heading"])
def test_no_note_and_no_shipped_heading_land_unchanged(repo, note):  # noqa: F811
    _stage(repo, note, stray=True)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert "shipped_claims" not in out


def test_a_halt_record_only_withhold_on_a_met_runs_product_lands_it(repo):  # noqa: F811
    _stage(repo, f"## What shipped\n\n- `{_LANDING}`\n")

    out = prod._land(repo, withhold_paths=[{"path": _LANDING, "reason": "halt-record-only"}])

    assert out["committed"] is True, out
    assert out["shipped_claims"]["unbacked"] == []
    assert _LANDING in prod._tree(repo)


def test_a_content_withhold_on_a_shipped_path_records_the_unbacked_claim_as_provenance(repo):  # noqa: F811
    _stage(repo, f"## What shipped\n\n- `{_LANDING}`\n")

    out = prod._land(repo, withhold_paths=[{"path": _LANDING, "reason": "peer owns this"}])

    assert out["refused"] is None, out
    assert _LANDING not in prod._tree(repo)
    entries = _recorded(out, SHIPPED_CLAIM_REFUSED)
    assert len(entries) == 1 and "peer owns this" in entries[0]["detail"], out
