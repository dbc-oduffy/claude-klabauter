"""A `--research-source structured` with no structured target is dropped at write time; with one it is kept."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import record_homes

_NAME = "2026-10-08-rs.yaml"
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
    Path(record_homes.record_path(str(tmp_path), "sizings", _NAME)).parent.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(repo: Path, *extra: str) -> tuple[int, dict]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = path.relative_to(repo).as_posix()
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, *extra])
    return code, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_structured_source_without_a_target_is_dropped(repo: Path, capfd: pytest.CaptureFixture[str]) -> None:
    code, doc = _write(
        repo, "--research-class", "corpus", "--research-source", "repo", "--research-source", "structured"
    )
    assert code == sa.EXIT_OK
    assert doc["research"]["sources"] == ["repo"]
    assert "dropped --research-source structured" in capfd.readouterr().err


def test_structured_source_with_a_target_is_kept(repo: Path) -> None:
    code, doc = _write(
        repo, "--research-class", "corpus", "--research-source", "repo", "--research-source", "structured",
        "--research-target", "structured=data/table.csv",
    )
    assert code == sa.EXIT_OK
    assert doc["research"]["sources"] == ["repo", "structured"]
