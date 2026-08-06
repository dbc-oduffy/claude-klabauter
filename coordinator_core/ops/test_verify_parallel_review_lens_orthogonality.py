"""Characterization tests for
coordinator_core.ops.verify_parallel_review_lens_orthogonality.

Port of: verify-parallel-review-lens-orthogonality.sh (example-doctrine-repo b5a4192c, 2026-07-20);
parity confirmed against the bash oracle across a positive+negative corpus
(no-args, valid/colliding/missing/empty chunk manifest, unknown arg, missing
--chunk-manifest value) before this file was written — see the port's
completion notes.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.verify_parallel_review_lens_orthogonality import (
    chunk_check,
    main,
    run,
    static_check,
)

_GOOD_SKILL_MD = """\
# parallel-code-review

## Lens-Domain Manifest

| Reviewer | Lens domain | Rationale |
|---|---|---|
| the Staff Engineer (`agents/staff-eng.md`) | code-semantics | ... |
| security-audit-worker (`agents/security-audit-worker.md`) | security | ... |
| dep-cve-auditor (`agents/dep-cve-auditor.md`) | deps | ... |
| test-evidence-parser (`agents/test-evidence-parser.md`) | tests | ... |

---

## Next section
"""


def _make_repo(tmp_path: Path, skill_md: str = _GOOD_SKILL_MD) -> Path:
    """Build a minimal example-doctrine-repo-shaped tree: coordinator/skills/... + coordinator/agents/*.md"""
    skills_dir = tmp_path / "coordinator" / "skills" / "parallel-code-review"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    agents_dir = tmp_path / "coordinator" / "agents"
    agents_dir.mkdir(parents=True)
    for name in ("staff-eng.md", "security-audit-worker.md", "dep-cve-auditor.md", "test-evidence-parser.md"):
        (agents_dir / name).write_text("# agent\n", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# static_check
# ---------------------------------------------------------------------------


def test_static_check_passes_with_four_orthogonal_lenses(tmp_path):
    repo = _make_repo(tmp_path)
    skill_file = repo / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo / "coordinator" / "agents"
    lines, passed = static_check(skill_file, agents_dir)
    assert passed is True
    assert any(line.startswith("OK (static): 4 orthogonal lenses") for line in lines)


def test_static_check_fails_on_missing_skill_file(tmp_path):
    lines, passed = static_check(tmp_path / "nope" / "SKILL.md", tmp_path / "agents")
    assert passed is False
    assert any("Skill file not found" in line for line in lines)


def test_static_check_fails_on_missing_agent_file(tmp_path):
    skill_md = _GOOD_SKILL_MD.replace(
        "| the Staff Engineer (`agents/staff-eng.md`) | code-semantics | ... |",
        "| the Staff Engineer (`agents/does-not-exist.md`) | code-semantics | ... |",
    )
    repo = _make_repo(tmp_path, skill_md)
    skill_file = repo / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo / "coordinator" / "agents"
    lines, passed = static_check(skill_file, agents_dir)
    assert passed is False
    assert any("Agent file missing" in line for line in lines)


def test_static_check_fails_on_lens_domain_collision(tmp_path):
    skill_md = _GOOD_SKILL_MD.replace(
        "| security-audit-worker (`agents/security-audit-worker.md`) | security | ... |",
        "| security-audit-worker (`agents/security-audit-worker.md`) | code-semantics | ... |",
    )
    repo = _make_repo(tmp_path, skill_md)
    skill_file = repo / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo / "coordinator" / "agents"
    lines, passed = static_check(skill_file, agents_dir)
    assert passed is False
    assert any("Lens-domain collision" in line for line in lines)


def test_static_check_fails_below_minimum_reviewer_count(tmp_path):
    skill_md = """\
## Lens-Domain Manifest

| Reviewer | Lens domain | Rationale |
|---|---|---|
| the Staff Engineer (`agents/staff-eng.md`) | code-semantics | ... |

---
"""
    repo = _make_repo(tmp_path, skill_md)
    skill_file = repo / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo / "coordinator" / "agents"
    lines, passed = static_check(skill_file, agents_dir)
    assert passed is False
    assert any("Expected at least 4 orthogonal-lens rows" in line for line in lines)


def test_static_check_fails_on_unparseable_reviewer_cell(tmp_path):
    skill_md = _GOOD_SKILL_MD.replace(
        "| the Staff Engineer (`agents/staff-eng.md`) | code-semantics | ... |",
        "| the Staff Engineer (no backtick path) | code-semantics | ... |",
    )
    repo = _make_repo(tmp_path, skill_md)
    skill_file = repo / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo / "coordinator" / "agents"
    lines, passed = static_check(skill_file, agents_dir)
    assert passed is False
    assert any("Could not parse agent path" in line for line in lines)


# ---------------------------------------------------------------------------
# chunk_check
# ---------------------------------------------------------------------------


def test_chunk_check_passes_on_disjoint_partitions(tmp_path):
    manifest = tmp_path / "chunks.tsv"
    manifest.write_text("chunk-1\tfoo.py\nchunk-1\tbar.py\nchunk-2\tbaz.py\n", encoding="utf-8")
    lines, passed = chunk_check(manifest)
    assert passed is True
    assert any("OK (chunks): 2 chunks over 3 distinct files" in line for line in lines)


def test_chunk_check_fails_on_file_claimed_by_two_chunks(tmp_path):
    manifest = tmp_path / "chunks.tsv"
    manifest.write_text("chunk-1\tfoo.py\nchunk-2\tfoo.py\n", encoding="utf-8")
    lines, passed = chunk_check(manifest)
    assert passed is False
    assert any("chunk partitions not disjoint" in line for line in lines)


def test_chunk_check_same_file_same_chunk_twice_is_harmless(tmp_path):
    manifest = tmp_path / "chunks.tsv"
    manifest.write_text("chunk-1\tfoo.py\nchunk-1\tfoo.py\n", encoding="utf-8")
    lines, passed = chunk_check(manifest)
    assert passed is True


def test_chunk_check_fails_on_missing_manifest(tmp_path):
    lines, passed = chunk_check(tmp_path / "nope.tsv")
    assert passed is False
    assert any("chunk manifest not found" in line for line in lines)


def test_chunk_check_fails_on_empty_manifest(tmp_path):
    manifest = tmp_path / "empty.tsv"
    manifest.write_text("", encoding="utf-8")
    lines, passed = chunk_check(manifest)
    assert passed is False
    assert any("contained no chunk/file rows" in line for line in lines)


def test_chunk_check_skips_comment_and_blank_lines(tmp_path):
    manifest = tmp_path / "chunks.tsv"
    manifest.write_text("# comment\n\nchunk-1\tfoo.py\n", encoding="utf-8")
    lines, passed = chunk_check(manifest)
    assert passed is True


# ---------------------------------------------------------------------------
# run() — arg parsing, doe_root injection, stdout/stderr split, rc contract
# ---------------------------------------------------------------------------


def test_run_no_args_runs_static_only(tmp_path):
    repo = _make_repo(tmp_path)
    stdout, stderr, rc = run([], doe_root=str(repo))
    assert rc == 0
    assert stderr == []
    assert any("OK (static)" in line for line in stdout)


def test_run_chunk_manifest_runs_static_then_chunk(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = tmp_path / "chunks.tsv"
    manifest.write_text("chunk-1\tfoo.py\n", encoding="utf-8")
    stdout, stderr, rc = run(["--chunk-manifest", str(manifest)], doe_root=str(repo))
    assert rc == 0
    assert any("OK (static)" in line for line in stdout)
    assert any("OK (chunks)" in line for line in stdout)


def test_run_static_fail_short_circuits_before_chunk_check(tmp_path):
    lines, passed = static_check(tmp_path / "nope" / "SKILL.md", tmp_path / "agents")
    assert passed is False
    stdout, stderr, rc = run(
        ["--chunk-manifest", "/does/not/matter.tsv"], doe_root=str(tmp_path)
    )
    assert rc == 1
    assert not any("chunk" in line.lower() for line in stdout if "Skill" not in line and "manifest" not in line.lower())


def test_run_unknown_arg_goes_to_stderr(tmp_path):
    stdout, stderr, rc = run(["--bogus"], doe_root=str(tmp_path))
    assert rc == 1
    assert stdout == []
    assert any("unknown argument" in line for line in stderr)


def test_run_missing_chunk_manifest_value_goes_to_stderr(tmp_path):
    stdout, stderr, rc = run(["--chunk-manifest"], doe_root=str(tmp_path))
    assert rc == 1
    assert stdout == []
    assert any("requires a path argument" in line for line in stderr)


def test_run_unresolvable_doe_root_fails_with_business_code(tmp_path):
    stdout, stderr, rc = run([], doe_root="")
    assert rc == 1
    assert any("could not resolve the example-doctrine-repo repo root" in line for line in stdout)


def test_main_prints_stdout_to_stdout_and_returns_rc(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    import coordinator_core.ops.verify_parallel_review_lens_orthogonality as mod

    monkeypatch.setattr(mod, "_resolve_doe_root", lambda: str(repo))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "OK (static)" in captured.out
    assert captured.err == ""


def test_main_prints_usage_error_to_stderr(monkeypatch, capsys):
    rc = main(["--bogus"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "unknown argument" in captured.err
