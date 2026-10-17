"""Call-count pins for head_sha's per-call memo (HEAD_NAMESPACE, fresh= bypass)."""

from pathlib import Path

from coordinator_core.git import git_state
from coordinator_core.git.call_io_scope import HEAD_NAMESPACE, call_io_scope, invalidate


def _repo(tmp_path: Path, sha: str) -> Path:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    return tmp_path


def _count_reads(monkeypatch):
    calls = []
    real = Path.read_text

    def spy(self, *a, **k):
        calls.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    return calls


A, B = "a" * 40, "b" * 40


def test_scope_reads_head_and_ref_once(tmp_path, monkeypatch):
    repo = _repo(tmp_path, A)
    calls = _count_reads(monkeypatch)
    with call_io_scope():
        assert [git_state.head_sha(repo) for _ in range(5)] == [A] * 5
    assert calls == ["HEAD", "main"]


def test_invalidate_observes_peer_write(tmp_path):
    repo = _repo(tmp_path, A)
    with call_io_scope():
        assert git_state.head_sha(repo) == A
        (repo / ".git" / "refs" / "heads" / "main").write_text(B, encoding="utf-8")
        assert git_state.head_sha(repo) == A
        invalidate(HEAD_NAMESPACE)
        assert git_state.head_sha(repo) == B


def test_fresh_always_reads_and_never_populates(tmp_path, monkeypatch):
    repo = _repo(tmp_path, A)
    calls = _count_reads(monkeypatch)
    with call_io_scope():
        git_state.head_sha(repo, fresh=True)
        git_state.head_sha(repo, fresh=True)
        assert calls == ["HEAD", "main"] * 2
        git_state.head_sha(repo)
        assert len(calls) == 6
        git_state.head_sha(repo, fresh=True)
        assert len(calls) == 8


def test_outside_scope_every_call_reads(tmp_path, monkeypatch):
    repo = _repo(tmp_path, A)
    calls = _count_reads(monkeypatch)
    for _ in range(3):
        git_state.head_sha(repo)
    assert calls == ["HEAD", "main"] * 3
