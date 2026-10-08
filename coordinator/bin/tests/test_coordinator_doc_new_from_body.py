"""`coordinator-doc-new --from-body` wraps an already-written body in the
type's canonical frontmatter, in place, through the same generator as a fresh
scaffold.

Run:
    python -m pytest -p no:cacheprovider -q coordinator/bin/tests/test_coordinator_doc_new_from_body.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    name = "coordinator_doc_new_from_body_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_BIN_DIR / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()

_BODY = "# Adopt me\n\n## Problem\n\nreal prose with unicode — kept\n\n## Decision\n\nDo it.\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    monkeypatch.setattr(_MOD, "_current_repo_root", lambda: str(tmp_path))
    return tmp_path


def _split(text: str) -> tuple[str, str]:
    assert text.startswith("---\n")
    fm, body = text[4:].split("\n---\n", 1)
    return fm, body


def test_bodyless_of_frontmatter_gets_canonical_frontmatter_in_place(repo):
    path = repo / "docs" / "decisions" / "my-decision.md"
    path.write_bytes(_BODY.encode("utf-8"))

    rc = _MOD.main(["--type", "decision", "--from-body", str(path)])

    assert rc == 0
    fm, body = _split(path.read_bytes().decode("utf-8"))
    assert body == _BODY
    assert "title: Adopt me" in fm or 'title: "Adopt me"' in fm
    assert "status: proposed" in fm
    assert "deciders:" in fm
    assert any(line.startswith("id: DR-") for line in fm.splitlines())


def test_partial_frontmatter_is_conformed_and_its_values_win(repo):
    path = repo / "docs" / "decisions" / "partial.md"
    path.write_bytes(
        ("---\ntitle: Kept title\nid: DR-777\nstatus: accepted\n---\n" + _BODY).encode("utf-8")
    )

    rc = _MOD.main(["--type", "decision", "--from-body", str(path)])

    assert rc == 0
    fm, body = _split(path.read_text(encoding="utf-8"))
    assert body == _BODY
    lines = fm.splitlines()
    assert "id: DR-777" in lines
    assert "status: accepted" in lines
    assert any(line.startswith("title:") and "Kept title" in line for line in lines)
    assert any(line.startswith("created:") for line in lines)
    assert "deciders:" in lines
    assert sum(1 for line in lines if line.startswith("status:")) == 1


def test_unreadable_from_body_fails_loud(repo, capsys):
    rc = _MOD.main(["--type", "decision", "--from-body", str(repo / "missing.md")])
    assert rc == 1
    assert "cannot read --from-body" in capsys.readouterr().err
