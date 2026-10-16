"""`ceremony.commit_v2` threads `expected_blobs` / `expected_content_sha256` to `commit_paths`
and returns a structured `blob_drift` refusal. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _blob(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _call(repo: Path, **params) -> dict:
    params.setdefault("message", "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111")
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_blob_drift_refuses_structured_and_head_unchanged(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    before = _head(repo)
    reply = _call(repo, paths=["a.py"], expected_blobs={"a.py": _blob("reviewed\n")})
    assert reply["committed"] is False and reply["sha"] is None
    assert reply["refused"] == "blob-drift"
    assert reply["blob_drift"] == [{
        "path": "a.py", "basis": "blob",
        "expected": _blob("reviewed\n"), "actual": _blob("mine\n"),
    }]
    assert _head(repo) == before


def test_blob_match_lands(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    reply = _call(repo, paths=["a.py"], expected_blobs={"a.py": _blob("mine\n")})
    assert reply["committed"] is True
    assert "blob_drift" not in reply and "refused" not in reply


def test_content_sha256_drift_and_match(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "B\n")
    before = _head(repo)
    reply = _call(repo, paths=["a.py"], expected_content_sha256={"a.py": _sha256("A\n")})
    assert reply["refused"] == "blob-drift"
    assert reply["blob_drift"][0]["basis"] == "content-sha256"
    assert reply["blob_drift"][0]["actual"] == _sha256("B\n")
    assert _head(repo) == before
    ok = _call(repo, paths=["a.py"], expected_content_sha256={"a.py": _sha256("B\n")})
    assert ok["committed"] is True


def test_backslash_keys_are_normalised(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/a.py", "mine\n")
    reply = _call(repo, paths=["d/a.py"], expected_blobs={"d\\a.py": _blob("mine\n")})
    assert reply["committed"] is True


@pytest.mark.parametrize("name", ["expected_blobs", "expected_content_sha256"])
@pytest.mark.parametrize("bad", ["x", ["a.py"], {"a.py": 1}, {1: "x"}])
def test_type_errors_name_the_param(tmp_path, name, bad):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    reply = _call(repo, paths=["a.py"], **{name: bad})
    assert reply["committed"] is False
    assert f"params.{name}" in reply["error"]
    assert "blob_drift" not in reply


def test_no_params_reply_has_no_drift_keys(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    reply = _call(repo, paths=["a.py"])
    assert reply["committed"] is True
    assert "blob_drift" not in reply and "refused" not in reply


def test_mixed_halves_each_get_only_their_keys(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    _write(repo, "state/n.md", "note\n")
    reply = _call(
        repo,
        paths=["a.py", "state/n.md"],
        expected_blobs={"a.py": _blob("mine\n"), "state/n.md": _blob("note\n")},
    )
    assert reply["committed"] is True, reply
    assert reply.get("process_sha")


def test_mixed_drift_in_product_half_refuses_everything(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    _write(repo, "state/n.md", "note\n")
    before = _head(repo)
    reply = _call(
        repo,
        paths=["a.py", "state/n.md"],
        expected_blobs={"a.py": _blob("other\n"), "state/n.md": _blob("note\n")},
    )
    assert reply["refused"] == "blob-drift"
    assert [d["path"] for d in reply["blob_drift"]] == ["a.py"]
    assert _head(repo) == before


def test_mixed_drift_in_process_half_keeps_the_structured_refusal(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    _write(repo, "state/n.md", "note\n")
    reply = _call(
        repo,
        paths=["a.py", "state/n.md"],
        expected_blobs={"a.py": _blob("mine\n"), "state/n.md": _blob("other\n")},
    )
    assert reply["committed"] is False
    assert reply["refused"] == "blob-drift"
    assert [d["path"] for d in reply["blob_drift"]] == ["state/n.md"]
    assert reply["landed_sha"] == _head(repo)


def test_key_outside_pathspec_is_refused_in_a_mixed_call(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.py", "mine\n")
    _write(repo, "state/n.md", "note\n")
    before = _head(repo)
    reply = _call(
        repo, paths=["a.py", "state/n.md"], expected_blobs={"zzz.py": _blob("x\n")}
    )
    assert reply["committed"] is False
    assert _head(repo) == before
