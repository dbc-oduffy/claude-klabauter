
import hashlib
import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git import rollback_check
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_sha as read_head_sha

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _blob_sha(content: bytes) -> str:
    header = b"blob " + str(len(content)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + content).hexdigest()


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


def _common(repo):
    return resolve_git_common_dir(repo)


def _commit(repo, path, content, msg):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content, encoding="utf-8", newline="\n")
    outcome = gcommit.commit_paths(repo, [path], msg)
    return outcome.sha


def test_depth_2_single_path_rollback_refused(tmp_path):
    repo = _repo(tmp_path)
    v0_commit = _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")
    head = _commit(repo, "p.txt", "v2\n", "v2")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"p.txt": _blob_sha(b"v0\n")}, window=500
    )

    assert findings == [rollback_check.RollbackFinding("p.txt", 2, v0_commit)]
    assert rollback_check.refusal(findings) is True


def test_lone_depth_1_revert_of_the_latest_change_allowed(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    head = _commit(repo, "p.txt", "v1\n", "v1")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"p.txt": _blob_sha(b"v0\n")}, window=500
    )

    assert [f.depth for f in findings] == [1]
    assert rollback_check.refusal(findings) is False


def test_depth_counts_versions_not_commits(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")
    _commit(repo, "other.txt", "x\n", "x")
    head = _commit(repo, "other.txt", "y\n", "y")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"p.txt": _blob_sha(b"v0\n")}, window=500
    )

    assert [f.depth for f in findings] == [1]


def test_breadth_3_depth_1_refused_on_breadth_alone(tmp_path):
    repo = _repo(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit(repo, name, f"{name} old\n", f"{name} old")
    for name in ("a.txt", "b.txt", "c.txt"):
        head = _commit(repo, name, f"{name} new\n", f"{name} new")

    candidates = {n: _blob_sha(f"{n} old\n".encode()) for n in ("a.txt", "b.txt", "c.txt")}
    findings = rollback_check.find_exact_blob_rollbacks(_common(repo), head, candidates, window=500)

    assert {f.path for f in findings} == {"a.txt", "b.txt", "c.txt"}
    assert all(f.depth == 1 for f in findings)
    assert rollback_check.refusal(findings) is True


def test_status_d_revert_of_in_window_add_undeclared(tmp_path):
    repo = _repo(tmp_path)
    before_add = read_head_sha(repo)
    _commit(repo, "new.txt", "added\n", "add new.txt")
    head = _commit(repo, "new.txt", "edited\n", "edit new.txt")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"new.txt": rollback_check.ABSENT}, window=500
    )

    assert len(findings) == 1
    assert findings[0].path == "new.txt"
    assert findings[0].depth == 2
    assert findings[0].restores_commit == before_add
    assert rollback_check.refusal(findings) is True


def test_unchanged_paths_are_never_findings(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
    (repo / "c.txt").write_text("c\n", encoding="utf-8", newline="\n")
    outcome = gcommit.commit_paths(repo, ["a.txt", "b.txt", "c.txt"], "abc")
    head = outcome.sha

    candidates = {
        "a.txt": _blob_sha(b"a\n"),
        "b.txt": _blob_sha(b"b\n"),
        "c.txt": _blob_sha(b"c\n"),
    }
    findings = rollback_check.find_exact_blob_rollbacks(_common(repo), head, candidates, window=500)

    assert findings == []
    assert rollback_check.refusal(findings) is False


def test_never_tracked_path_declared_absent_is_not_a_finding(tmp_path):
    repo = _repo(tmp_path)
    head = _commit(repo, "p.txt", "v\n", "v")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"never.txt": rollback_check.ABSENT}, window=500
    )

    assert findings == []


def test_clean_multi_path_edit_no_findings(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.txt", "a0\n", "a0")
    _commit(repo, "b.txt", "b0\n", "b0")
    head = read_head_sha(repo)

    candidates = {
        "a.txt": _blob_sha(b"a-new\n"),
        "b.txt": _blob_sha(b"b-new\n"),
    }
    findings = rollback_check.find_exact_blob_rollbacks(_common(repo), head, candidates, window=500)

    assert findings == []
    assert rollback_check.refusal(findings) is False


def test_a_path_left_as_head_has_it_is_not_a_finding(tmp_path):
    repo = _repo(tmp_path)
    head = _commit(repo, "p.txt", "cur\n", "cur")

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), head, {"p.txt": _blob_sha(b"cur\n")}, window=500
    )

    assert findings == []


def test_first_parent_only_side_branch_content_never_reported(tmp_path):
    repo = _repo(tmp_path)
    (repo / "p.txt").write_text("r\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "root-p")
    root_sha = read_head_sha(repo)

    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "p.txt").write_text("side_only\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "side-p")

    _git(repo, "checkout", "-q", "work/z")
    (repo / "p.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main-a")

    _git(repo, "merge", "-q", "-s", "ours", "--no-edit", "side")
    merge_sha = read_head_sha(repo)

    findings = rollback_check.find_exact_blob_rollbacks(
        _common(repo), merge_sha, {"p.txt": _blob_sha(b"side_only\n")}, window=500
    )
    assert findings == []

    findings_own = rollback_check.find_exact_blob_rollbacks(
        _common(repo), merge_sha, {"p.txt": _blob_sha(b"a\n")}, window=500
    )
    assert findings_own == []

    findings_root = rollback_check.find_exact_blob_rollbacks(
        _common(repo), merge_sha, {"p.txt": _blob_sha(b"r\n")}, window=500
    )
    assert len(findings_root) == 1
    assert findings_root[0].depth == 1
    assert findings_root[0].restores_commit == root_sha


def test_default_window_is_1000_not_500():
    import inspect

    sig = inspect.signature(rollback_check.find_exact_blob_rollbacks)
    assert sig.parameters["window"].default == 1000


def test_reads_no_process(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    head = _commit(repo, "p.txt", "v1\n", "v1")

    with _SpawnCounter() as counter:
        rollback_check.find_exact_blob_rollbacks(
            _common(repo), head, {"p.txt": _blob_sha(b"v0\n")}, window=500
        )

    assert counter.argvs == [], f"expected zero spawns, got {counter.argvs}"
