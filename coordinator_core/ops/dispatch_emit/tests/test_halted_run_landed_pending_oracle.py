"""dispatch.record_halted_run_landed ``pending_oracle``: a relayed row owned by an external oracle stays open.

An entry (owner, closing_check, requester_citation) never counts as met, writes one PM flag per (run, row),
leaves the run halted and refirable, and only a passing closing check credits the row (``met_by_oracle``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import pending_oracle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
SENTINEL = "oracle.ok"
_PROBE = f"import os; print('GREEN' if os.path.exists({SENTINEL!r}) else 'RED')"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _entry(**over) -> dict:
    """A fully owned and cited entry whose check passes only once ``oracle.ok`` exists in the repo root."""
    base = {
        "owner": "oracle-team",
        "closing_check": {"command": [sys.executable, "-c", _PROBE], "expect": "GREEN"},
        "requester_citation": "PM 2026-11-23: 2) yes, but that needs to be flagged to me",
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture
def halted(tmp_path):
    """A repo whose committed halt marker blocks on R1 and R2 and names a plan; two commits follow the halt."""
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({
            "status": "halted", "halted": "ledger-check", "sizing_rel": None,
            "blocking": ["R1", "R2"], "plan": "docs/plans/p.md",
        }),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "--", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    after1 = _commit(tmp_path, "after1")
    after2 = _commit(tmp_path, "after2")
    return tmp_path, marker, after1, after2


def _call(root, **params):
    return op._handler({"run_id": SOLO, **params}, root)


def _record(marker: Path) -> dict:
    return json.loads(marker.read_text(encoding="utf-8"))


def _flags(root: Path) -> list:
    flag_dir = pending_oracle.flag_path(root, SOLO, "R1").parent
    return sorted(flag_dir.glob("*.json")) if flag_dir.exists() else []


@pytest.mark.parametrize("field", pending_oracle.REQUIRED_FIELDS)
@pytest.mark.parametrize("blank", ["absent", "empty"])
def test_an_entry_missing_a_required_field_refuses_naming_the_row_and_field(halted, field, blank):
    root, marker, _, after2 = halted
    entry = _entry()
    if blank == "absent":
        del entry[field]
    else:
        entry[field] = {} if field == "closing_check" else ""
    out = _call(root, sha=after2, pending_oracle={"R1": entry})
    assert not out["ok"] and "R1" in out["error"] and field in out["error"], out
    record = _record(marker)
    assert record["status"] == "halted" and "pending_oracle" not in record
    assert _flags(root) == []


def test_a_string_closing_check_command_refuses(halted):
    root, marker, _, after2 = halted
    entry = _entry(closing_check={"command": f"{sys.executable} -c pass", "expect": "GREEN"})
    out = _call(root, sha=after2, pending_oracle={"R1": entry})
    assert not out["ok"] and "R1" in out["error"] and "argv list" in out["error"], out
    assert _record(marker)["status"] == "halted" and _flags(root) == []


def test_a_pending_row_that_is_not_blocking_refuses_without_a_flag(halted):
    root, marker, _, after2 = halted
    out = _call(root, sha=after2, pending_oracle={"R9": _entry()})
    assert not out["ok"] and "R9" in out["error"] and "not a blocking row" in out["error"], out
    assert _record(marker)["status"] == "halted" and _flags(root) == []


@pytest.mark.parametrize(
    "pending",
    [
        {"R1": {**_entry(), "accepted_partial": True}},
        {"R1": {**_entry(), "state": "accepted_partial"}},
        {"accepted_partial": _entry()},
    ],
    ids=["as-a-key", "as-a-row-state-value", "as-a-row-id"],
)
def test_accepted_partial_is_refused_wherever_it_appears(halted, pending):
    root, marker, _, after2 = halted
    out = _call(root, sha=after2, pending_oracle=pending)
    assert not out["ok"] and "accepted_partial is not a state" in out["error"], out
    record = _record(marker)
    assert record["status"] == "halted" and "pending_oracle" not in record
    assert _flags(root) == []


def test_a_valid_entry_stays_open_unmet_halted_and_refirable(halted, launches):
    root, marker, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R2": after1}, pending_oracle={"R1": _entry()})
    assert not out["ok"], out
    assert out["pending_oracle_rows"] == ["R1"]
    assert "pending external oracle: R1 owned by oracle-team" in out["error"]
    assert out["refire"] == {"resume_run_id": SOLO} and f"resume_run_id: {SOLO}" in out["error"]
    record = _record(marker)
    assert record["status"] == "halted"
    assert record["pending_oracle"]["R1"] == _entry()
    assert "R1" not in (record.get("met_by") or {}) and "R1" not in (record.get("met_by_oracle") or {})
    assert launches == [] and "close" not in out


def test_an_entry_writes_one_pm_flag_carrying_the_row_owner_check_citation_and_refire_ask(halted):
    root, _, _, after2 = halted
    _call(root, sha=after2, pending_oracle={"R1": _entry()})
    flags = _flags(root)
    assert [p.name for p in flags] == [f"{SOLO}.R1.json"]
    flag = json.loads(flags[0].read_text(encoding="utf-8"))
    entry = _entry()
    assert flag["run_id"] == SOLO and flag["row"] == "R1"
    assert flag["owner"] == entry["owner"] and flag["closing_check"] == entry["closing_check"]
    assert flag["requester_citation"] == entry["requester_citation"]
    assert flag["refire"] == {"resume_run_id": SOLO} and flag["needs_pm"] is True


def test_a_repeat_call_for_the_same_row_writes_no_duplicate_flag(halted):
    root, _, _, after2 = halted
    _call(root, sha=after2, pending_oracle={"R1": _entry()})
    (path,) = _flags(root)
    first = path.read_bytes()
    out = _call(root, sha=after2, pending_oracle={"R1": _entry()})
    assert not out["ok"] and out["pending_oracle_rows"] == ["R1"]
    assert _flags(root) == [path] and path.read_bytes() == first


def test_each_pending_row_gets_its_own_flag(halted):
    root, _, _, after2 = halted
    _call(root, sha=after2, pending_oracle={"R1": _entry(), "R2": _entry(owner="other-oracle")})
    assert [p.name for p in _flags(root)] == [f"{SOLO}.R1.json", f"{SOLO}.R2.json"]


def test_a_failing_check_on_a_later_call_keeps_the_row_pending_without_the_param(halted):
    root, marker, _, after2 = halted
    _call(root, sha=after2, pending_oracle={"R1": _entry()})
    out = _call(root, sha=after2)
    assert not out["ok"] and out["pending_oracle_rows"] == ["R1"], out
    assert _record(marker)["status"] == "halted"


def test_a_passing_closing_check_credits_the_row_and_closes_the_run(halted, launches):
    root, marker, after1, after2 = halted
    first = _call(root, sha=after2, met_by={"R2": after1}, pending_oracle={"R1": _entry()})
    assert not first["ok"] and launches == []
    (root / SENTINEL).write_text("closed by the oracle", encoding="utf-8")
    out = _call(root, sha=after2, met_by={"R2": after1})
    assert out["ok"], out
    assert set(out["met_by_oracle"]) == {"R1"} and out["met_by_oracle"]["R1"]["owner"] == "oracle-team"
    record = _record(marker)
    assert record["status"] == "landed"
    assert set(record["met_by_oracle"]) == {"R1"} and "R1" not in (record.get("met_by") or {})
    assert launches == [SOLO]


def test_a_check_that_exits_nonzero_never_credits_even_with_matching_output(halted):
    root, marker, _, after2 = halted
    probe = f"import sys; print('GREEN'); sys.exit(3)"
    entry = _entry(closing_check={"command": [sys.executable, "-c", probe], "expect": "GREEN"})
    out = _call(root, sha=after2, pending_oracle={"R1": entry})
    assert not out["ok"] and out["pending_oracle_rows"] == ["R1"]
    assert _record(marker)["status"] == "halted"


def test_a_check_that_cannot_spawn_leaves_the_row_pending(halted):
    root, marker, _, after2 = halted
    entry = _entry(closing_check={"command": [str(root / "no-such-binary")], "expect": "GREEN"})
    out = _call(root, sha=after2, pending_oracle={"R1": entry})
    assert not out["ok"] and out["pending_oracle_rows"] == ["R1"]
    assert _record(marker)["status"] == "halted"


def test_a_check_past_its_timeout_leaves_the_row_pending_and_a_spent_deadline_spawns_nothing(tmp_path, monkeypatch):
    check = {"command": [sys.executable, "-c", "import time; time.sleep(5); print('GREEN')"], "expect": "GREEN"}
    assert pending_oracle.closing_check_passes(tmp_path, check, timeout=0.05) is False
    seen = _counting_run(monkeypatch)
    ok = {"command": [sys.executable, "-c", "print('GREEN')"], "expect": "GREEN"}
    assert pending_oracle.closing_check_passes(tmp_path, ok, timeout=0) is False
    assert seen == []
    assert pending_oracle.closing_check_passes(tmp_path, ok) is True


def _counting_run(monkeypatch) -> list:
    """Count every `subprocess.run` whose argv0 is the interpreter: the closing check, never run_git's git."""
    real = subprocess.run
    seen: list = []

    def counting(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] == sys.executable:
            seen.append(list(argv))
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    return seen


