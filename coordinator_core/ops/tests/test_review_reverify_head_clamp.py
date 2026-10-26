"""review.reverify in a real git repo: a path a commit moved since the snapshot is baselined at HEAD,
and a revert that would strand a committed caller is refused whole."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edit_source, review_edits
from coordinator_core.session.machinery_paths import share_dir

_REAL_RUN = subprocess.run
SESSION = "sess-own"
PREFIX = "prep-hc"

BASE_FILES = {
    "pkg/__init__.py": b"",
    "pkg/reply_sidecar.py": b"def old():\n    return 1\n",
    "pkg/op.py": b"def run():\n    return 1\n",
    "tests/test_rs.py": b"from pkg.reply_sidecar import old\n\n\ndef test_x():\n    assert old() == 1\n",
}
CHECKPOINT_RS = b"def old():\n    return 1\n\n\ndef ensure_twin_token():\n    return 't'\n"
CHECKPOINT_OP = (
    b"def run():\n    from pkg import reply_sidecar\n\n    return reply_sidecar.ensure_twin_token()\n"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Repo:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.declared: list = []
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        done = _REAL_RUN(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=self.root, capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.stdout.strip()

    def write(self, files: dict) -> None:
        for rel, data in files.items():
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def snapshot(self, base: str, head, files: dict, recorded_at: str = "2026-01-01T00:00:00Z") -> None:
        snap = Path(share_dir(str(self.root), SESSION)) / f"{PREFIX}.pre-review"
        snap.mkdir(parents=True)
        for data in files.values():
            (snap / _sha(data)).write_bytes(data)
        manifest = {
            "base": base,
            "recorded_at": recorded_at,
            "paths": list(files),
            "files": {rel: _sha(data) for rel, data in files.items()},
        }
        if head is not None:
            manifest["head"] = head
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.snap = snap

    def arm(self, writes: dict | None = None) -> None:
        """Install the no-spawn trap after all git setup."""
        self.monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: writes or {})
        self.monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: [])
        self.monkeypatch.setattr(rr, "declare_write", lambda p: self.declared.append(Path(p).as_posix()))

        def boom(*_a, **_k):
            raise AssertionError("review.reverify spawned a process")

        self.monkeypatch.setattr(subprocess, "Popen", boom)

    def members(self, agent: str, *paths: str, record: bool = True) -> None:
        """Per-run membership record: ``agent`` wrote ``paths`` plus a path named for this run's slice."""
        by_agent = {agent: {*paths, f".coordinator-local/x/{PREFIX}/note.md"}} if record else {}
        self.monkeypatch.setattr(
            review_edit_source, "session_agent_writes", lambda *_a, **_k: by_agent
        )

    def list(self, base: str) -> dict:
        return rr.review_reverify(self.root, base=base, slice_prefix=PREFIX, session_id=SESSION)

    def revert(self, base: str, failing: list, rows: list | None = None) -> dict:
        """Revert with a findings sidecar carrying ``rows`` (applied ledger rows)."""
        self.monkeypatch.setattr(
            review_findings_ledger, "read_ledger", lambda _p: (True, rows or [])
        )
        return rr.review_reverify(
            self.root, base=base, slice_prefix=PREFIX, session_id=SESSION, failing=failing,
            sidecars=["review.md"] if rows else None,
        )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


def _checkpointed(repo: Repo, with_head: bool) -> str:
    """Base commit B, snapshot at B, checkpoint K moving reply_sidecar.py and op.py. Returns B."""
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base if with_head else None, BASE_FILES)
    repo.write({"pkg/reply_sidecar.py": CHECKPOINT_RS, "pkg/op.py": CHECKPOINT_OP})
    repo.commit("checkpoint")
    return base


@pytest.mark.parametrize("with_head", [True, False], ids=["head-anchor", "legacy-base-anchor"])
def test_incident_replay_inverts_the_finding_and_keeps_checkpoint_bytes(repo, with_head):
    base = _checkpointed(repo, with_head)
    repo.write({"pkg/reply_sidecar.py": CHECKPOINT_RS.replace(b"    return 1\n", b"    return 9\n")})
    repo.arm()
    row = {"id": "F1", "file": "pkg/reply_sidecar.py", "before": "    return 1\n", "after": "    return 9\n"}
    out = repo.revert(base, ["tests/test_rs.py::test_x"], [row])
    assert out["error"] is None
    assert [(e["path"], e["action"], e["source"]) for e in out["reverted"]] == [
        ("pkg/reply_sidecar.py", "reverted-findings", "findings")
    ]
    assert (repo.root / "pkg/reply_sidecar.py").read_bytes() == CHECKPOINT_RS
    assert (repo.root / "pkg/op.py").read_bytes() == CHECKPOINT_OP


