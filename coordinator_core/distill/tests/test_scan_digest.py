"""scan_digest: machine blocks collapse; prose, headings and kept metadata survive byte-for-byte."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.distill.scan_digest import build_digest, digest_name, write_digests

PROSE = "# Title\n\nA nugget: always do X.\n\n## Heading two\n\n- bullet kept\n"

DOC = (
    "---\n"
    "title: Something\n"
    "status: implemented\n"
    "created: 2026-11-12\n"
    "author: someone\n"
    "supersedes:\n"
    "  - old-plan\n"
    "branch: work/x\n"
    "---\n"
    + PROSE
    + "\n```yaml plan-tasks\n"
    "- id: C0\n"
    "  title: First task\n"
    "  writes: [a.py, b.py]\n"
    "  body: |\n"
    "    long machine text\n"
    "- id: C1\n"
    "  title: Second task\n"
    "```\n"
    "\nTail prose.\n"
)


def test_frontmatter_keeps_only_metadata_keys_with_continuations():
    digest = build_digest(DOC)
    assert digest.startswith("---\nstatus: implemented\ncreated: 2026-11-12\nsupersedes:\n  - old-plan\n---\n")
    assert "author:" not in digest and "branch:" not in digest and "title: Something" not in digest


def test_plan_tasks_block_collapses_to_one_line_per_item():
    digest = build_digest(DOC)
    assert "[yaml plan-tasks] C0: First task\n[yaml plan-tasks] C1: Second task\n" in digest
    assert "long machine text" not in digest and "writes:" not in digest and "```" not in digest


def test_prose_and_headings_survive_byte_for_byte():
    digest = build_digest(DOC)
    assert PROSE in digest
    assert digest.endswith("\nTail prose.\n")


def test_non_machine_fence_is_kept_verbatim():
    text = "intro\n```python\nprint('x')\n```\n```yaml\nkey: v\n```\n"
    assert build_digest(text) == text


def test_unclosed_machine_fence_is_kept_verbatim():
    text = "intro\n```yaml plan-tasks\n- id: C0\n  title: T\n"
    assert build_digest(text) == text


def test_evidence_block_collapses():
    text = "```yaml evidence\n- id: E1\n  title: Proof\n  detail: x\n```\n"
    assert build_digest(text) == "[yaml evidence] E1: Proof\n"


def test_no_frontmatter_is_all_prose():
    assert build_digest(PROSE) == PROSE


def test_unterminated_frontmatter_is_left_alone():
    text = "---\nstatus: x\nno closing fence\n"
    assert build_digest(text) == text


def test_crlf_prose_preserved():
    text = "---\r\nstatus: done\r\n---\r\nline one\r\nline two\r\n"
    assert build_digest(text) == text


def test_digest_is_idempotent_and_deterministic():
    once = build_digest(DOC)
    assert build_digest(DOC) == once
    assert build_digest(once) == once


def test_digest_name_is_stable_and_path_keyed():
    assert digest_name("a/b.md") == digest_name("a/b.md")
    assert digest_name("a/b.md") != digest_name("a/c.md")
    assert digest_name("a/b.md").endswith(".md")


def test_write_digests_writes_beside_run_and_reports_sizes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.md").write_text(DOC, encoding="utf-8")
    run_dir = "state/scratch/artifact-distillation/r1"
    out = write_digests(tmp_path, run_dir, ["docs/p.md", "docs/missing.md"])
    assert set(out) == {"docs/p.md"}
    rel, size = out["docs/p.md"]
    assert rel == f"{run_dir}/digest/{digest_name('docs/p.md')}"
    on_disk = (tmp_path / rel).read_bytes()
    assert len(on_disk) == size and on_disk.decode() == build_digest(DOC)
    assert size < len(DOC.encode())
