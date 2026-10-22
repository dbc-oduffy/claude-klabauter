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


_RS_BODY = "# Synth\n\n## Findings\n\nreal prose\n"


def _fm_lines(path):
    fm, _ = _split(path.read_text(encoding="utf-8"))
    return fm.splitlines()


def test_research_synthesis_flags_land_in_frontmatter(repo):
    out = repo / "docs" / "research" / "fresh.md"
    rc = _MOD.main([
        "--type", "research-synthesis", "--title", "T", "--out", str(out),
        "--question", "Does X hold?", "--pipeline", "repo",
    ])
    assert rc == 0
    lines = _fm_lines(out)
    assert 'question: "Does X hold?"' in lines or "question: Does X hold?" in lines
    assert "pipeline: repo" in lines


def test_research_synthesis_flags_land_under_from_body(repo):
    path = repo / "docs" / "research" / "adopt.md"
    path.parent.mkdir(parents=True)
    path.write_text(_RS_BODY, encoding="utf-8")
    rc = _MOD.main([
        "--type", "research-synthesis", "--from-body", str(path),
        "--question", "Why Y?", "--pipeline", "structured",
    ])
    assert rc == 0
    lines = _fm_lines(path)
    assert any(line.startswith("question:") and "Why Y?" in line for line in lines)
    assert "pipeline: structured" in lines
    assert sum(1 for line in lines if line.startswith("pipeline:")) == 1
    assert path.read_text(encoding="utf-8").endswith(_RS_BODY)


def test_research_synthesis_body_values_kept_without_flags_and_flags_override(repo):
    path = repo / "docs" / "research" / "kept.md"
    path.parent.mkdir(parents=True)
    src = '---\ntitle: K\nquestion: "Body question?"\npipeline: notebooklm\n---\n' + _RS_BODY
    path.write_text(src, encoding="utf-8")
    assert _MOD.main(["--type", "research-synthesis", "--from-body", str(path)]) == 0
    lines = _fm_lines(path)
    assert any("Body question?" in line for line in lines)
    assert "pipeline: notebooklm" in lines

    assert _MOD.main([
        "--type", "research-synthesis", "--from-body", str(path), "--pipeline", "web",
    ]) == 0
    lines = _fm_lines(path)
    assert any("Body question?" in line for line in lines)
    assert "pipeline: web" in lines


def test_research_synthesis_placeholder_question_refused(repo, capsys):
    out = repo / "docs" / "research" / "nope.md"
    rc = _MOD.main(["--type", "research-synthesis", "--title", "T", "--out", str(out)])
    assert rc != 0
    assert "--question" in capsys.readouterr().err
    assert not out.exists()

    path = repo / "docs" / "research" / "nope-body.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_RS_BODY, encoding="utf-8")
    assert _MOD.main(["--type", "research-synthesis", "--from-body", str(path)]) != 0
    assert path.read_text(encoding="utf-8") == _RS_BODY


def test_question_flag_refused_for_other_types(repo, capsys):
    out = repo / "d.md"
    rc = _MOD.main(["--type", "decision", "--title", "T", "--question", "Q?", "--out", str(out)])
    assert rc != 0
    assert "research-synthesis" in capsys.readouterr().err
    assert not out.exists()


def test_research_synthesis_unknown_pipeline_rejected(repo):
    with pytest.raises(SystemExit) as exc:
        _MOD.main([
            "--type", "research-synthesis", "--title", "T", "--question", "Q?",
            "--pipeline", "bogus", "--out", str(repo / "x.md"),
        ])
    assert exc.value.code != 0


_SZ = ["--type", "sizing-object", "--title", "T", "--premise", "not-applicable", "--premise-evidence", "x"]
_YAML_BODY = "# kept comment\ntitle: Adopted sizing\nextra_key: kept\n"


def test_sizing_object_yaml_body_adopted_without_fence(repo, capsys):
    path = repo / "sz.yaml"
    path.write_text(_YAML_BODY, encoding="utf-8")
    rc = _MOD.main(_SZ + ["--from-body", str(path)])
    err = capsys.readouterr().err
    assert "no frontmatter to adopt" not in err
    assert rc == 0, err
    text = path.read_text(encoding="utf-8")
    assert not text.startswith("---")
    assert "# kept comment" in text
    assert text.rstrip().endswith("extra_key: kept")
    assert sum(1 for ln in text.splitlines() if ln.startswith("title:")) == 1


def test_yaml_adopt_merges_body_keys_over_defaults():
    gen = "title: default\nkind: gen\n"
    out = _MOD._adopt_yaml_body(gen, "extra: 1\ntitle: mine\n")
    assert out == "title: mine\nkind: gen\nextra: 1\n"


def test_non_mapping_yaml_body_refused_exit_2(repo, capsys):
    path = repo / "bad.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    rc = _MOD.main(_SZ + ["--from-body", str(path)])
    assert rc == 2
    assert "sizing-object" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == "- a\n- b\n"


def test_unreadable_from_body_fails_loud(repo, capsys):
    rc = _MOD.main(["--type", "decision", "--from-body", str(repo / "missing.md")])
    assert rc == 1
    assert "cannot read --from-body" in capsys.readouterr().err