def test_a_landing_spawns_one_closing_check_per_pending_row(halted, monkeypatch):
    root, _, after1, after2 = halted
    seen = _counting_run(monkeypatch)
    out = _call(root, sha=after2, pending_oracle={"R1": _entry(), "R2": _entry()})
    assert not out["ok"] and sorted(out["pending_oracle_rows"]) == ["R1", "R2"]
    assert len(seen) == 2, seen
    (root / SENTINEL).write_text("closed", encoding="utf-8")
    seen.clear()
    assert _call(root, sha=after2)["ok"]
    assert len(seen) == 2, seen


def _spooled(root: Path) -> list:
    from coordinator_core.ask_spool import record as spool

    out = []
    for spool_id in spool.list_pending(str(root)):
        raw = Path(spool.entry_path(str(root), spool_id)).read_bytes().split(b"\n", 1)[1]
        out.append(json.loads(raw))
    return out


def _with_sizing(marker: Path) -> None:
    data = _record(marker)
    marker.write_text(json.dumps({**data, "sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")


def test_a_pending_refusal_spools_the_resume_refire_once_per_attempt(halted):
    root, marker, _, after2 = halted
    _with_sizing(marker)
    out = _call(root, sha=after2, pending_oracle={"R1": _entry()})
    assert not out["ok"] and out["refire"] == {"resume_run_id": SOLO, "spooled": True}, out
    (payload,) = _spooled(root)
    args = payload["tool_input"]["args"]
    assert args["resume_run_id"] == SOLO and args["sizing"] == "state/sizings/s.yaml"
    assert _record(marker)["oracle_refires"] == 1


def test_the_automatic_refire_is_capped_and_the_run_stays_halted(halted):
    root, marker, _, after2 = halted
    _with_sizing(marker)
    _call(root, sha=after2, pending_oracle={"R1": _entry()})
    for _ in range(pending_oracle.MAX_AUTO_REFIRES + 2):
        out = _call(root, sha=after2)
        assert not out["ok"] and out["pending_oracle_rows"] == ["R1"]
    assert len(_spooled(root)) == pending_oracle.MAX_AUTO_REFIRES
    assert _record(marker)["oracle_refires"] == pending_oracle.MAX_AUTO_REFIRES
    assert _record(marker)["status"] == "halted"
    assert out["refire"] == {"resume_run_id": SOLO}


def test_a_run_with_no_sizing_spools_nothing(halted):
    root, _, _, after2 = halted
    out = _call(root, sha=after2, pending_oracle={"R1": _entry()})
    assert out["refire"] == {"resume_run_id": SOLO} and _spooled(root) == []


def test_a_passing_check_spools_no_refire(halted):
    root, marker, after1, after2 = halted
    _with_sizing(marker)
    (root / SENTINEL).write_text("closed", encoding="utf-8")
    out = _call(root, sha=after2, met_by={"R2": after1}, pending_oracle={"R1": _entry()})
    assert out["ok"], out
    assert _spooled(root) == []


def test_a_landed_under_credit_spawns_one_closing_check_per_pending_row(halted, monkeypatch):
    """The call `ask_gate._credit_split_predecessors` makes reaches the closing check once per stamped row."""
    root, _, _, after2 = halted
    _call(root, sha=after2, pending_oracle={"R1": _entry()})
    seen = _counting_run(monkeypatch)
    out = _call(root, sha=after2, landed_under="warp-2026-10-20-other-20261020T000000-abcdef")
    assert not out["ok"] and out["pending_oracle_rows"] == ["R1"], out
    assert len(seen) == 1, seen


def test_a_landing_with_no_pending_row_spawns_no_closing_check(halted, monkeypatch):
    root, _, after1, after2 = halted
    seen = _counting_run(monkeypatch)
    out = _call(root, sha=after2, met_by={"R1": after1, "R2": after2})
    assert out["ok"], out
    assert seen == []
