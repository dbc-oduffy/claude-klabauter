"""review.reachability doc kind: a new markdown doc is reachable only from an added reference or an ancestor index."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_reachability import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_WIKI = "docs" + "/wiki/"
_X = _WIKI + "x.md"
_OTHER = _WIKI + "other.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path: Path, base: dict[str, str]) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _put(root, "keep.txt", "x\n")
    for rel, text in base.items():
        _put(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _run(root: Path, base: str, paths: list[str]) -> dict:
    out = _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "paths": paths, "entry_kinds": ["doc"]})
    assert out["error"] is None
    assert out["cost"]["spawns"] == 1
    return {e["ref"]: e["verdict"] for e in out["entries"]}


def test_unreferenced_wiki_doc_is_unreachable(tmp_path):
    root, base = _repo(tmp_path, {_OTHER: "other\n"})
    _put(root, _X, "# x\n")
    assert _run(root, base, [_X]) == {_X: "unreachable"}


def test_added_link_in_sibling_doc_makes_it_reachable(tmp_path):
    root, base = _repo(tmp_path, {_OTHER: "other\n"})
    _put(root, _X, "# x\n")
    _put(root, _OTHER, "other\n[x](x.md)\n")
    verdicts = _run(root, base, [_X])
    assert verdicts == {_X: "reachable"}


def test_ancestor_readme_at_base_makes_it_reachable(tmp_path):
    root, base = _repo(tmp_path, {_WIKI + "README.md": f"see {_X}\n"})
    _put(root, _X, "# x\n")
    assert _run(root, base, [_X]) == {_X: "reachable"}


def test_generic_index_name_mention_does_not_wire_another_index(tmp_path):
    root, base = _repo(tmp_path, {_OTHER: "other\n"})
    _put(root, "docs/reference/index.md", "# ref\n")
    _put(root, _OTHER, "other\nsee index.md\n")
    assert _run(root, base, ["docs/reference/index.md"]) == {"docs/reference/index.md": "unreachable"}


def test_only_a_test_file_referrer_stays_unreachable(tmp_path):
    root, base = _repo(tmp_path, {"tests/test_a.py": "pass\n"})
    _put(root, _X, "# x\n")
    _put(root, "tests/test_a.py", f"pass\n# {_X}\n")
    assert _run(root, base, [_X]) == {_X: "unreachable"}


def test_a_plan_citing_the_doc_does_not_wire_it(tmp_path):
    root, base = _repo(tmp_path, {"docs/plans/p.md": "plan\n"})
    _put(root, _X, "# x\n")
    _put(root, "docs/plans/p.md", f"plan\n{_X}\n")
    assert _run(root, base, [_X]) == {_X: "unreachable"}


def test_skill_and_plan_and_discovered_pages_produce_no_entry(tmp_path):
    root, base = _repo(tmp_path, {})
    paths = ["skills/foo/SKILL.md", "docs/plans/p.md", "commands/c.md", "agents/a.md"]
    for p in paths:
        _put(root, p, "# n\n")
    assert _run(root, base, paths) == {}


def test_skill_page_referenced_from_skill_md_is_reachable(tmp_path):
    root, base = _repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    _put(root, "skills/foo/ref.md", "# ref\n")
    _put(root, "skills/foo/SKILL.md", "# foo\nSee ref.md\n")
    assert _run(root, base, ["skills/foo/ref.md"]) == {"skills/foo/ref.md": "reachable"}


def test_skill_page_nobody_names_is_unreachable(tmp_path):
    root, base = _repo(tmp_path, {"skills/foo/SKILL.md": "# foo\n"})
    _put(root, "skills/foo/ref.md", "# ref\n")
    assert _run(root, base, ["skills/foo/ref.md"]) == {"skills/foo/ref.md": "unreachable"}


def test_unmodelled_markdown_is_undecidable(tmp_path):
    root, base = _repo(tmp_path, {})
    _put(root, "notes/x.md", "# x\n")
    assert _run(root, base, ["notes/x.md"]) == {"notes/x.md": "undecidable"}


def test_relative_parent_link_from_another_directory_is_reachable(tmp_path):
    root, base = _repo(tmp_path, {"docs/reference/r.md": "r\n"})
    _put(root, _X, "# x\n")
    _put(root, "docs/reference/r.md", "r\n[x](../wiki/x.md)\n")
    assert _run(root, base, [_X]) == {_X: "reachable"}
