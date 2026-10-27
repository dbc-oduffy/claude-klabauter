"""`attribution_gate` at the terminal-commit shape: many clean product files, reads scale with suspects."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.attribution.patterns import PATTERNS
from coordinator_core.ops.ceremony import commit_gates
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
