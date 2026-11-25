"""A `resumed_from` refire counts its same-sizing predecessor's delta as its own: the staged
`foreign_at_baseline` never lists a path the predecessor wrote, the clean-export mirror carries that delta,
and a path dirty before the chain began (never written by it) stays foreign.

The predecessor `ask-*` record halted WITHOUT `run_base_sha`, so the refire's first stage derives its marker
fresh against HEAD; a refire that already halted with a base inherits that poisoned marker."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, review_edits, run_write_prints
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING_REL = "state/sizings/2026-11-24-fixture.yaml"
PRED = "ask-20261124T200536-e6b75f"
REFIRE = "warp-2026-11-24-fixture-e6b75f-20261124T211323-26e8bb"
MOD = "pkg/mod.py"
TEST = "tests/test_mod.py"
UNRELATED = "pkg/unrelated.py"
PAD = "".join(f"# pad {i:02d}\n" for i in range(30))
HEAD_MOD = "def old():\n    return 1\n" + PAD
PRED_MOD = "def old():\n    return 1\n\n\ndef added():\n    return 2\n" + PAD
PEER_MOD = PRED_MOD + "PEER = 1\n"
TEST_TEXT = "from pkg.mod import added\n\n\ndef test_added():\n    assert added() == 2\n"
WRITES = [MOD, TEST]


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _record(root: Path, run_id: str, **files) -> Path:
    rec = ask_stage.record_dir(root, run_id)
    rec.mkdir(parents=True, exist_ok=True)
    for name, doc in files.items():
        (rec / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    return rec


@pytest.fixture
def chain(tmp_path):
    """A repo at HEAD, the predecessor's halted record (no `run_base_sha`; its manifest baseline lists MOD
    and TEST as the stage found them), the predecessor's edits in the worktree, an unrelated dirty tracked
    file the predecessor never baselined, and a refire run.json naming it via `resumed_from`."""
    root = seed_repo(tmp_path / "repo")
    _put(root, ".gitignore", "state/\nscratch/\n.coordinator-local/\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, MOD, HEAD_MOD)
    _put(root, UNRELATED, "U = 1\n")
    _git(["add", ".gitignore", "pkg"], root)
    _git(["commit", "-q", "-m", "pkg"], root)
    _put(root, SIZING_REL, yaml.safe_dump({
        "schema": "sizing-object",
        "name": "refire fixture",
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
    pred_baseline = {MOD: fingerprint(root, MOD), TEST: None}
    _record(root, PRED, halted={"status": "halted", "halted": "x"}, manifest={"write_baseline": pred_baseline})
    _record(root, PRED, run={"sizing_rel": SIZING_REL})
    _put(root, MOD, PRED_MOD)
    _put(root, TEST, TEST_TEXT)
    _put(root, UNRELATED, "U = 2  # dirty before the chain began\n")
    _record(root, REFIRE, run={"sizing_rel": SIZING_REL, "resumed_from": PRED})
    return root


def _stage(root: Path, writes=WRITES) -> dict:
    return ask_stage.stage(root, run_id=REFIRE, sizing_rel=SIZING_REL, writes=list(writes)).foreign_at_baseline


def test_a_fresh_refire_stage_does_not_mark_the_predecessor_written_paths_foreign(chain):
    marker = _stage(chain)

    assert not {MOD, TEST} & set(marker), marker


def test_a_refire_holding_a_poisoned_inherited_marker_drops_the_chain_written_paths_on_its_next_stage(chain):
    head = _git(["rev-parse", "HEAD"], chain).strip()
    baseline = {MOD: fingerprint(chain, MOD), TEST: fingerprint(chain, TEST)}
    _record(
        chain, REFIRE, halted={"status": "halted", "halted": "x", "run_base_sha": head},
        manifest={"write_baseline": baseline, "foreign_at_baseline": dict(baseline)},
    )

    marker = _stage(chain)

    assert not {MOD, TEST} & set(marker), marker


def _mirror(root: Path, probe: list, overrides_for=WRITES) -> "object":
    paths, overrides = cov.own_delta(root, REFIRE, list(overrides_for))
    return cov.run_in_export(root, REFIRE, paths, probe, overrides)


@pytest.fixture
def no_peer(monkeypatch):
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])


def test_the_default_export_mirror_carries_the_predecessor_bytes_and_the_importing_test_passes(chain, no_peer):
    assert not {MOD, TEST} & set(_stage(chain))

    ran = _mirror(chain, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST])

    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_path_a_live_peer_also_declares_reaches_the_mirror_at_head_plus_the_chains_hunks_only(
    chain, monkeypatch
):
    assert not {MOD, TEST} & set(_stage(chain))
    run_write_prints.record(chain, REFIRE, [MOD])
    _put(chain, MOD, PEER_MOD)
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {MOD: "warp-peer-run"})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    probe = (
        "t = open('pkg/mod.py', encoding='utf-8').read();"
        "raise SystemExit(0 if 'def added' in t and 'PEER' not in t and 'def old' in t else 3)"
    )

    ran = _mirror(chain, [sys.executable, "-c", probe])

    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_path_dirty_before_the_chain_began_and_never_written_by_it_stays_foreign(chain):
    marker = _stage(chain, [*WRITES, UNRELATED])

    assert UNRELATED in marker and not {MOD, TEST} & set(marker), marker
