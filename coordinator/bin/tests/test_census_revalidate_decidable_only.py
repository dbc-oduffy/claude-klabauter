"""coordinator/bin/tests/test_census_revalidate_decidable_only.py — the census leg's skip and memo.

Subject: `can_drift`, `revalidate(decidable_only=, memo=)` and `spent_ms` in
`coordinator/bin/mise-census-revalidate.py`. A skipped entry is UNDECIDABLE and spawns nothing;
the proof that skipping preserves verdicts is that `compare` never DRIFTs a result `can_drift`
rules out. The memo holds `MemoEntry` values and hits only on an equal `input_key`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_do", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan(path: Path, entries: list[tuple[str, str]]) -> Path:
    lines = ["---", "mise_prepped_sha: abc", "census:"]
    for command, result in entries:
        lines += [f"  - question: q", f"    command: {command!r}", f"    result: {result!r}"]
    lines += ["---", "body", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class _SpawnCounter:
    def __init__(self, monkeypatch, mcr):
        self.calls = 0
        real = subprocess.run

        def counting(*a, **kw):
            self.calls += 1
            return real(*a, **kw)

        monkeypatch.setattr(mcr.subprocess, "run", counting)


def test_all_prose_plan_spawns_nothing_under_decidable_only(mcr, tmp_path, monkeypatch):
    plan = _plan(tmp_path / "p.md", [("echo a", "prose answer"), ("echo b", "12 (prose)")])
    counter = _SpawnCounter(monkeypatch, mcr)
    rep = mcr.revalidate(plan, tmp_path, 10, decidable_only=True)
    assert counter.calls == 0
    assert [e["state"] for e in rep["entries"]] == [mcr.UNDECIDABLE, mcr.UNDECIDABLE]
    assert all(e["basis"] == "not run: recorded result cannot drift" for e in rep["entries"])
    assert all(e["observed"] == "" for e in rep["entries"])
    assert isinstance(rep["spent_ms"], int)


def test_decidable_entries_still_run_under_decidable_only(mcr, tmp_path, monkeypatch):
    plan = _plan(tmp_path / "p.md", [("echo 5", "7"), ("echo a", "prose")])
    counter = _SpawnCounter(monkeypatch, mcr)
    rep = mcr.revalidate(plan, tmp_path, 10, decidable_only=True)
    assert counter.calls == 1
    assert rep["entries"][0]["state"] == mcr.DRIFT
    assert rep["entries"][1]["state"] == mcr.UNDECIDABLE


def test_refused_entry_is_still_reported_under_decidable_only(mcr, tmp_path, monkeypatch):
    plan = _plan(tmp_path / "p.md", [("rm -rf x", "prose")])
    counter = _SpawnCounter(monkeypatch, mcr)
    rep = mcr.revalidate(plan, tmp_path, 10, decidable_only=True)
    assert counter.calls == 0
    assert rep["entries"][0]["state"] == mcr.REFUSED


CANNOT_DRIFT = ["prose answer", "12 (prose)", "two names, alpha and beta", "7 — the line", ""]
CAN_DRIFT = ["5", "-3", " 42 ", "count 3; x"]


@pytest.mark.parametrize("result", CANNOT_DRIFT)
def test_compare_never_drifts_a_result_can_drift_rules_out(mcr, result):
    assert mcr.can_drift({"command": "echo", "result": result}) is False
    battery = ["", "0", "5", "a\nb\nc\nd\ne\nf", result, "path:3", "12 file.py\n"]
    for out in battery:
        assert mcr.compare(result, out)["state"] != mcr.DRIFT, (result, out)


@pytest.mark.parametrize("result", CAN_DRIFT)
def test_can_drift_true_for_bare_int_and_count_prefix(mcr, result):
    assert mcr.can_drift({"command": "echo", "result": result}) is True


def test_can_drift_true_for_declared_count_and_typed_not_for_bool(mcr):
    assert mcr.can_drift({"command": "echo", "result": "prose", "count": 3}) is True
    assert mcr.can_drift({"command": "echo", "result": "prose", "count": True}) is False
    assert mcr.can_drift({"kind": "next-free-number", "result": "prose"}) is True


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("5\n", encoding="utf-8")
    return repo


def test_shared_memo_spawns_once_and_judges_each_entry_on_its_own_result(
    mcr, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    a = _plan(repo / "a.md", [("cat f.txt", "5")])
    b = _plan(repo / "b.md", [("cat f.txt", "7")])
    counter = _SpawnCounter(monkeypatch, mcr)
    memo: dict = {}
    rep_a = mcr.revalidate(a, repo, 10, memo=memo)
    rep_b = mcr.revalidate(b, repo, 10, memo=memo)
    assert counter.calls == 1
    assert rep_a["entries"][0]["state"] == mcr.MATCH
    assert rep_b["entries"][0]["state"] == mcr.DRIFT
    assert (rep_a["memo_hits"], rep_b["memo_hits"]) == (0, 1)
    assert list(memo) == ["cat f.txt"]
    assert isinstance(memo["cat f.txt"], mcr.MemoEntry)


def test_timed_out_entry_is_not_memoised(mcr, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    plan = _plan(repo / "p.md", [("cat f.txt", "5")])

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(mcr.subprocess, "run", boom)
    memo: dict = {}
    rep = mcr.revalidate(plan, repo, 1, memo=memo)
    assert rep["entries"][0]["state"] == mcr.UNRUNNABLE
    assert memo == {}


def test_spent_deadline_spawns_nothing_and_stores_nothing_under_memo(mcr, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    plan = _plan(repo / "p.md", [("cat f.txt", "5")])
    counter = _SpawnCounter(monkeypatch, mcr)
    memo: dict = {}
    rep = mcr.revalidate(plan, repo, 10, deadline=mcr.time.monotonic() - 1, memo=memo)
    assert counter.calls == 0
    assert rep["entries"][0]["state"] == mcr.UNRUNNABLE
    assert memo == {}


GOLDEN = (
    "census: DRIFT — p.md\n"
    "  DRIFT        wc -l < f.txt\n"
    "    basis    recorded 5, observed 2\n"
    "    recorded 5\n"
    "    observed 2\n"
    "  UNDECIDABLE  cat f.txt\n"
    "    basis    recorded result is prose; no decidable comparison\n"
    "    recorded two names, alpha and beta\n"
    "    observed alpha\n"
    "beta\n"
    "  MATCH        echo hi\n"
    "\n"
    "1 plan(s); entries: DRIFT 1, MATCH 1, UNDECIDABLE 1\n"
)


def test_defaults_leave_cli_stdout_identical_to_head(mcr, tmp_path, capsys):
    (tmp_path / "f.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    _plan(
        tmp_path / "p.md",
        [
            ("wc -l < f.txt", "5"),
            ("cat f.txt", "two names, alpha and beta"),
            ("echo hi", "hi"),
        ],
    )
    rc = mcr.main(["p.md", "--repo-root", str(tmp_path)])
    assert rc == 1
    assert capsys.readouterr().out == GOLDEN
