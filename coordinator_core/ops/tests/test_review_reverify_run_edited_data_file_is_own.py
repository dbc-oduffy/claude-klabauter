"""A failing node whose test reads a data file the run edited is the run's own red, never `foreign`,
`foreign_red` or `sibling_owned`, driven through the registered ``review.reverify`` handler, even when a
live peer run also declares that file. A node naming only a peer's path stays foreign; one naming none of
the run's paths stays foreign_red."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.session.machinery_paths import share_dir

SESSION = "sess-data"
PREFIX = "prep-data"
DATA = "setup/publish-targets.portable"
PEER_DATA = "setup/peer-only.portable"
READER = "coordinator/bin/tests/test_reads_portable.py"
PEER_READER = "coordinator/bin/tests/test_reads_peer.py"
OTHER = "tests/test_reads_nothing.py"
KEPT = "pkg/mod.py"
READER_SRC = (
    "from pathlib import Path\n\n"
    '_PORTABLE_PATH = Path(__file__).parents[3] / "setup" / "publish-targets.portable"\n\n\n'
    "def test_rows():\n    assert _PORTABLE_PATH.read_text().count('row') == 1\n"
)
PEER_READER_SRC = READER_SRC.replace("publish-targets.portable", "peer-only.portable")
OTHER_SRC = "def test_n():\n    assert False\n"
BEFORE, AFTER = "row-a\n", "row-a\nrow-b\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")


def _manifest(root: Path) -> Path:
    return Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review" / "manifest.json"


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    files = {
        DATA: BEFORE, READER: READER_SRC, OTHER: OTHER_SRC, KEPT: "V = 1\n",
        PEER_DATA: BEFORE, PEER_READER: PEER_READER_SRC,
    }
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    snap = _manifest(root).parent
    snap.mkdir(parents=True)
    run_files = {rel: text for rel, text in files.items() if rel not in (PEER_DATA, PEER_READER)}
    for text in run_files.values():
        (snap / _sha(text.encode())).write_bytes(text.encode())
    _manifest(root).write_text(json.dumps({
        "base": base, "recorded_at": "2026-11-17T00:00:00Z", "paths": list(run_files),
        "files": {rel: _sha(text.encode()) for rel, text in run_files.items()},
    }), encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    return root, base


def _revert(root: Path, base: str, node: str, at_base=None) -> dict:
    return rr._handler(
        {
            "base": base, "slice_prefix": PREFIX, "session_id": SESSION, "failing": [node],
            "run_id": "me", "failing_output": {node: "AssertionError: assert 2 == 1"},
            "failing_at_base": at_base,
        },
        repo_root=root,
    )


@pytest.mark.parametrize("committed", [False, True], ids=["dirty-edit", "checkpoint-committed-edit"])
def test_a_node_reading_a_run_edited_data_file_is_own(world, committed: bool) -> None:
    root, base = world
    _write(root, DATA, AFTER)
    if committed:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "checkpoint")
    node = f"{READER}::test_rows"
    out = _revert(root, base, node, at_base=[])
    assert out["error"] is None
    assert node not in out["foreign"]
    assert node not in out["foreign_red"]
    assert node not in out["sibling_owned"]
    assert node in out["unattributed"] or any(node in r["tests"] for r in out["reverted"] + out["kept"])


def test_a_peer_declaring_the_run_edited_data_file_does_not_make_the_node_foreign(world, monkeypatch) -> None:
    root, base = world
    _write(root, DATA, AFTER)
    data = json.loads(_manifest(root).read_text(encoding="utf-8"))
    data["files"][DATA] = _sha(AFTER.encode())
    _manifest(root).write_text(json.dumps(data), encoding="utf-8")
    (_manifest(root).parent / _sha(AFTER.encode())).write_bytes(AFTER.encode())
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {DATA: "peer-run"})
    node = f"{READER}::test_rows"
    out = _revert(root, base, node)
    assert out["error"] is None
    assert node not in out["foreign"] and node not in out["foreign_red"]
    assert node in out["unattributed"]


def test_a_node_naming_only_a_peer_declared_path_stays_foreign(world, monkeypatch) -> None:
    root, base = world
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {PEER_DATA: "peer-run"})
    node = f"{PEER_READER}::test_rows"
    out = _revert(root, base, node)
    assert node in out["foreign"]


def test_a_node_naming_none_of_the_run_paths_stays_foreign_red(world) -> None:
    root, base = world
    _write(root, DATA, AFTER)
    data = json.loads(_manifest(root).read_text(encoding="utf-8"))
    for rel in (OTHER, READER):
        data["paths"].remove(rel)
        del data["files"][rel]
    _manifest(root).write_text(json.dumps(data), encoding="utf-8")
    node = f"{OTHER}::test_n"
    out = _revert(root, base, node)
    assert node in out["foreign_red"]
