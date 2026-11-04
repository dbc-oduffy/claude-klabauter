"""A `--research-source repo` sizing with no `--research-target` is accepted at write time and stamps no target."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import record_homes

_NAME = "2026-10-08-rt.yaml"
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


@pytest.mark.parametrize("primary", [(), ("--research-primary", "repo")])
def test_repo_source_without_a_target_writes_cleanly(repo: Path, primary: tuple[str, ...]) -> None:
    code, doc = _write(repo, "--research-class", "corpus", "--research-source", "repo", *primary)
    assert code == sa.EXIT_OK
    assert doc["research"]["sources"] == ["repo"]
    assert not doc["research"].get("targets")


def test_repo_primary_without_any_repo_source_is_still_refused(repo: Path) -> None:
    code, _ = _write(repo, "--research-class", "corpus", "--research-source", "web", "--research-primary", "repo")
    assert code != sa.EXIT_OK
