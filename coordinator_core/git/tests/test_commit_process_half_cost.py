"""The commit landing loop resolves `core.autocrlf` once per call, not once per landed path."""

from pathlib import Path

from coordinator_core.git import commit

N_PATHS = 40


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    for i in range(N_PATHS):
        (tmp_path / f"f{i}.txt").write_bytes(b"a\r\nb\r\n")
    return tmp_path


def _count_resolutions(monkeypatch, answer: bool) -> list:
    calls: list = []

    def fake(root):
        calls.append(root)
        return answer

    monkeypatch.setattr(commit, "_repo_autocrlf_true", fake)
    return calls


def test_checkin_sizes_resolves_autocrlf_once_for_many_crlf_paths(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_resolutions(monkeypatch, True)
    paths = [f"f{i}.txt" for i in range(N_PATHS)]
    sizes = commit.checkin_sizes(root, paths, blob_fallback=lambda p: {})
    assert len(calls) == 1
    assert all(sizes[p] == len(b"a\nb\n") for p in paths)


def test_autocrlf_off_still_refuses_cr_bytes_with_one_resolution(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_resolutions(monkeypatch, False)
    refused: list = []

    def fallback(ps):
        refused.extend(ps)
        raise RuntimeError("fallback reached")

    try:
        commit.checkin_sizes(root, ["f0.txt", "f1.txt"], blob_fallback=fallback)
    except RuntimeError:
        pass
    assert refused == ["f0.txt", "f1.txt"]
    assert len(calls) == 1


def test_unhoisted_checkin_bytes_still_resolves_per_call(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_resolutions(monkeypatch, True)
    for i in range(3):
        commit._checkin_bytes(root, f"f{i}.txt", b"a\r\n")
    assert len(calls) == 3


def _git(root: Path, *args: str) -> None:
    import os
    import subprocess

    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example",
    )
    subprocess.run(
        ["git", "-C", str(root), *args], env=env, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_commit_paths_reads_the_config_stack_once_for_many_crlf_paths(tmp_path, monkeypatch):
    from coordinator_core.git import content_hash

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "true")
    names = [f"f{i}.txt" for i in range(N_PATHS)]
    for name in names:
        (root / name).write_bytes(b"a\nb\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    for name in names:
        (root / name).write_bytes(b"a\r\nb\r\nc\r\n")

    reads: list = []
    orig = content_hash._repo_autocrlf_true_uncached

    def counting(r):
        reads.append(r)
        return orig(r)

    monkeypatch.setattr(content_hash, "_repo_autocrlf_true_uncached", counting)
    commit.commit_paths(root, names, "autocrlf once")
    assert len(reads) == 1, (len(reads), N_PATHS)