def test_uncommitted_snapshot_work_survives_the_revert(repo):
    repo.write(BASE_FILES)
    base = repo.commit("base")
    work = b"def old():\n    return 2\n"
    repo.write({"pkg/reply_sidecar.py": work})
    repo.snapshot(base, base, {**BASE_FILES, "pkg/reply_sidecar.py": work})
    repo.write({"pkg/reply_sidecar.py": b"def old():\n    return 3\n"})
    repo.arm()
    row = {"id": "F1", "file": "pkg/reply_sidecar.py", "before": "    return 2\n", "after": "    return 3\n"}
    out = repo.revert(base, ["tests/test_rs.py::test_x"], [row])
    assert out["error"] is None
    assert [(e["path"], e["source"]) for e in out["reverted"]] == [("pkg/reply_sidecar.py", "findings")]
    assert (repo.root / "pkg/reply_sidecar.py").read_bytes() == work


def test_created_path_a_commit_tracks_is_restored_to_head_never_unlinked(repo):
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base, BASE_FILES)
    committed = b"def test_n():\n    assert True\n"
    repo.write({"tests/test_new.py": committed})
    repo.commit("checkpoint adds a test")
    repo.write({"tests/test_new.py": b"def test_n():\n    assert False\n"})
    repo.arm({"tests/test_new.py": "a1"})
    repo.members("a1", "tests/test_new.py")
    out = repo.revert(base, ["tests/test_new.py::test_n"])
    assert out["error"] is None
    assert [(e["path"], e["action"], e["source"]) for e in out["reverted"]] == [
        ("tests/test_new.py", "restored", "head")
    ]
    assert (repo.root / "tests/test_new.py").read_bytes() == committed


def test_created_untracked_path_is_still_removed(repo):
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base, BASE_FILES)
    repo.write({"tests/test_new.py": b"def test_n():\n    assert False\n"})
    repo.arm({"tests/test_new.py": "a1"})
    repo.members("a1", "tests/test_new.py")
    out = repo.revert(base, ["tests/test_new.py::test_n"])
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [("tests/test_new.py", "removed")]
    assert not (repo.root / "tests/test_new.py").exists()


@pytest.mark.parametrize("record", [False, True], ids=["unreadable-record", "sibling-agent"])
def test_unattributable_created_path_is_left_in_place(repo, record):
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base, BASE_FILES)
    body = b"def test_n():\n    assert False\n"
    repo.write({"tests/test_new.py": body})
    repo.arm({"tests/test_new.py": "a1"})
    if record:
        repo.monkeypatch.setattr(
            review_edit_source, "session_agent_writes",
            lambda *_a, **_k: {"a1": {"tests/test_new.py"}, "a2": {f"x/{PREFIX}/note.md"}},
        )
    else:
        repo.members("a1", record=False)
    out = repo.revert(base, ["tests/test_new.py::test_n"])
    assert out["reverted"] == []
    assert (repo.root / "tests/test_new.py").read_bytes() == body


def test_revert_that_strands_a_committed_caller_is_refused_whole(repo):
    files = {
        "pkg/__init__.py": b"",
        "pkg/mod.py": b"S = 1\n",
        "pkg/caller.py": b"from pkg.mod import S\n\n\ndef use():\n    return S\n",
        "tests/test_mod.py": b"from pkg.mod import S\n\n\ndef test_s():\n    assert S\n",
    }
    repo.write(files)
    base = repo.commit("base")
    lacking = {**files, "pkg/mod.py": b"T = 1\n"}
    repo.snapshot(base, base, lacking)
    review_edit = b"S = 1\nT = 2\n"
    repo.write({"pkg/mod.py": review_edit})
    repo.arm()
    row = {"id": "F1", "file": "pkg/mod.py", "before": "T = 1\n", "after": review_edit.decode()}
    out = repo.revert(base, ["tests/test_mod.py::test_s"], [row])
    assert out["error"] is not None
    assert "pkg/caller.py" in out["error"] and "pkg.mod.S" in out["error"]
    assert out["reverted"] == []
    assert (repo.root / "pkg/mod.py").read_bytes() == review_edit
    assert not (repo.snap / "reverify.json").exists()
    assert repo.declared == []


def test_list_mode_does_not_report_a_checkpoint_moved_path_as_edited(repo):
    base = _checkpointed(repo, True)
    repo.arm()
    out = repo.list(base)
    assert out["error"] is None
    assert out["edited"] == []
    repo.write({"pkg/op.py": b"def run():\n    return 0\n"})
    assert repo.list(base)["edited"] == ["pkg/op.py"]


def test_unreadable_anchor_fails_closed_for_revert_and_degrades_list(repo):
    base = _checkpointed(repo, True)
    manifest = json.loads((repo.snap / "manifest.json").read_text(encoding="utf-8"))
    manifest["head"] = "0" * 40
    (repo.snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    edit = b"def old():\n    return 9\n"
    repo.write({"pkg/reply_sidecar.py": edit})
    repo.arm()
    out = repo.revert(base, ["tests/test_rs.py::test_x"])
    assert out["error"] is not None
    assert out["reverted"] == []
    assert (repo.root / "pkg/reply_sidecar.py").read_bytes() == edit
    listed = repo.list(base)
    assert listed["error"] is None and listed["head_unreadable"] is True
    assert listed["edited"] == ["pkg/op.py", "pkg/reply_sidecar.py"]
