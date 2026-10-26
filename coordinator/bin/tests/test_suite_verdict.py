"""suite-verdict on a tmp_path git repo with a seeded first-red commit; never the real suite."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
REAL_FOLD_BAR = True

_CLI = Path(__file__).resolve().parents[1] / "suite-verdict.py"
_spec = importlib.util.spec_from_file_location("suite_verdict", _CLI)
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)  # type: ignore[union-attr]

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _run(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str, newline: str = "\n") -> str:
    for name, body in files.items():
        with open(repo / name, "w", encoding="utf-8", newline="") as fh:
            fh.write(body.replace("\n", newline))
    _run([*_GIT, "-C", str(repo), "add", "-A"])
    _run([*_GIT, "-C", str(repo), "commit", "-qm", msg])
    return _run(["git", "-C", str(repo), "rev-parse", "HEAD"])


def _seeded_repo(tmp_path: Path, autocrlf: str = "", newline: str = "\n") -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    if autocrlf:
        _run(["git", "-C", str(repo), "config", "core.autocrlf", autocrlf])
    ok = "def test_ok():\n    assert True\n"
    _commit(repo, {"test_a.py": ok + "def test_b():\n    assert 1 == 1\n"}, "c1", newline)
    _commit(repo, {"notes.txt": "x"}, "c2", newline)
    first_red = _commit(repo, {"test_a.py": ok + "def test_b():\n    assert 1 == 2\n"}, "c3 breaks b", newline)
    head = _commit(repo, {"notes.txt": "y"}, "c4", newline)
    return repo, first_red, head


def _grant(ok: bool):
    return lambda: (ok, "basis: grant" if ok else "no grant")


def test_red_is_bisected_to_the_seeded_first_red_commit(tmp_path: Path):
    repo, first_red, head = _seeded_repo(tmp_path)
    rc = sv.main(["--repo", str(repo)], grant_check=_grant(True))
    assert rc == 1
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{head}.json").read_text())
    assert verdict["sha"] == head and verdict["tier"] == "full"
    assert (verdict["failed"], verdict["passed"]) == (1, 1)
    assert verdict["reds"] == [{
        "nodeid": "test_a.py::test_b", "first_red_commit": first_red,
        "first_red_subject": "c3 breaks b", "bar": "test_a.py",
    }]
    assert verdict["grant_ref"] == "basis: grant"


def test_red_is_bisected_under_autocrlf_true_with_crlf_worktree(tmp_path: Path):
    repo, first_red, head = _seeded_repo(tmp_path, autocrlf="true", newline="\r\n")
    assert sv.main(["--repo", str(repo)], grant_check=_grant(True)) == 1
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{head}.json").read_text())
    assert [(r["nodeid"], r["first_red_commit"]) for r in verdict["reds"]] == [("test_a.py::test_b", first_red)]
    assert (verdict["failed"], verdict["passed"]) == (1, 1)


def test_green_sha_writes_a_green_verdict(tmp_path: Path):
    repo, first_red, _ = _seeded_repo(tmp_path)
    good = _run(["git", "-C", str(repo), "rev-parse", f"{first_red}~1"])
    assert sv.main(["--repo", str(repo), "--sha", good], grant_check=_grant(True)) == 0
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{good}.json").read_text())
    assert verdict["failed"] == 0 and verdict["reds"] == []


def test_no_live_grant_refuses_and_writes_nothing(tmp_path: Path):
    repo, _, _ = _seeded_repo(tmp_path)
    assert sv.main(["--repo", str(repo)], grant_check=_grant(False)) == 3
    assert not (repo / "state").exists()


def test_flaky_red_that_does_not_reproduce_is_recorded_unbisected(tmp_path: Path):
    repo, _, head = _seeded_repo(tmp_path)
    flaky = [sys.executable, "-c", "import sys; print('FAILED t.py::x - boom'); print('1 failed, 2 passed in 0.1s')"]
    sv.main(["--repo", str(repo), "--", *flaky], grant_check=_grant(True))
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{head}.json").read_text())
    assert verdict["reds"] == [
        {"nodeid": "t.py::x", "first_red_commit": "", "first_red_subject": "", "bar": "t.py"}
    ]


def test_unparseable_output_writes_nothing(tmp_path: Path):
    repo, _, _ = _seeded_repo(tmp_path)
    rc = sv.main(["--repo", str(repo), "--", sys.executable, "-c", "print('hi')"], grant_check=_grant(True))
    assert rc == 1 and not (repo / "state").exists()


def test_unresolved_pytest_writes_an_unavailable_verdict_and_exits_zero(tmp_path: Path, monkeypatch):
    repo, _, head = _seeded_repo(tmp_path)
    real = sv.importlib.util.find_spec
    monkeypatch.setattr(sv.importlib.util, "find_spec", lambda n, *a: None if n == "pytest" else real(n, *a))
    assert sv.main(["--repo", str(repo)], grant_check=_grant(True)) == 0
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{head}.json").read_text())
    assert verdict["tier"] == "unavailable" and verdict["reds"] == [] and "pytest" in verdict["unavailable"]
    assert verdict["tier"] != "full"


def test_resolved_pytest_runs_the_suite_not_unavailable(tmp_path: Path):
    repo, _, head = _seeded_repo(tmp_path)
    sv.main(["--repo", str(repo)], grant_check=_grant(True))
    verdict = json.loads((repo / "state/warp/suite-verdicts" / f"{head}.json").read_text())
    assert verdict["tier"] == "full" and "unavailable" not in verdict


def test_parse_counts_errors_as_failed():
    out = "FAILED a.py::t - x\nERROR b.py::u - y\n1 failed, 3 passed, 2 skipped, 1 error in 1.2s\n"
    assert sv.parse_suite_output(out) == {
        "failed": 2, "passed": 3, "skipped": 2, "reds": ["a.py::t", "b.py::u"],
    }


def test_verdict_file_is_read_by_fold_bar(tmp_path: Path):
    from coordinator_core.ops import fold_bar

    repo, first_red, head = _seeded_repo(tmp_path)
    sv.main(["--repo", str(repo)], grant_check=_grant(True))
    res = fold_bar.verdict(head, repo)
    assert not res.ok and res.nodeids == ("test_a.py::test_b",)
    assert "1 failed" in res.reason and "first_red_commit" not in res.reason
    good = _run(["git", "-C", str(repo), "rev-parse", f"{first_red}~1"])
    sv.main(["--repo", str(repo), "--sha", good], grant_check=_grant(True))
    assert fold_bar.verdict(good, repo).ok
