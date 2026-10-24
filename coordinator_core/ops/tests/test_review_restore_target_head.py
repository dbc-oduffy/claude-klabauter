"""A review restore or revert lands on HEAD's bytes, never the run base's: a peer commit after the
run base survives the revert, and the re-verify prompts name HEAD as the restore target."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.session.machinery_paths import share_dir

_REAL_RUN = subprocess.run
SESSION = "sess-own"
PREFIX = "prep-rt"
BASE_OP = b"def run():\n    return 1\n"
PEER_OP = b"def run():\n    return 1\n\n\ndef peer():\n    return 'p'\n"
TEST_PY = b"from pkg.op import run\n\n\ndef test_x():\n    assert run() == 1\n"


def _git(root: Path, *args: str) -> str:
    done = _REAL_RUN(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for rel, data in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _snapshot(root: Path, base: str, files: dict) -> None:
    snap = Path(share_dir(str(root), SESSION)) / f"{PREFIX}.pre-review"
    snap.mkdir(parents=True)
    shas = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    for rel, data in files.items():
        (snap / shas[rel]).write_bytes(data)
    manifest = {"base": base, "head": base, "recorded_at": "2026-01-01T00:00:00Z", "paths": list(files), "files": shas}
    (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _arm(monkeypatch, rows: list) -> None:
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: [])
    monkeypatch.setattr(rr, "declare_write", lambda _p: None)
    monkeypatch.setattr(review_findings_ledger, "read_ledger", lambda _p: (True, rows))


def test_revert_after_a_peer_commit_keeps_head_bytes_not_run_base_bytes(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"pkg/__init__.py": b"", "pkg/op.py": BASE_OP, "tests/test_op.py": TEST_PY}, "base")
    _snapshot(tmp_path, base, {"pkg/op.py": BASE_OP})
    _commit(tmp_path, {"pkg/op.py": PEER_OP}, "peer commit after the run base")
    (tmp_path / "pkg/op.py").write_bytes(PEER_OP.replace(b"return 1", b"return 9"))
    row = {"id": "F1", "file": "pkg/op.py", "before": "    return 1\n", "after": "    return 9\n"}
    _arm(monkeypatch, [row])
    out = rr.review_reverify(
        tmp_path, base=base, slice_prefix=PREFIX, session_id=SESSION,
        failing=["tests/test_op.py::test_x"], sidecars=["review.md"],
    )
    assert out["error"] is None
    assert [e["path"] for e in out["reverted"]] == ["pkg/op.py"]
    assert (tmp_path / "pkg/op.py").read_bytes() == PEER_OP == _git(tmp_path, "show", "HEAD:pkg/op.py").encode() + b"\n"


def test_unedited_path_a_peer_commit_changed_is_not_reported_edited_and_stays_head(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"pkg/op.py": BASE_OP}, "base")
    _snapshot(tmp_path, base, {"pkg/op.py": BASE_OP})
    _commit(tmp_path, {"pkg/op.py": PEER_OP}, "peer commit after the run base")
    _arm(monkeypatch, [])
    out = rr.review_reverify(tmp_path, base=base, slice_prefix=PREFIX, session_id=SESSION)
    assert out["error"] is None and out["edited"] == []
    assert (tmp_path / "pkg/op.py").read_bytes() == PEER_OP


def test_reverify_prompts_name_head_as_the_restore_target():
    kw = dict(
        base_ref="abc123", prep_slice_id="s", repo_root=None, plan_path="p.md", run_base_sha="abc123",
        prompt_head="head", host_degraded=False, sc=lambda call, *_a, **_k: call,
    )
    for extra in ({}, {"group_prefixes": ["g1", "g2"]}):
        block = er._reverify_phase(**kw, **extra)[1]
        assert er._js_string_literal(er.RESTORE_TARGET_CLAUSE)[1:-1] in block
