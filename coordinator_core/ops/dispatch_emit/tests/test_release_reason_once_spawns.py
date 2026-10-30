"""One sibling claim pass over three halted runs with kept work: release_reason's body runs once per run and
git is spawned at most twice (one batched cat-file prefetch plus the shared status read)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import sibling_claims

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/s.yaml"
PATHS = ("a.py", "b.py", "c.py")
BASE_TEXT = "def f():\n    return 1\n"
KEPT_TEXT = BASE_TEXT + "\n\ndef g(a, b):\n    return a + b\n"


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str, stdin: str = "") -> str:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _repo_with_three_halted_runs(root: Path):
    """Real repo; each halted run withholds one path whose kept blob is NOT yet at HEAD (so every run holds)."""
    _git(root, "init", "-q")
    for p in PATHS:
        _write(root / p, BASE_TEXT)
    _write(root / SIZING, "status: open\n")
    _git(root, "add", *PATHS)
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    kept_blob = _git(root, "hash-object", "-w", "--stdin", stdin=KEPT_TEXT)
    halted = []
    for i, p in enumerate(PATHS):
        run_id = _rid(300 + i, f"{i}" * 6)
        rec = root / "state/warp" / run_id
        _write(rec / "run.json", json.dumps({"sizing_rel": SIZING}))
        _write(rec / "manifest.json", json.dumps({"rows": [{"writes": [p]}]}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": SIZING, "run_base_sha": base,
            "run_artifacts": {"withheld": {p: "halt-record-only"}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {p: kept_blob}}}, "plan_gate": None}))
        _write(root / p, KEPT_TEXT)  # the halted run's work is dirty in the tree, or the hold releases clean
        halted.append(run_id)
    return halted, _rid(10, "ffffff")


def _count_git_spawns(monkeypatch) -> list:
    from coordinator_core.git import run as git_run

    calls: list = []
    real = git_run.run_git

    def counted(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(git_run, "run_git", counted)
    return calls


def test_claim_pass_over_three_halted_runs_spawns_at_most_two_git(tmp_path, monkeypatch):
    halted, committing = _repo_with_three_halted_runs(tmp_path)
    bodies: list = []
    real_body = hr._release_reason
    monkeypatch.setattr(hr, "_release_reason", lambda root, h, c, p: (bodies.append(h), real_body(root, h, c, p))[1])
    spawns = _count_git_spawns(monkeypatch)
    import subprocess

    raw_git: list = []
    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, *a, **kw: (raw_git.append(argv), real_run(argv, *a, **kw))[1] if argv and argv[0] == "git" else real_run(argv, *a, **kw),
    )

    claims = sibling_claims.sibling_run_claims(tmp_path, committing, PATHS)

    assert len(raw_git) <= 2, raw_git  # a spawn that bypasses run_git is still counted
    assert sorted(claims) == sorted(PATHS)  # every run still holds: the pass is not trivially empty
    assert set(claims.values()) == set(halted)
    cat_files = [c for c in spawns if "cat-file" in c]
    assert len(cat_files) == 1
    assert len(spawns) <= 2, spawns
    for run_id in halted:
        assert bodies.count(run_id) <= 2, (run_id, bodies)  # at most one per distinct (committing plan) key


def test_claim_pass_release_body_runs_once_per_run_for_one_key(tmp_path, monkeypatch):
    halted, committing = _repo_with_three_halted_runs(tmp_path)
    bodies: list = []
    real_body = hr._release_reason
    monkeypatch.setattr(hr, "_release_reason", lambda root, h, c, p: (bodies.append((h, c, p)), real_body(root, h, c, p))[1])

    sibling_claims.sibling_run_claims(tmp_path, committing, PATHS)

    assert len(bodies) == len(set(bodies)), bodies  # no (run, committing, plan) key evaluated twice


def test_corrupt_marker_among_runs_still_holds_inside_the_pass(tmp_path):
    halted, committing = _repo_with_three_halted_runs(tmp_path)
    _write(tmp_path / "state/warp" / halted[0] / "halted.json", "{not json")

    claims = sibling_claims.sibling_run_claims(tmp_path, committing, PATHS)

    assert halted[0] in claims.get("a.py", "")


def _build_130_run_repo(root: Path) -> tuple:
    """Real repo with 130 halted runs, each withholding one path whose kept blob is not yet at HEAD."""
    base_paths = [f"p{i}.py" for i in range(130)]
    _git(root, "init", "-q")
    for p in base_paths:
        _write(root / p, BASE_TEXT)
    _write(root / SIZING, "status: open\n")
    _git(root, "add", *base_paths)
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    kept_blob = _git(root, "hash-object", "-w", "--stdin", stdin=KEPT_TEXT)
    for i, p in enumerate(base_paths):
        rec = root / "state/warp" / _rid(300 + i, f"{i:06x}")
        _write(rec / "run.json", json.dumps({"sizing_rel": SIZING}))
        _write(rec / "manifest.json", json.dumps({"rows": [{"writes": [p]}]}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": SIZING, "run_base_sha": base,
            "run_artifacts": {"withheld": {p: "halt-record-only"}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {p: kept_blob}}}, "plan_gate": None}))
        _write(root / p, KEPT_TEXT)
    return base_paths, _rid(10, "ffffff")


def _full_hold_computation(root: Path, committing: str, paths: list) -> tuple:
    """The claim pass plus the halted-run leg of the live-run claims, under one release_scope."""
    from coordinator_core.ops import warp_run_deliverable

    with hr.release_scope():
        claims = sibling_claims.sibling_run_claims(root, committing, paths)
        halted = warp_run_deliverable.halted_run_write_claims(root, paths, exclude_run=committing)
    return claims, halted


# Process-time pin (time.process_time, never wall clock). The DR-344 500ms bar is not asserted here: the
# remainder below it is L38's. HEAD_FIGURE_S is plain HEAD on this fixture (no release_scope; release_reason
# recomputed per consumer); UNION_FIGURE_S is the union on the same fixture. Ceiling: union plus 20% plus two
# process-clock ticks, never above HEAD. Windows advances process_time in 15.625ms ticks, so 20% of 0.23s is
# under four ticks and one stray tick flaked it (0.281 against 0.276).
HEAD_FIGURE_S = 0.94  # 3 runs: 0.942, 0.963, 0.958
UNION_FIGURE_S = 0.23  # 3 runs: 0.234 each, with the 130 withheld paths dirty (a clean one releases early)
PROCESS_TICK_S = 0.015625
CEILING_S = min(UNION_FIGURE_S * 1.2 + 2 * PROCESS_TICK_S, HEAD_FIGURE_S)


def test_claim_pass_over_130_halted_runs_stays_under_the_process_time_bar(tmp_path):
    base_paths, committing = _build_130_run_repo(tmp_path)

    start = time.process_time()
    claims, halted = _full_hold_computation(tmp_path, committing, base_paths)
    spent = time.process_time() - start

    assert sorted(claims) == sorted(base_paths)  # every run still holds
    assert sorted(halted) == sorted(base_paths)
    assert spent < CEILING_S, (spent, CEILING_S, HEAD_FIGURE_S, UNION_FIGURE_S)
