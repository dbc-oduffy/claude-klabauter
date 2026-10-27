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
