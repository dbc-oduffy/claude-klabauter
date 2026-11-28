import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.group_em import run_delta

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ENGINE = str(Path(__file__).resolve().parents[3])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, creationflags=_FLAGS,
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_bytes(b"one\ntwo\n")
    (root / "blob.bin").write_bytes(bytes(range(256)))
    (root / "peer.txt").write_bytes(b"peer\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    (root / "tracked.txt").write_bytes(b"one\nTWO\nthree\n")
    (root / "blob.bin").write_bytes(bytes(reversed(range(256))))
    (root / "peer.txt").write_bytes(b"peer dirt\n")
    (root / "new dir").mkdir()
    (root / "new dir" / "fresh.py").write_bytes(b"x = 1\r\n")
    declared = ["tracked.txt", "blob.bin", "new dir/fresh.py"]
    monkeypatch.setattr("coordinator_core.ops.test_leg_run._declared_overlay", lambda r, run_id: (list(declared), {}))
    return root


def _run_script(script: str, repo: Path, scratch: Path) -> Path:
    env = {**os.environ, "MACHINE_LOCAL_PATHS_SCRATCH_ROOT": str(scratch), "PYTHONPATH": _ENGINE}
    done = subprocess.run(
        [sys.executable, "-c", script, str(repo)], capture_output=True, env=env, creationflags=_FLAGS,
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    import json

    return Path(json.loads(done.stdout.decode().strip().splitlines()[-1])["export_dir"])


def test_apply_reproduces_modified_binary_and_untracked_bytes(repo: Path, tmp_path: Path) -> None:
    b = run_delta.bundle(repo, "run-1")
    assert "tracked.txt" in b["patch"].decode() and "peer.txt" not in b["patch"].decode()
    assert set(b["files"]) == {"new dir/fresh.py"}
    out = _run_script(run_delta.apply_script(b), repo, tmp_path / "scratch")
    assert (out / "tracked.txt").read_bytes() == b"one\nTWO\nthree\n"
    assert (out / "blob.bin").read_bytes() == bytes(reversed(range(256)))
    assert (out / "new dir" / "fresh.py").read_bytes() == b"x = 1\r\n"
    assert (out / "peer.txt").read_bytes() == b"peer\n"


def test_digest_is_stable_and_tracks_content(repo: Path) -> None:
    first = run_delta.bundle(repo, "run-1")
    assert run_delta.bundle(repo, "run-1")["digest"] == first["digest"]
    (repo / "new dir" / "fresh.py").write_bytes(b"x = 2\n")
    assert run_delta.bundle(repo, "run-1")["digest"] != first["digest"]


def test_override_path_ships_as_file_bytes_not_patch(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.test_leg_run._declared_overlay",
        lambda r, run_id: (["tracked.txt", "peer.txt"], {"peer.txt": b"peer own hunk\n"}),
    )
    b = run_delta.bundle(repo, "run-1")
    assert b["files"] == {"peer.txt": b"peer own hunk\n"}
    assert "peer.txt" not in b["patch"].decode()


def test_unsafe_overlay_paths_are_dropped(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.test_leg_run._declared_overlay",
        lambda r, run_id: (["../escape.txt", "tracked.txt"], {"/abs.txt": b"x"}),
    )
    b = run_delta.bundle(repo, "run-1")
    assert b["files"] == {} and "escape" not in b["patch"].decode()


def test_missing_scratch_root_is_a_typed_refusal_naming_the_key(repo: Path) -> None:
    ns: dict = {"__name__": "run_delta_apply"}
    exec(run_delta.apply_script(run_delta.bundle(repo, "run-1")), ns)
    with pytest.raises(SystemExit, match="settings-key-missing: paths.scratch_root"):
        ns["main"](repo, get=lambda key: None)
