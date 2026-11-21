"""A review fix to a tracked file outside the declared writes is listed, landed and tested, end to end.

Replays the example-stats-repo run shape in a tmp_path git repo: ``src/defaults.ts`` is the declared write and the
review stage edits the tracked ``test/contracts.test.ts``. Each stage is driven through its production
entry point: ``review.reverify`` list mode, ``select_tests``, ``_review_recorded_edits`` into
``_review_edit_verdict``, and the composed reconcile and tests-not-run script blocks under node.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import ask_compose, review_edit_source, review_edits
from coordinator_core.ops.dispatch_emit import terminal_commit_review as tcr
from coordinator_core.ops.reverify_scope import select_tests
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SESSION = "sess-own"
PREFIX = "prep-e2e"
DECLARED = "src/defaults.ts"
OUTSIDE = "test/contracts.test.ts"
DEFAULTS_TEST = "test/defaults.test.ts"
ORPHAN = "src/orphan.ts"
_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *argv], check=True, env=_ENV, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def run_shape(tmp_path, monkeypatch):
    """A repo whose base commit tracks the declared file and two ``*.test.ts`` files, a pre-review
    snapshot naming only the declared file, and a review stage that edits the declared file and
    ``test/contracts.test.ts``. Returns ``(root, base, head, reverify)`` with the review edits committed at ``head``."""
    files = {
        DECLARED: b"export const A = 1;\n",
        OUTSIDE: b"expect(A).toBe(1);\n",
        DEFAULTS_TEST: b"expect(A).toBeDefined();\n",
        ORPHAN: b"export const O = 1;\n",
    }
    for rel, data in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", *files)
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / _sha(files[DECLARED])).write_bytes(files[DECLARED])
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": base,
            "recorded_at": "2026-11-11T00:00:00Z",
            "paths": [DECLARED],
            "files": {DECLARED: _sha(files[DECLARED])},
        }),
        encoding="utf-8",
    )
    (tmp_path / DECLARED).write_bytes(b"export const A = 2;\n")
    (tmp_path / OUTSIDE).write_bytes(b"expect(A).toBe(2);\n")

    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {DECLARED: "a1", OUTSIDE: "a1"})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(
        review_edit_source, "session_agent_writes",
        lambda *_a, **_k: {"a1": {f".coordinator-local/x/{PREFIX}/note.md"}},
    )
    reverify = rr.review_reverify(tmp_path, base=base, slice_prefix=PREFIX, session_id=SESSION)
    assert reverify["error"] is None, reverify
    _git(tmp_path, "add", DECLARED, OUTSIDE)
    _git(tmp_path, "commit", "-q", "-m", "run")
    return tmp_path, base, _git(tmp_path, "rev-parse", "HEAD"), reverify


def test_reverify_lists_the_outside_fix_and_keeps_the_declared_edit_apart(run_shape):
    _root, _base, _head, reverify = run_shape

    assert reverify["outside_declared"] == [OUTSIDE]
    assert reverify["edited"] == [DECLARED]
    assert reverify["created"] == []
    assert OUTSIDE not in reverify["foreign"]


def test_commit_set_carries_the_outside_fix_and_the_old_key_set_dropped_it(run_shape):
    root, _base, _head, reverify = run_shape
    writes = {DECLARED: "a1", OUTSIDE: "a1"}
    snapshot = SimpleNamespace(changed={DECLARED, OUTSIDE}, ignored_among=lambda _paths: [])

    def verdict(own_files):
        return tcr._review_edit_verdict(root, writes, snapshot, None, None, set(), own_files=own_files)

    recorded = tcr._review_recorded_edits({"reverify": reverify})
    assert OUTSIDE in recorded
    assert OUTSIDE in verdict(recorded).land

    pre_fix_keys = frozenset(reverify["edited"]) | frozenset(reverify["created"])
    assert OUTSIDE not in verdict(pre_fix_keys).land


def test_an_unanswerable_write_record_lands_nothing_and_says_so(run_shape):
    root, *_ = run_shape
    got = tcr._review_edit_verdict(root, None, None, None, None, set())

    assert got.land == () and got.halt == {}
    assert got.unanswerable


def test_selection_includes_every_touched_and_stem_matched_test_ts(run_shape):
    root, base, head, _reverify = run_shape
    picked = select_tests(root, base, head)

    assert OUTSIDE in picked["tests"]
    assert DEFAULTS_TEST in picked["tests"]
    assert picked["fully_mapped"] is True


def test_a_source_diff_with_no_test_selects_nothing(run_shape):
    root, _base, head, _reverify = run_shape
    (root / ORPHAN).write_bytes(b"export const O = 2;\n")
    _git(root, "add", ORPHAN)
    _git(root, "commit", "-q", "-m", "orphan")

    picked = select_tests(root, head, "HEAD")

    assert picked["tests"] == []
    assert picked["fully_mapped"] is False


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


def _run_js(source: str) -> dict:
    proc = subprocess.run(
        [_node(), "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _reconcile(reverify: dict) -> dict:
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 1, "applied": 1, "sidecar_path": "rev/s1.md"}]
    prep = {"slices": [{"id": "s1", "files": [OUTSIDE]}]}
    return _run_js(
        f"let _reviewReused = false; let _reviewWave = {json.dumps(wave)}; "
        f"const _reviewPrep = {json.dumps(prep)}; let _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._WAVE_DIFF_RECONCILE_JS
        + "\nconsole.log(JSON.stringify(_reviewWave[0]));\n"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_wave_entry_for_the_outside_fix_stands_and_is_demoted_without_the_key(run_shape):
    _root, _base, _head, reverify = run_shape

    kept = _reconcile(reverify)
    assert kept["applied"] == 1 and "unapplied_reason" not in kept

    baseline_shape = {k: v for k, v in reverify.items() if k != "outside_declared"}
    demoted = _reconcile(baseline_shape)
    assert demoted["applied"] == 0
    assert demoted["unapplied_reason"].startswith("not in run diff")


def _tests_not_run(reverify: dict, manifest: dict) -> dict:
    return _run_js(
        f"let _halted = null; const _runId = 'r'; let _reviewReused = false; const _codeReviewOwed = true; "
        f"const _manifest = {json.dumps(manifest)}; let _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._TESTS_NOT_RUN_JS
        + "\nconsole.log(JSON.stringify({ halted: _halted, rv: _reviewReverify }));\n"
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_empty_selection_is_tests_not_run_and_a_real_selection_is_not(run_shape):
    root, base, head, reverify = run_shape
    selected = select_tests(root, base, head)["tests"]
    assert selected

    ran = _tests_not_run({**reverify, "status_after": None}, {"test_targets": selected})
    assert ran["halted"] is None and "tests_not_run" not in ran["rv"]

    (root / ORPHAN).write_bytes(b"export const O = 2;\n")
    _git(root, "add", ORPHAN)
    _git(root, "commit", "-q", "-m", "orphan")
    empty = select_tests(root, head, "HEAD")["tests"]
    assert empty == []

    skipped = _tests_not_run({**reverify, "status_after": None}, {"test_targets": empty})
    assert skipped["halted"]["halted"] == ask_compose.HALT_TESTS_NOT_RUN
    assert skipped["rv"]["tests_not_run"] is True
