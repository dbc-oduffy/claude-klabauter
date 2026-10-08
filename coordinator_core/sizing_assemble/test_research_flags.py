"""Tests for `sizing-assemble --research-*`: the flags write a schema-valid `research:` block."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

_NAME = "2026-10-08-r.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(repo: Path, *extra: str) -> tuple[int, dict]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, *extra])
    return code, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_full_flag_set_writes_a_schema_valid_block(repo: Path) -> None:
    code, doc = _run(
        repo,
        "--research-class", "corpus",
        "--research-appetite", "large",
        "--research-source", "web",
        "--research-source", "repo",
        "--research-source", "web",
        "--research-depth", "deeper",
        "--research-target", "repo=coordinator_core/ops",
        "--research-target", "web=https://example.com/a=b",
    )
    assert code == sa.EXIT_OK
    assert doc["research"] == {
        "value_class": "corpus",
        "appetite": "large",
        "sources": ["web", "repo"],
        "depth": "deeper",
        "targets": [
            {"source": "repo", "ref": "coordinator_core/ops"},
            {"source": "web", "ref": "https://example.com/a=b"},
        ],
    }
    assert validate_frontmatter(doc, _SCHEMA) == []


def test_only_given_keys_are_written(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "scouts")
    assert code == sa.EXIT_OK
    assert doc["research"] == {"value_class": "scouts"}


def test_research_appetite_never_touches_top_level_appetite(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "deep", "--research-appetite", "small")
    assert code == sa.EXIT_OK
    assert "appetite" not in doc
    assert doc["research"]["appetite"] == "small"


def test_no_research_flags_writes_no_research_key(repo: Path) -> None:
    code, doc = _run(repo)
    assert code == sa.EXIT_OK
    assert "research" not in doc


@pytest.mark.parametrize(
    "flags",
    [
        ("--research-class", "bogus"),
        ("--research-class", "deep", "--research-appetite", "huge"),
        ("--research-class", "deep", "--research-source", "dns"),
        ("--research-class", "deep", "--research-depth", "abyssal"),
        ("--research-class", "deep", "--research-target", "dns=x"),
        ("--research-class", "deep", "--research-target", "web"),
        ("--research-appetite", "small"),
    ],
)
def test_bad_values_are_refused_with_usage_exit_and_nothing_written(
    repo: Path, flags: tuple[str, ...]
) -> None:
    code, doc = _run(repo, *flags)
    assert code == sa.EXIT_USAGE
    assert "research" not in doc
    assert doc["status"] == "draft"


def test_research_flags_without_write_are_refused(
    capsys: pytest.CaptureFixture[str], repo: Path
) -> None:
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--research-class", "deep"])
    assert code == sa.EXIT_USAGE
    assert "--research-* requires --write" in capsys.readouterr().err


def test_one_refusal_per_bad_value(capsys: pytest.CaptureFixture[str], repo: Path) -> None:
    code, _ = _run(
        repo, "--research-class", "x", "--research-depth", "y", "--research-source", "z"
    )
    assert code == sa.EXIT_USAGE
    assert len(capsys.readouterr().err.strip().splitlines()) == 3
