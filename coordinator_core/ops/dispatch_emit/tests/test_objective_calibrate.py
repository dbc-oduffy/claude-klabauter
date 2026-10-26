"""Calibration: a falsifier must be red at base and green at the credited sha, else nothing pins."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.ops.dispatch_emit.objective_calibrate import calibrate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

RUN = "run-x"

SCRIPT = """#!__PY__
import json, os, sys
path = __TARGET__
ok = False
if os.path.isfile(path):
    ok = "Lens 8" in open(path, encoding="utf-8").read()
print(json.dumps({"axis": "lens8", "kind": "behaviour", "command": "grep", "base": None, "now": None,
                  "threshold": None, "comparator": "bool", "pass": ok, "evidence": path}))
print(json.dumps({"summary": True, "pass": ok, "failing": [] if ok else ["lens8"]}))
sys.exit(0 if ok else 1)
"""


def _script(target: str) -> str:
    return SCRIPT.replace("__PY__", sys.executable).replace("__TARGET__", repr(target))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, **no_console_creationflags(),
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _repo(root: Path, rel: str, text: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "work")
    return _git(root, "rev-parse", "HEAD")


def _claude_klabauter(tmp_path: Path, target: str, *, base_text: str | None = None) -> Path:
    root = tmp_path / "claude-klabauter"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "falsify.py"
    script.write_text(_script(target), encoding="utf-8")
    script.chmod(0o755)
    if base_text is not None:
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        (root / target).write_text(base_text, encoding="utf-8")
    run_dir = root / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    obj = oc.Objective(verbatim="v", falsifier_script="scripts/falsify.py", axes=[oc.Axis(id="lens8")],
                       written_at="t")
    (run_dir / oc.OBJECTIVE_FILE).write_text(json.dumps(obj.to_json()), encoding="utf-8")
    return root


def _objective(root: Path) -> dict:
    return json.loads((root / "state" / "warp" / RUN / oc.OBJECTIVE_FILE).read_text(encoding="utf-8"))


def test_discriminating_falsifier_passes_and_pins(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    sha = _repo(tmp_path / "work", "lens.md", "Lens 8\n")
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": [{"sha": sha, "repo_root": str(tmp_path / "work"),
                                                                  "axes": ["lens8"]}]}, root)
    assert res["verdict"] == "pass" and res["pinned"] is True
    obj = _objective(root)
    assert obj["pinned_at"] == "wave0" and len(obj["falsifier_sha256"]) == 64
    assert (root / "state" / "warp" / RUN / oc.BASE_FILE).is_file()
    loop = [json.loads(x) for x in (root / "state/warp" / RUN / oc.LOOP_FILE).read_text().splitlines()]
    assert loop[-1]["kind"] == "calibration" and loop[-1]["wave"] == 0
    assert loop[-1]["calibration"]["verdict"] == "pass"
    assert not (root / "scratch/objective-calibration" / RUN / sha).exists()


def test_pass_with_stale_candidate_sha_fails_instead_of_passing_unpinned(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    sha = _repo(tmp_path / "work", "lens.md", "Lens 8\n")
    path = root / "state" / "warp" / RUN / oc.OBJECTIVE_FILE
    path.write_text(json.dumps({**_objective(root), "falsifier_candidate_sha256": "0" * 64}), encoding="utf-8")
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": [{"sha": sha, "repo_root": str(tmp_path / "work"),
                                                                  "axes": ["lens8"]}]}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False
    assert any(p["problem"] == "candidate_sha_mismatch" for p in res["problems"])
    assert _objective(root)["falsifier_sha256"] is None


def test_green_at_base_on_undone_axis_fails_and_does_not_pin(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md", base_text="Lens 8\n")
    res = calibrate({"run_id": RUN, "at": "sizing", "credited": []}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False
    assert any(p["problem"] == "green at base on an undone axis" for p in res["problems"])
    assert _objective(root)["falsifier_sha256"] is None


def test_run_e_replay_claude_klabauter_file_falsifier_fails_against_doe_commit(tmp_path):
    root = _claude_klabauter(tmp_path, "skills/lens.md")
    sha = _repo(tmp_path / "doe", "coordinator/lens.md", "Lens 8\n")
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": [{"sha": sha, "repo_root": str(tmp_path / "doe"),
                                                                  "axes": ["lens8"]}]}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False
    assert any(p["problem"].startswith("red at a credited sha") for p in res["problems"])
    obj = _objective(root)
    assert obj["falsifier_sha256"] is None and obj["pinned_at"] is None


def test_unusable_falsifier_output_fails(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    (root / "scripts" / "falsify.py").write_text(f"#!{sys.executable}\nprint('not json')\n", encoding="utf-8")
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": []}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False


def test_spawn_count_is_one_falsifier_at_base_plus_archive_and_falsifier_per_credited_sha(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import objective_calibrate as mod

    root = _claude_klabauter(tmp_path, "lens.md")
    shas = [_repo(tmp_path / f"w{i}", "lens.md", f"Lens 8 {i}\n") for i in range(2)]
    counts = {"falsifier": 0, "archive": 0}
    real_run, real_stream = mod.subprocess.run, mod.run_git_stream

    def run(*a, **k):
        counts["falsifier"] += 1
        return real_run(*a, **k)

    def stream(*a, **k):
        counts["archive"] += 1
        return real_stream(*a, **k)

    monkeypatch.setattr(mod.subprocess, "run", run)
    monkeypatch.setattr(mod, "run_git_stream", stream)
    credited = [{"sha": s, "repo_root": str(tmp_path / f"w{i}"), "axes": ["lens8"]} for i, s in enumerate(shas)]
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": credited}, root)
    assert res["verdict"] == "pass"
    assert counts == {"falsifier": 3, "archive": 2}


def test_bad_sha_and_missing_objective_refuse(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    res = calibrate({"run_id": RUN, "at": "wave0",
                     "credited": [{"sha": "--output=x", "repo_root": str(root), "axes": ["lens8"]}]}, root)
    assert res["verdict"] == "fail"
    assert calibrate({"run_id": "nope", "at": "wave0"}, root)["calibrated"] is False
