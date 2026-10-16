"""commit_paths refuses a path whose blob or read bytes drifted from the caller's expectation."""
import hashlib
import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _write(repo, rel, text):
    (repo / rel).write_text(text, encoding="utf-8", newline="\n")


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _state(repo):
    gitdir = repo / ".git"
    return (
        _git(repo, "rev-parse", "HEAD").stdout,
        (gitdir / "index").read_bytes(),
        (gitdir / "refs/heads/work/z").read_bytes(),
    )


class _SpawnCounter:
    def __init__(self):
        self.argvs = []

    def __enter__(self):
        self._run, self._popen = subprocess.run, subprocess.Popen
        counter = self

        def run_spy(*a, **k):
            if a:
                counter.argvs.append(a[0])
            return counter._run(*a, **k)

        class PopenSpy(counter._popen):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **k):
                if a:
                    counter.argvs.append(a[0])
                super().__init__(*a, **k)

        subprocess.run, subprocess.Popen = run_spy, PopenSpy
        return self

    def __exit__(self, *exc):
        subprocess.run, subprocess.Popen = self._run, self._popen
        return False


def test_blob_match_lands(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "seed.txt", "edited\n")
    out = gcommit.commit_paths(
        repo, ["seed.txt"], "m", expected_blobs={"seed.txt": _blob(b"edited\n")}
    )
    assert _git(repo, "show", f"{out.sha}:seed.txt").stdout == "edited\n"


def test_blob_mismatch_refuses_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "seed.txt", "reviewed\n")
    want = _blob(b"reviewed\n")
    _write(repo, "seed.txt", "peer hunk\n")
    before = _state(repo)
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(repo, ["seed.txt"], "m", expected_blobs={"seed.txt": want})
    assert isinstance(ei.value, gcommit.CommitRefused)
    d = ei.value.drift
    assert d == (gcommit.BlobDrift("seed.txt", "blob", want, _blob(b"peer hunk\n")),)
    assert _state(repo) == before


def test_content_match_and_mismatch(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "seed.txt", "A\n")
    gcommit.commit_paths(
        repo, ["seed.txt"], "m", expected_content_sha256={"seed.txt": _sha256(b"A\n")}
    )
    _write(repo, "seed.txt", "B\n")
    before = _state(repo)
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(
            repo, ["seed.txt"], "m", expected_content_sha256={"seed.txt": _sha256(b"A\n")}
        )
    assert ei.value.drift[0].basis == "content-sha256"
    assert ei.value.drift[0].actual == _sha256(b"B\n")
    assert _state(repo) == before


def _stage_then_diverge(repo):
    _write(repo, "seed.txt", "STAGED\n")
    _git(repo, "add", "--", "seed.txt")
    _write(repo, "seed.txt", "WORKTREE\n")


def test_prefer_staged_with_content_expectation_is_unverifiable(tmp_path):
    repo = _repo(tmp_path)
    _stage_then_diverge(repo)
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(
            repo, ["seed.txt"], "m", prefer_staged=["seed.txt"],
            expected_content_sha256={"seed.txt": _sha256(b"STAGED\n")},
        )
    assert ei.value.drift[0].actual is None


def test_prefer_staged_with_blob_expectation_compares_staged_blob(tmp_path):
    repo = _repo(tmp_path)
    _stage_then_diverge(repo)
    out = gcommit.commit_paths(
        repo, ["seed.txt"], "m", prefer_staged=["seed.txt"],
        expected_blobs={"seed.txt": _blob(b"STAGED\n")},
    )
    assert _git(repo, "show", f"{out.sha}:seed.txt").stdout == "STAGED\n"


def test_deliberate_stage_substitution_content_unverifiable_blob_compares_substituted(tmp_path):
    repo = _repo(tmp_path)
    _stage_then_diverge(repo)
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(
            repo, ["seed.txt"], "m", prefer_deliberate_stage=True,
            expected_content_sha256={"seed.txt": _sha256(b"WORKTREE\n")},
        )
    assert ei.value.drift[0].actual is None
    with pytest.raises(gcommit.BlobDriftRefused):
        gcommit.commit_paths(
            repo, ["seed.txt"], "m", prefer_deliberate_stage=True,
            expected_blobs={"seed.txt": _blob(b"WORKTREE\n")},
        )
    gcommit.commit_paths(
        repo, ["seed.txt"], "m", prefer_deliberate_stage=True,
        expected_blobs={"seed.txt": _blob(b"STAGED\n")},
    )


def test_supplied_blob_with_content_expectation_is_unverifiable(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "seed.txt", "edited\n")
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(
            repo, ["seed.txt"], "m", supplied_blobs={"seed.txt": _blob(b"edited\n")},
            expected_content_sha256={"seed.txt": _sha256(b"edited\n")},
        )
    assert ei.value.drift[0].actual is None


