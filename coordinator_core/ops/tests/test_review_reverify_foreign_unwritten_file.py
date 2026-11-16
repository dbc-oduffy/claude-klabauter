"""`review.reverify` classes a red in a test file the run never wrote, whose failure is not reachable from
the run diff (it reaches no written path through imports or literals and its failure text names none), as
`foreign_red`, never a blocker, whether or not the file is dirty; a reachable one, or one green at the run
base, stays blocking."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import land_run, review_edit_source, review_edits
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-own"
PREFIX = "prep-t"
BASE = "abc1234"
WRITTEN = "pkg/mod.py"
PEER_TEST = "tests/test_likeness_pipeline_input.py"
NODE = f"{PEER_TEST}::test_input_shape"
PLAIN_TEXT = f"{PEER_TEST}:390: in test_input_shape\n    assert payload == expected\nAssertionError: 1 != 2\n"
UNRELATED = "def test_input_shape():\n    assert 1 == 2\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A run whose write set is the single file pkg/mod.py, edited since the snapshot."""
    original = b"VALUE = 1\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / WRITTEN).write_bytes(b"VALUE = 2\n")
    snap = Path(share_dir(str(tmp_path), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    (snap / _sha(original)).write_bytes(original)
    (snap / "manifest.json").write_text(
        json.dumps({"base": BASE, "recorded_at": "2026-10-19T00:00:00Z", "paths": [WRITTEN],
                    "files": {WRITTEN: _sha(original)}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(review_edit_source, "session_agent_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "_head_readable", lambda _r: True)
    return tmp_path


def _peer_test(root: Path, monkeypatch: pytest.MonkeyPatch, text: str, *, head: str) -> None:
    """A peer's test file; ``head`` is ``dirty`` (differs from HEAD), ``untracked`` (HEAD lacks it) or
    ``clean`` (equals HEAD). The written file is always dirty."""
    (root / "tests").mkdir(exist_ok=True)
    (root / PEER_TEST).write_bytes(text.encode("utf-8"))

    def head_blobs(r, paths):
        return {
            p: (0o100644, "0" * 40 if p == WRITTEN or (p == PEER_TEST and head == "dirty") else _blob_sha((r / p).read_bytes()))
            for p in paths if p != PEER_TEST or head != "untracked"
        }

    monkeypatch.setattr(rr.git_state, "head_blobs", head_blobs)


def _reverify(root: Path, output: str, **kw) -> dict:
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[NODE],
        run_id="me", failing_output={NODE: output}, **kw,
    )


@pytest.mark.parametrize("head", ["dirty", "untracked", "clean"])
def test_unwritten_unreachable_red_is_foreign_red_and_does_not_block(
    run, monkeypatch: pytest.MonkeyPatch, head: str
) -> None:
    _peer_test(run, monkeypatch, UNRELATED, head=head)
    out = _reverify(run, PLAIN_TEXT)
    assert out["foreign_red"] == {NODE: [PEER_TEST]}
    assert out["unattributed"] == []
    assert from_reverify({**out, "failing": [NODE]}) == []


def test_red_green_at_the_run_base_stays_the_runs_own(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _peer_test(run, monkeypatch, UNRELATED, head="clean")
    out = _reverify(run, PLAIN_TEXT, failing_at_base=[])
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_unwritten_red_importing_a_written_module_stays_blocking(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _peer_test(run, monkeypatch, "from pkg.mod import VALUE\n\n\ndef test_input_shape():\n    assert VALUE == 1\n", head="clean")
    out = _reverify(run, PLAIN_TEXT)
    assert out["foreign_red"] == {}
    assert [(k["path"], k["tests"]) for k in out["kept"]] == [(WRITTEN, [NODE])]


def test_unwritten_red_reaching_a_written_module_through_another_import_stays_blocking(
    run, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run / "pkg/__init__.py").write_bytes(b"")
    (run / "helper.py").write_bytes(b"from pkg import mod\n")
    _peer_test(run, monkeypatch, "import helper\n\n\ndef test_input_shape():\n    assert helper\n", head="clean")
    out = _reverify(run, PLAIN_TEXT)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_unwritten_red_whose_literal_names_a_written_path_stays_blocking(run, monkeypatch: pytest.MonkeyPatch) -> None:
    _peer_test(run, monkeypatch, f'P = "{WRITTEN}"\n\n\ndef test_input_shape():\n    assert P\n', head="clean")
    out = _reverify(run, PLAIN_TEXT)
    assert out["foreign_red"] == {}


@pytest.mark.parametrize("head", ["dirty", "clean"])
def test_unwritten_red_naming_an_own_path_in_its_failure_stays_blocking(
    run, monkeypatch: pytest.MonkeyPatch, head: str
) -> None:
    _peer_test(run, monkeypatch, UNRELATED, head=head)
    out = _reverify(run, PLAIN_TEXT + f"{WRITTEN}:1: in <module>\n")
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]
