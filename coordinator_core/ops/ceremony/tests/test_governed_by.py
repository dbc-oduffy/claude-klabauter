"""Governed-By trailer builder, parser, ledger read and the commit_message / hand_land stamping."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import governed_by
from coordinator_core.ops.ceremony.commit_message import compose_message


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("plan\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "plan")
    run = tmp_path / "state/warp/run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"plan_rel": "docs/plans/p.md", "sizing_rel": "state/sizings/none.yaml"}), encoding="utf-8")
    return tmp_path


def test_format_and_parse_round_trip() -> None:
    line = governed_by.format_trailer("a" * 40, "2026-11-26T20:00:00+00:00", "docs\\plans\\p.md")
    assert line == f"Governed-By: {'a' * 40} 2026-11-26T20:00:00+00:00 docs/plans/p.md"
    msg = f"subject\n\nbody\n\nHand-Landed: true\n{line}\n"
    assert governed_by.parse_trailers(msg) == [{"sha": "a" * 40, "ts": "2026-11-26T20:00:00+00:00", "path": "docs/plans/p.md"}]
    assert governed_by.parse_trailers("subject\n\nGoverned-By: not a trailer line\n") == []


def test_compose_message_appends_governed_by_to_trailer_paragraph() -> None:
    line = governed_by.format_trailer("b" * 40, "2026-11-26T20:00:00+00:00", "docs/plans/p.md")
    msg = compose_message(subject="s", prose="p", trailers="Plan: x", governed_by=[line])
    assert msg.endswith(f"Plan: x\n{line}\n")
    assert governed_by.parse_trailers(msg)[0]["sha"] == "b" * 40
    assert compose_message(subject="s") == "s\n"


def test_trailer_lines_resolve_to_the_plan_landing_commit(repo: Path) -> None:
    plan_sha = _git(repo, "rev-parse", "HEAD")
    lines = governed_by.trailer_lines(repo, "run-1")
    assert len(lines) == 1
    entry = governed_by.parse_trailers(f"s\n\n{lines[0]}\n")[0]
    assert entry["sha"] == plan_sha and entry["path"] == "docs/plans/p.md"
    assert governed_by.unresolved_shas(repo, [plan_sha]) == []
    assert governed_by.unresolved_shas(repo, ["0" * 40]) == ["0" * 40]


def test_trailer_lines_empty_without_run_or_commit(repo: Path) -> None:
    assert governed_by.trailer_lines(repo, None) == []
    assert governed_by.trailer_lines(repo, "no-such-run") == []


def test_ledger_lists_trailers_of_commits_touching_path(repo: Path) -> None:
    line = governed_by.trailer_lines(repo, "run-1")[0]
    target = repo / "src.py"
    target.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit\n\n{line}")
    other = repo / "other.py"
    other.write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no provenance")
    entries = governed_by.ledger(repo, "src.py")
    assert [e["path"] for e in entries] == ["docs/plans/p.md"]
    assert governed_by.ledger(repo, "other.py") == []
    reply = governed_by._handler({"repo_root": str(repo), "path": "src.py"})
    assert reply["ok"] and reply["heading"] in reply["text"] and entries[0]["ts"] in reply["text"]
    assert governed_by._handler({"repo_root": str(repo)})["refused"] == "params"


def test_ledger_is_one_git_spawn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinator_core.git.run as run_mod

    calls: list = []
    real = run_mod.run_git
    monkeypatch.setattr(run_mod, "run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    governed_by.ledger(repo, "docs/plans/p.md")
    assert len(calls) == 1


def test_stamp_joins_the_trailer_paragraph_once(repo: Path) -> None:
    msg = "subject\n\nbody\n\nSession-Id: s1\n"
    stamped = governed_by.stamp(msg, repo, "run-1")
    assert stamped.startswith(msg) and len(governed_by.parse_trailers(stamped)) == 1
    assert governed_by.stamp(stamped, repo, "run-1") == stamped
    assert governed_by.stamp(msg, repo, None) == msg
    assert governed_by.stamp("subject only\n", repo, "run-1").count("\n\nGoverned-By:") == 1


def test_stamp_derives_the_run_from_the_message(repo: Path) -> None:
    plan_sha = _git(repo, "rev-parse", "HEAD")
    for msg in ("halt run-1\n\nRun-Id: run-1\n", "checkpoint\n\nCheckpoint-Run-Id: run-1\n"):
        assert [e["sha"] for e in governed_by.parse_trailers(governed_by.stamp(msg, repo))] == [plan_sha]


def test_stamp_derives_the_plan_from_a_path_the_message_names(repo: Path) -> None:
    plan_sha = _git(repo, "rev-parse", "HEAD")
    stamped = governed_by.stamp("review trail\n\nPlan: docs/plans/p.md\n", repo)
    assert [(e["sha"], e["path"]) for e in governed_by.parse_trailers(stamped)] == [(plan_sha, "docs/plans/p.md")]
    unlanded = "review trail\n\nPlan: docs/plans/never-landed.md\n"
    assert governed_by.stamp(unlanded, repo) == unlanded
    assert governed_by.stamp("plain commit\n", repo) == "plain commit\n"


def test_commit_v2_stamps_provenance_and_the_ledger_is_registered() -> None:
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.ops import _EAGER_OP_MODULES

    assert OP_CLASSIFICATION[governed_by.OP_NAME] is OpClass.COMPUTE_ONLY
    assert any(mod == "coordinator_core.ops.ceremony.governed_by" and governed_by.OP_NAME in desc for mod, desc in _EAGER_OP_MODULES)
    inventory = json.loads((Path(__file__).resolve().parents[4] / ".github/op-inventory.json").read_text(encoding="utf-8"))
    rows = [r for r in (inventory if isinstance(inventory, list) else inventory.get("ops", [])) if r.get("op_key") == governed_by.OP_NAME]
    assert [r["classification"] for r in rows] == ["COMPUTE_ONLY"]


_SID = "11111111-2222-4333-8444-555555555555"


def _commit(repo: Path, name: str, message: str) -> dict:
    from coordinator_core.ops.ceremony import commit_v2

    (repo / name).write_text("x = 1\n", encoding="utf-8")
    return commit_v2._handler({"paths": [name], "message": message, "session_id": _SID}, repo_root=repo / ".git")


def test_commit_v2_lands_the_governed_by_trailer(repo: Path) -> None:
    plan_sha = _git(repo, "rev-parse", "HEAD")
    assert _commit(repo, "b.py", "add b\n\nPlan: docs/plans/p.md\n")["committed"] is True
    assert [e["sha"] for e in governed_by.parse_trailers(_git(repo, "log", "-1", "--format=%B"))] == [plan_sha]
    assert _commit(repo, "c.py", "add c, see docs/plans/p.md in prose\n")["committed"] is True
    assert governed_by.parse_trailers(_git(repo, "log", "-1", "--format=%B")) == []


def test_provenance_findings_over_a_real_log(repo: Path) -> None:
    line = governed_by.trailer_lines(repo, "run-1")[0]
    base = _git(repo, "rev-parse", "HEAD")
    for name, msg in (("a.py", f"governed\n\nSession-Id: s1\n{line}"), ("b.py", "bare\n\nSession-Id: s1"),
                      ("c.py", "human"), ("d.md", "docs\n\nSession-Id: s1")):
        (repo / name).write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", msg)
    bare = _git(repo, "rev-parse", "HEAD~2")
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-p", "-U0", "--no-renames", "--format=\x1e%H\x1f%B\x1f", f"{base}..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    rows = governed_by.provenance_findings(repo, log)
    assert [(r["commit"], r["reason"], r["paths"]) for r in rows] == [(bare, "missing", ["b.py"])]


def test_land_check_folds_sha_resolution_into_the_detector_batch(repo: Path) -> None:
    from coordinator_core.ops import review_reverify as rr

    plan_sha = _git(repo, "rev-parse", "HEAD")
    code = repo / "pkg/mod.py"
    code.parent.mkdir()
    base = plan_sha
    msgs = [("a", plan_sha), ("b", "0" * 40)]
    for name, sha in msgs:
        code.write_text(f"{name} = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        trailer = governed_by.format_trailer(sha, "2026-11-26T20:00:00+00:00", "docs/plans/p.md")
        _git(repo, "commit", "-q", "-m", f"edit {name}\n\nSession-Id: sess-1\n{trailer}")
    spawned: list = []
    real = subprocess.run

    def counting(*a, **k):
        spawned.append(a[0])
        return real(*a, **k)

    import unittest.mock as mock

    full_base = _git(repo, "rev-parse", base)
    with mock.patch("coordinator_core.commenting.authorship.subprocess.run", counting):
        _comments, rows = rr._land_findings(repo, full_base)
    assert len(spawned) == 2
    assert [(r["reason"], r["shas"]) for r in rows] == [("unresolved", ["0" * 40])]
