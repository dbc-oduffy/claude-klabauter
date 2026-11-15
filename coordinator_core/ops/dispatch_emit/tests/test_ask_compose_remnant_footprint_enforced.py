"""A remnant-complete leg's write outside its recorded footprint is withheld at landing by the engine
(`outside-row-footprint`), whether the leg reported the path or its self-report is clean; in-footprint
writes still land."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    RUN,
    _call,
    _git,
    _names,
    _write,
    repo,
)

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: E402,F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

STRAY = "stray.py"


def _record_remnant(root, *, footprint, paths) -> None:
    remnant = {
        "field": "remnant", "loop_owner": "engine", "cap": 1, "items": [], "footprint": footprint,
        "status": "blocked" if paths else "open", "paths": paths, "last_reason": "",
    }
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/{lr.RECOVERY_RECORD_FILE}", json.dumps({"remnant": remnant}))


def test_a_reported_out_of_footprint_remnant_write_is_withheld_while_the_in_footprint_write_lands(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[STRAY])

    out = _call(repo)

    assert out["committed"] is True, out
    assert out["footprint_breach"] == [STRAY]
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert "a.py" in landed and STRAY not in landed
    assert (repo / STRAY).read_text(encoding="utf-8") == "x = 1\n"


def test_an_unreported_out_of_footprint_remnant_write_with_a_clean_self_report_is_never_landed(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[])

    out = _call(repo)

    assert out["committed"] is True, out
    assert "footprint_breach" not in out
    landed = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())
    assert "a.py" in landed and STRAY not in landed
    assert STRAY in _git(repo, "status", "--porcelain", "--", STRAY)


def test_a_record_whose_status_hides_the_breach_still_withholds_it(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _write(repo, STRAY, "x = 1\n")
    _record_remnant(repo, footprint=["a.py"], paths=[STRAY])
    path = repo / RUN_DIR_ROOT / RUN / lr.RECOVERY_RECORD_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["remnant"]["status"] = "open"
    path.write_text(json.dumps(data), encoding="utf-8")

    out = _call(repo)

    assert out["footprint_breach"] == [STRAY]
    assert STRAY not in set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split())


_PATH = "pkg/a.py"
_BASE = "".join(f"line {n}\n" for n in range(1, 11))
_FOREIGN = _BASE.replace("line 2\n", "FOREIGN 2\n")
_OWN = _BASE.replace("line 8\n", "OWN 8\n")
_LEG_WRITES_HOOK = r"""
  if (label && (label.startsWith('live-snap') || label.startsWith('live-restore'))) {
    const cmd = String(prompt).split('do not retry it:\n\n')[1].split('\n\nReturn')[0].replace(/\bREPO\b/g, process.env.REPO_ROOT);
    const out = require('child_process').execFileSync(process.env.BASH_EXE, ['-c', cmd], { encoding: 'utf8' });
    return JSON.parse(out.trim().split('\n').pop());
  }
  if (label === 'remnant-complete') {
    for (const [p, c] of Object.entries(cfg.leg_writes ?? {})) require('fs').writeFileSync(p, c);
    return cfg.replies[label];
  }
