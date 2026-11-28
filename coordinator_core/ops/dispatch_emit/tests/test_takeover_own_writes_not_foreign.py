"""A taken-over run counts the dead session's recorded writes as its own, never as `foreign_at_baseline`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage, resume_gate
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, review_edits, run_write_prints
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING_REL = "state/sizings/2026-11-28-fixture.yaml"
RUN = "warp-2026-11-28-fixture-cdffc5-20261128T051056-f97c57"
MOD = "pkg/mod.py"
TEST = "tests/test_mod.py"
UNRELATED = "pkg/unrelated.py"
PAD = "".join(f"# pad {i:02d}\n" for i in range(30))
HEAD_MOD = "def old():\n    return 1\n" + PAD
DEAD_MOD = "def old():\n    return 1\n\n\ndef added():\n    return 2\n" + PAD
TEST_TEXT = "from pkg.mod import added\n\n\ndef test_added():\n    assert added() == 2\n"
WRITES = [MOD, TEST, UNRELATED]


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _record(root: Path, **files) -> Path:
    rec = ask_stage.record_dir(root, RUN)
    rec.mkdir(parents=True, exist_ok=True)
    for name, doc in files.items():
        (rec / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    return rec


@pytest.fixture
def taken_over(tmp_path):
    """A taken-over run's record (takeover row, no `halted.json`) after the dead session edited MOD and TEST."""
    root = seed_repo(tmp_path / "repo")
    _put(root, ".gitignore", "state/\nscratch/\n.coordinator-local/\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, MOD, HEAD_MOD)
    _put(root, UNRELATED, "U = 1\n")
    _git(["add", ".gitignore", "pkg"], root)
    _git(["commit", "-q", "-m", "pkg"], root)
    _put(root, SIZING_REL, yaml.safe_dump({
        "schema": "sizing-object",
        "name": "takeover fixture",
        "intent": f"Fix {MOD} and {TEST}.",
        "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "exit_criterion": {"statement": "ok", "accepted": None},
    }))
    _put(root, UNRELATED, "U = 2  # dirty before the run began\n")
    rec = _record(
        root,
        manifest={
            "write_baseline": {MOD: fingerprint(root, MOD), TEST: None},
            "foreign_at_baseline": {UNRELATED: fingerprint(root, UNRELATED)},
        },
        run={"sizing_rel": SIZING_REL},
    )
    resume_gate.record_takeover(rec, "dead-session", "live-session", "driver died")
    assert not (rec / "halted.json").exists()
    _put(root, MOD, DEAD_MOD)
    _put(root, TEST, TEST_TEXT)
    return root


def _stage(root: Path):
    return ask_stage.stage(root, run_id=RUN, sizing_rel=SIZING_REL, writes=list(WRITES))


def _manifest(root: Path) -> dict:
    return json.loads((ask_stage.record_dir(root, RUN) / "manifest.json").read_text(encoding="utf-8"))


def test_a_takeover_restage_does_not_mark_the_dead_sessions_written_paths_foreign(taken_over):
    marker = _stage(taken_over).foreign_at_baseline

    assert not {MOD, TEST} & set(marker), marker


def test_a_path_dirty_before_the_run_began_stays_foreign_through_a_takeover_restage(taken_over):
    marker = _stage(taken_over).foreign_at_baseline

    assert UNRELATED in marker and not {MOD, TEST} & set(marker), marker


def test_the_restaged_write_baseline_keeps_the_recorded_value_and_a_second_takeover_restage_still_credits_the_edit(
    taken_over,
):
    head_fp = _manifest(taken_over)["write_baseline"][MOD]

    first = _stage(taken_over).foreign_at_baseline
    after_first = _manifest(taken_over)["write_baseline"]
    second = _stage(taken_over).foreign_at_baseline
    after_second = _manifest(taken_over)["write_baseline"]

    assert not {MOD, TEST} & set(first), first
    assert after_first[MOD] == head_fp and after_first[TEST] is None, after_first
    assert not {MOD, TEST} & set(second), second
    assert after_second[MOD] == head_fp and after_second[TEST] is None, after_second


@pytest.fixture
def no_peer(monkeypatch):
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])


def test_the_default_export_mirror_carries_the_dead_sessions_bytes_and_the_importing_test_passes(taken_over, no_peer):
    _stage(taken_over)
    paths, overrides = cov.own_delta(taken_over, RUN, list(WRITES))

    ran = cov.run_in_export(
        taken_over, RUN, paths, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST], overrides
    )

    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_path_a_live_peer_also_declares_reaches_the_mirror_at_head_plus_the_dead_sessions_hunks_only(
    taken_over, monkeypatch
):
    _stage(taken_over)
    run_write_prints.record(taken_over, RUN, [MOD])
    _put(taken_over, MOD, DEAD_MOD + "PEER = 1\n")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {MOD: "warp-peer-run"})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    probe = (
        "t = open('pkg/mod.py', encoding='utf-8').read();"
        "raise SystemExit(0 if 'def added' in t and 'PEER' not in t and 'def old' in t else 3)"
    )
    paths, overrides = cov.own_delta(taken_over, RUN, list(WRITES))

    ran = cov.run_in_export(taken_over, RUN, paths, [sys.executable, "-c", probe], overrides)

    assert ran.returncode == 0, ran.stdout + ran.stderr
