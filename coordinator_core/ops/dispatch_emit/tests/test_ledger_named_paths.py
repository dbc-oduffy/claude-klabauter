"""Pins named_paths and gated_paths of ledger_remaining_paths."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import _named_tagged, gated_paths, named_paths


def _repo(tmp_path):
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/x.md").write_text("x")
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch/a.py").write_text("x")
    return tmp_path


def test_admits_existing_files(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["edit docs/plans/x.md and CLAUDE.md"], root) == ["docs/plans/x.md", "CLAUDE.md"]


def test_dedupes_across_texts(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["CLAUDE.md", "CLAUDE.md docs/plans/x.md"], root) == ["CLAUDE.md", "docs/plans/x.md"]


def test_drops_unnameable_tokens(tmp_path):
    root = _repo(tmp_path)
    texts = ["nodir/absent.md scratch/a.py ../x.md /etc/passwd.txt README word"]
    assert named_paths(texts, root) == []


def test_strips_line_suffix(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["docs/plans/x.md:12"], root) == ["docs/plans/x.md"]


def test_spelling_called_once_canonicalizes_and_dedupes(tmp_path):
    calls = []

    def spelling(paths):
        calls.append(list(paths))
        return {"a/Pub.py": "a/pub.py", "a/pub.py": "a/pub.py", "a/gone.py": None}

    out = named_paths(["a/Pub.py a/gone.py", "a/pub.py"], tmp_path, spelling=spelling)
    assert out == ["a/pub.py"]
    assert len(calls) == 1 and calls[0] == ["a/Pub.py", "a/gone.py", "a/pub.py", "a"]


def test_new_file_in_existing_dir_admitted_with_one_spelling_call(tmp_path):
    (tmp_path / "pkg").mkdir()
    calls = []

    def spelling(paths):
        calls.append(list(paths))
        return {"pkg": "pkg", "pkg/new.py": None, "nodir/new.py": None}

    out, dropped, _ = _named_tagged(["pkg/new.py nodir/new.py"], tmp_path, spelling)
    assert out == ["pkg/new.py"]
    assert dropped == ["nodir/new.py"]
    assert calls == [["pkg/new.py", "nodir/new.py", "pkg", "nodir"]]


def test_new_file_under_case_variant_dir_admitted_when_variant_is_not_a_dir(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    real_is_dir = Path.is_dir

    def case_sensitive_is_dir(self, *a, **kw):
        if self.name == "Pkg":
            return False
        return real_is_dir(self, *a, **kw)

    monkeypatch.setattr(Path, "is_dir", case_sensitive_is_dir)
    calls = []

    def spelling(paths):
        calls.append(list(paths))
        return {p: {"Pkg": "pkg"}.get(p) for p in paths}

    out, dropped, _ = _named_tagged(["Pkg/new.py"], tmp_path, spelling)
    assert out == ["pkg/new.py"] and dropped == []
    assert len(calls) == 1 and "Pkg" in calls[0]


def test_spelling_ignores_disk_and_parent_dir_leg(tmp_path):
    root = _repo(tmp_path)
    (root / "docs/plans/y.md").write_text("x")
    backed = {"docs/plans/y.md": "docs/plans/y.md"}
    out = named_paths(["docs/plans/x.md docs/plans/y.md"], root, spelling=lambda ps: {p: backed.get(p) for p in ps})
    assert out == ["docs/plans/y.md"]


def test_gated_paths_selects_claude_md_class():
    assert gated_paths(["CLAUDE.md", "docs/plans/x.md", "coordinator/CLAUDE.md", "docs/CLAUDE.md.bak"]) == [
        "CLAUDE.md",
        "coordinator/CLAUDE.md",
    ]
