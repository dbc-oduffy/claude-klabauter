"""`attribution_gate` at the terminal-commit shape: many clean product files, reads scale with suspects."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.attribution.patterns import PATTERNS
from coordinator_core.ops.ceremony import commit_gates, commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

N_CLEAN = 400
_ATTR_LINE = "# Review: the Staff Engineer (Finding 1)"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], r)
    _git(["commit", "-q", "-m", "seed"], r)
    return r


def _count_head_and_index_reads(monkeypatch) -> dict:
    counts = {"object": 0, "spine_paths": 0, "index_wanted": 0}
    real_read, real_spine, real_index = (
        commit_gates.read_object, commit_gates.read_tree_spine, commit_gates.parse_index_identity,
    )

    def read_object(*a, **k):
        counts["object"] += 1
        return real_read(*a, **k)

    def read_tree_spine(root, paths):
        counts["spine_paths"] += len(list(paths))
        return real_spine(root, paths)

    def parse_index_identity(root, wanted=None, **k):
        counts["index_wanted"] += len(wanted or ())
        return real_index(root, wanted=wanted, **k)

    monkeypatch.setattr(commit_gates, "read_object", read_object)
    monkeypatch.setattr(commit_gates, "read_tree_spine", read_tree_spine)
    monkeypatch.setattr(commit_gates, "parse_index_identity", parse_index_identity)
    return counts


def _write_clean(repo: Path, n: int) -> list:
    paths = []
    for i in range(n):
        rel = f"pkg/f{i:04d}.py"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(f"v = {i}\n", encoding="utf-8")
        paths.append(rel)
    return paths


def test_clean_terminal_shape_reads_no_head_or_index(repo, monkeypatch):
    paths = _write_clean(repo, N_CLEAN)
    counts = _count_head_and_index_reads(monkeypatch)

    outcome = commit_gates.attribution_gate(repo, paths, vacated=[])

    assert outcome.passed and not outcome.skipped
    assert counts == {"object": 0, "spine_paths": 0, "index_wanted": 0}


def test_one_attributed_path_still_refused_and_only_it_reads_head(repo, monkeypatch):
    paths = _write_clean(repo, N_CLEAN)
    (repo / paths[7]).write_text(f"v = 7\n{_ATTR_LINE}\n", encoding="utf-8")
    counts = _count_head_and_index_reads(monkeypatch)

    outcome = commit_gates.attribution_gate(repo, paths, vacated=[])

    assert not outcome.passed
    assert any(f"{paths[7]}:2:" in d for d in outcome.diagnostics)
    assert counts["spine_paths"] == 1 and counts["index_wanted"] == 1


def test_staged_only_attribution_still_refused_when_deliberately_staged(repo):
    rel = "pkg/staged.py"
    (repo / "pkg").mkdir()
    (repo / rel).write_text(f"v = 1\n{_ATTR_LINE}\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    (repo / rel).write_text("v = 1\n", encoding="utf-8")

    plain = commit_gates.attribution_gate(repo, [rel], vacated=[])
    widened = commit_gates.attribution_gate(repo, [rel], vacated=[], deliberate_stage=True)

    assert plain.passed
    assert not widened.passed


def _head_classification_spines(monkeypatch) -> list:
    """Path counts of every `partition_declared_deletions` call `commit_v2` makes."""
    calls: list = []
    real = commit_v2.partition_declared_deletions

    def counting(repo, paths):
        calls.append(len(list(paths)))
        return real(repo, paths)

    monkeypatch.setattr(commit_v2, "partition_declared_deletions", counting)
    return calls


def _commit(repo: Path, paths: list):
    return commit_v2._handler({"paths": paths, "message": "add files"}, repo_root=repo / ".git")


def test_head_classification_walks_the_head_spine_once_for_n_new_paths(repo, monkeypatch):
    paths = _write_clean(repo, 50)
    calls = _head_classification_spines(monkeypatch)

    out = _commit(repo, paths)

    assert out["committed"] is True, out
    assert calls == [50]


def test_ignored_new_path_is_still_refused_after_the_shared_spine_walk(repo, monkeypatch):
    paths = _write_clean(repo, 20)
    (repo / ".gitignore").write_text("pkg/f0003.py\n", encoding="utf-8")
    _git(["add", "--", ".gitignore"], repo)
    _git(["commit", "-q", "-m", "ignore"], repo)
    calls = _head_classification_spines(monkeypatch)

    out = _commit(repo, paths)

    assert out["committed"] is False
    assert "pkg/f0003.py" in out["error"] and "gitignored" in out["error"]
    assert calls == [20]


def _landed_reader(repo: Path, calls: list):
    def read(rel):
        calls.append(rel)
        try:
            return (repo / rel).read_bytes()
        except OSError:
            return None
    return read


def test_attribution_gate_takes_landed_bytes_from_read_source_not_the_disk(repo, monkeypatch):
    paths = _write_clean(repo, 50)
    calls: list = []
    disk_reads: list = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (disk_reads.append(self), real(self, *a, **k))[1])

    outcome = commit_gates.attribution_gate(repo, paths, vacated=[], read_source=_landed_reader(repo, calls))

    assert outcome.passed
    assert sorted(calls) == sorted(paths) and disk_reads == []


def test_attribution_gate_via_read_source_still_refuses_crlf_attribution(repo):
    rel = "pkg/crlf.py"
    (repo / "pkg").mkdir()
    (repo / rel).write_bytes(f"v = 1\r\n{_ATTR_LINE}\r\n".encode("utf-8"))

    outcome = commit_gates.attribution_gate(repo, [rel], vacated=[], read_source=_landed_reader(repo, []))

    assert not outcome.passed
    assert any(f"{rel}:2:" in d for d in outcome.diagnostics)


def test_attribution_gate_reads_a_vacated_path_from_disk_not_read_source(repo):
    rel = "pkg/gone.py"
    (repo / "pkg").mkdir()
    (repo / rel).write_text(f"v = 1\n{_ATTR_LINE}\n", encoding="utf-8")
    calls: list = []

    commit_gates.attribution_gate(repo, [rel], vacated=[rel], read_source=_landed_reader(repo, calls))

    assert calls == []


def test_known_present_skips_the_stat_and_an_undeclared_deletion_is_still_refused(repo, monkeypatch):
    paths = _write_clean(repo, 30)
    stats: list = []
    real = commit_gates.os.path.exists
    monkeypatch.setattr(commit_gates.os.path, "exists", lambda p: (stats.append(p), real(p))[1])

    ok = commit_gates.declared_deletion_gate(repo, paths, [], known_present=set(paths))
    skipped_stats = len(stats)
    _git(["add", "--", "seed.txt"], repo)
    (repo / "seed.txt").unlink()
    _git(["rm", "-q", "--cached", "--", "seed.txt"], repo)
    refused = commit_gates.declared_deletion_gate(repo, ["seed.txt"], [], known_present=set())

    assert ok.passed and skipped_stats == 0
    assert not refused.passed


def test_files_present_matches_is_file_for_listed_missing_and_directory_names(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "adir.py").mkdir()
    names = [f"pkg/f{i:03d}.py" for i in range(40)]
    for rel in names:
        (tmp_path / rel).write_text("v = 1\n", encoding="utf-8")
    probe = [*names, "pkg/adir.py", "pkg/missing.py", "nodir/x.py", "top.py", "pkg\\f001.py"]
    (tmp_path / "top.py").write_text("v = 1\n", encoding="utf-8")

    got = commit_gates.files_present(tmp_path, probe)

    assert got == {p for p in probe if (tmp_path / p).is_file()}
    assert "pkg/adir.py" not in got and "pkg/missing.py" not in got


def test_files_present_lists_a_busy_directory_instead_of_stating_each_file(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    names = [f"pkg/f{i:03d}.py" for i in range(40)]
    for rel in names:
        (tmp_path / rel).write_text("v = 1\n", encoding="utf-8")
    stats: list = []
    real = commit_gates.os.path.isfile
    monkeypatch.setattr(commit_gates.os.path, "isfile", lambda p: (stats.append(p), real(p))[1])

    assert commit_gates.files_present(tmp_path, names) == set(names)
    assert stats == []


def test_worktree_bytes_reader_matches_read_bytes_across_sizes_and_unreadable_shapes(tmp_path):
    shapes = {"empty.py": b"", "small.py": b"v = 1\r\n", "exact.py": b"x" * 65536, "big.py": b"y" * 200_000}
    for name, data in shapes.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "adir").mkdir()

    for name, data in shapes.items():
        assert commit_v2._read_worktree_bytes(str(tmp_path), name) == data
    assert commit_v2._read_worktree_bytes(str(tmp_path), "missing.py") is None
    assert commit_v2._read_worktree_bytes(str(tmp_path), "adir") is None


def test_fast_worktree_read_serves_worktree_landed_paths_and_defers_the_rest(repo):
    paths = _write_clean(repo, 12)
    removed, staged = paths[0], paths[1]
    calls: list = []
    (repo / removed).write_text("removed = 1\n", encoding="utf-8")

    def source(rel):
        calls.append(rel)
        return (repo / rel).read_bytes() if (repo / rel).is_file() else None

    refusal = commit_v2._pre_commit_gates(
        repo, paths[1:], [removed], source, prefer_staged=[staged], fast_worktree_read=True,
    )

    assert refusal is None
    assert sorted(set(calls)) == sorted({removed, staged})


def test_without_fast_worktree_read_every_landed_path_asks_the_injected_reader(repo):
    paths = _write_clean(repo, 12)
    calls: list = []

    def source(rel):
        calls.append(rel)
        return (repo / rel).read_bytes()

    assert commit_v2._pre_commit_gates(repo, paths, [], source) is None
    assert sorted(set(calls)) == sorted(paths)


def test_leak_gate_takes_landed_text_from_the_shared_read_and_still_refuses_an_uncommitted_import(repo, monkeypatch):
    from coordinator_core.authoring_leaks import import_closure

    paths = _write_clean(repo, 6)
    (repo / "pkg" / "uncommitted.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / paths[0]).write_text("from pkg.uncommitted import helper\n\nv = helper()\n", encoding="utf-8")
    disk_reads: list = []
    real = import_closure._read_text
    monkeypatch.setattr(import_closure, "_read_text", lambda path: (disk_reads.append(path), real(path))[1])

    refusal = commit_v2._pre_commit_gates(repo, paths, [], _landed_reader(repo, []))

    assert refusal is not None and "leak_gate" in refusal and "pkg/uncommitted.py" in refusal
    assert not [p for p in disk_reads if p.name != "uncommitted.py"]


def test_leak_gate_reads_a_declared_removal_from_the_worktree_not_the_landed_tree(repo, monkeypatch):
    from coordinator_core.authoring_leaks import import_closure

    paths = _write_clean(repo, 4)
    removed = paths[0]
    disk_reads: list = []
    real = import_closure._read_text
    monkeypatch.setattr(import_closure, "_read_text", lambda path: (disk_reads.append(path.name), real(path))[1])

    commit_v2._pre_commit_gates(repo, paths[1:], [removed], _landed_reader(repo, []))

    assert Path(removed).name in disk_reads


def test_ignored_untracked_is_answered_in_process_and_spawns_only_for_a_declined_verdict(repo, monkeypatch):
    from coordinator_core.git import ignore_match

    (repo / ".gitignore").write_text("pkg/f0003.py\n", encoding="utf-8")
    paths = _write_clean(repo, 8)
    spawns: list = []
    real_run_git = commit_v2.run_git
    monkeypatch.setattr(commit_v2, "run_git", lambda *a, **k: (spawns.append(a), real_run_git(*a, **k))[1])

    found = commit_v2._ignored_untracked(repo, paths, set(paths))

    assert found == ["pkg/f0003.py"] and spawns == []

    monkeypatch.setattr(ignore_match.IgnoreMatcher, "ignored", lambda self, rel, is_dir: None)
    declined = commit_v2._ignored_untracked(repo, paths, set(paths))

    assert declined == ["pkg/f0003.py"] and len(spawns) == 1


def test_prefilter_is_a_superset_of_every_pattern():
    samples = {
        "review_colon": "# Review: the Staff Engineer (Finding 1)",
        "finding_ref": "see Finding 3 here",
        "staff_eng_finding": "the staff-eng finding says",
        "reviewer_finding": "a reviewer finding",
        "review_corrected": "REVIEW-CORRECTED",
    }
    for name, line in samples.items():
        assert PATTERNS[name].search(line), name
        assert commit_gates._ANY_ATTRIBUTION_PATTERN.search(line), name
    assert not commit_gates._ANY_ATTRIBUTION_PATTERN.search("v = 1\n")
    # Samples above cover only some patterns; every PATTERNS entry must be inside the alternation.
    alternation = commit_gates._AnyAttributionPattern._compiled.pattern
    for name, p in PATTERNS.items():
        assert p.pattern in alternation, name