"""


def _git_repo(root):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True,
            **no_console_creationflags(),
        )

    (root / "pkg").mkdir(parents=True)
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    (root / _PATH).write_bytes(_BASE.encode())
    git("add", _PATH)
    git("commit", "-q", "-m", "base")


def _drive_remnant(root, monkeypatch, *, leg_writes):
    """Runs the composed ask script through its remnant loop in `root`, the leg stub writing `leg_writes`; the
    live-snap and live-restore legs execute their real engine commands."""
    import shutil
    import subprocess

    from coordinator_core.ops.dispatch_emit import ask_compose
    from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
    from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_ledger_recovery_loop as loop
    from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import readers_cfg, stage_reply
    from coordinator_core.win_portability import no_console_creationflags

    node, bash = shutil.which("node"), shutil.which("bash")
    if node is None or bash is None:
        pytest.skip("node or bash not on PATH")
    marker = "  if (label === 'halt-commit' || label === 'commit') return cfg.commit;"
    assert marker in loop._NODE_PROGRAM
    program = loop._NODE_PROGRAM.replace(marker, _LEG_WRITES_HOOK + marker, 1)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=loop._SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (loop._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": loop._RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": stage_reply(loop._DISK),
    }
    cfg = {
        "token": loop._TOKEN, "verify": {"items": [loop._ORPHAN]}, "commit": loop._LANDED, **readers_cfg(),
        "leg_writes": leg_writes,
        "replies": {
            "gate": gate, "gate-stage": gate,
            "remnant-complete": {"status": "DONE", "paths": [_PATH], "reason": "completed"},
            "remnant-reverify": {"items": [loop._MET]},
        },
    }
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=root,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg), "BASH_EXE": bash, "REPO_ROOT": root.as_posix()},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _remnant_rec(out: dict) -> dict:
    prompt = next(c for c in out["calls"] if c["label"] == "remnant-record")["prompt"]
    return json.loads(prompt.split("exactly this JSON text: ", 1)[1].split(". Edit nothing else", 1)[0])


def test_a_remnant_leg_that_writes_head_plus_own_over_a_foreign_hunk_keeps_the_foreign_hunk(tmp_path, monkeypatch):
    root = tmp_path
    _git_repo(root)
    (root / _PATH).write_bytes(_FOREIGN.encode())

    out = _drive_remnant(root, monkeypatch, leg_writes={_PATH: _OWN})

    assert (root / _PATH).read_text(encoding="utf-8") == _FOREIGN.replace("line 8\n", "OWN 8\n")
    rec = _remnant_rec(out)
    assert rec["live_tree_writes"] == [_PATH] and rec["live_restored"] == [_PATH]
    assert rec["status"] == "fixed" and "live_refused" not in rec
    labels = [c["label"] for c in out["calls"]]
    assert labels.index("remnant-complete") < labels.index("live-snap-after:remnant") < labels.index("live-restore:remnant")
    assert labels.index("live-restore:remnant") < labels.index("remnant-reverify")


def test_a_remnant_leg_whose_write_overlaps_a_foreign_hunk_is_refused_and_not_accepted_as_fixed(tmp_path, monkeypatch):
    root = tmp_path
    _git_repo(root)
    (root / _PATH).write_bytes(_FOREIGN.encode())
    clash = _BASE.replace("line 2\n", "OWN 2\n")

    out = _drive_remnant(root, monkeypatch, leg_writes={_PATH: clash})

    assert (root / _PATH).read_text(encoding="utf-8") == clash
    rec = _remnant_rec(out)
    assert rec["status"] == "blocked" and "remnant-reverify" not in [c["label"] for c in out["calls"]]
    assert rec["live_refused"][0]["path"] == _PATH and "kept as blob" in rec["live_refused"][0]["reason"]
    assert _PATH in rec["last_reason"]
    record_dir = root / next(p for p in root.rglob("tree-write-refusals.json")).parent.relative_to(root)
    entries = json.loads((record_dir / "tree-write-refusals.json").read_text(encoding="utf-8"))
    assert entries[0]["path"] == _PATH and entries[0]["run_id"] == "run-1"


def test_a_remnant_leg_over_a_clean_file_restores_nothing(tmp_path, monkeypatch):
    root = tmp_path
    _git_repo(root)

    out = _drive_remnant(root, monkeypatch, leg_writes={_PATH: _OWN})

    assert (root / _PATH).read_text(encoding="utf-8") == _OWN
    rec = _remnant_rec(out)
    assert rec["status"] == "fixed" and "live_restored" not in rec and "live_refused" not in rec


def test_an_in_footprint_only_remnant_record_withholds_nothing(repo):  # noqa: F811
    _write(repo, "a.py", "a = 2\n")
    _record_remnant(repo, footprint=["a.py"], paths=[])

    out = _call(repo)

    assert out["committed"] is True and "footprint_breach" not in out, out
    assert "a.py" in _names(repo, out["sha"])