def test_crlf_pinned_fallback_path_with_blob_expectation(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, ".gitattributes", "*.cmd text eol=crlf\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    (repo / "a.cmd").write_bytes(b"one\r\ntwo\r\n")
    resolved = _blob(b"one\ntwo\n")

    def fallback(paths):
        return {p: resolved for p in paths}

    with pytest.raises(gcommit.BlobDriftRefused):
        gcommit.commit_paths(
            repo, ["a.cmd"], "m", blob_fallback=fallback,
            expected_blobs={"a.cmd": "0" * 40},
        )
    with pytest.raises(gcommit.BlobDriftRefused):
        gcommit.commit_paths(
            repo, ["a.cmd"], "m", blob_fallback=fallback,
            expected_content_sha256={"a.cmd": "0" * 64},
        )
    seen = []

    def fallback_real(paths):
        seen.append(list(paths))
        out = _git(repo, "hash-object", "-w", "--path=a.cmd", "a.cmd").stdout.strip()
        return {p: out for p in paths}

    out = gcommit.commit_paths(
        repo, ["a.cmd"], "m", blob_fallback=fallback_real,
        expected_content_sha256={"a.cmd": _sha256(b"one\r\ntwo\r\n")},
    )
    assert out.sha and seen == [["a.cmd"]]


def test_multi_path_drift_lists_all(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", "a\n")
    _write(repo, "b.txt", "b\n")
    _write(repo, "c.txt", "c\n")
    with pytest.raises(gcommit.BlobDriftRefused) as ei:
        gcommit.commit_paths(
            repo, ["a.txt", "b.txt", "c.txt"], "m",
            expected_blobs={"a.txt": "0" * 40, "b.txt": _blob(b"b\n"), "c.txt": "1" * 40},
        )
    assert {d.path for d in ei.value.drift} == {"a.txt", "c.txt"}


def test_key_outside_pathspec_refused(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", "a\n")
    with pytest.raises(gcommit.CommitRefused) as ei:
        gcommit.commit_paths(repo, ["a.txt"], "m", expected_blobs={"other.txt": "0" * 40})
    assert not isinstance(ei.value, gcommit.BlobDriftRefused)


def test_deleted_path_key_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.txt").unlink()
    _write(repo, "a.txt", "a\n")
    with pytest.raises(gcommit.CommitRefused) as ei:
        gcommit.commit_paths(
            repo, ["a.txt"], "m", deleted_paths=["seed.txt"],
            expected_blobs={"seed.txt": "0" * 40},
        )
    assert not isinstance(ei.value, gcommit.BlobDriftRefused)


@pytest.mark.parametrize(
    "kwarg,value",
    [
        ("expected_blobs", "0" * 39),
        ("expected_blobs", "A" * 40),
        ("expected_blobs", "g" * 40),
        ("expected_content_sha256", "0" * 40),
        ("expected_content_sha256", "F" * 64),
    ],
)
def test_malformed_hex_refused(tmp_path, kwarg, value):
    repo = _repo(tmp_path)
    _write(repo, "a.txt", "a\n")
    before = _state(repo)
    with pytest.raises(gcommit.CommitRefused) as ei:
        gcommit.commit_paths(repo, ["a.txt"], "m", **{kwarg: {"a.txt": value}})
    assert not isinstance(ei.value, gcommit.BlobDriftRefused)
    assert _state(repo) == before


def test_spawn_count_equal_with_and_without_expectations(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, ".gitattributes", "*.cmd text eol=crlf\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")

    def fallback(paths):
        subprocess.run(["git", "--version"], capture_output=True, **_NOWIN)
        return {p: _blob(b"x\n") for p in paths}

    def run(with_exp, tag):
        _write(repo, f"p{tag}.txt", f"p{tag}\n")
        (repo / f"q{tag}.cmd").write_bytes(b"x\r\n")
        kw = {}
        if with_exp:
            kw = dict(
                expected_blobs={f"p{tag}.txt": _blob(f"p{tag}\n".encode())},
                expected_content_sha256={f"p{tag}.txt": _sha256(f"p{tag}\n".encode())},
            )
        with _SpawnCounter() as c:
            gcommit.commit_paths(
                repo, [f"p{tag}.txt", f"q{tag}.cmd"], "m", blob_fallback=fallback, **kw
            )
        return len(c.argvs)

    assert run(False, "1") == run(True, "2")

    _write(repo, "z.txt", "z\n")
    with _SpawnCounter() as c0:
        gcommit.commit_paths(repo, ["z.txt"], "m")
    _write(repo, "y.txt", "y\n")
    with _SpawnCounter() as c1:
        gcommit.commit_paths(
            repo, ["y.txt"], "m", expected_blobs={"y.txt": _blob(b"y\n")},
            expected_content_sha256={"y.txt": _sha256(b"y\n")},
        )
    assert len(c0.argvs) == len(c1.argvs) == 0
