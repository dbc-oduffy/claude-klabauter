"""The published form of structural-index-refresh.py still carries the real registry key."""

from __future__ import annotations

import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "structural-index-refresh.py"
_CODENAME = "project" "_rag"
_PLACEHOLDER = "example_retrieval_repo"


def _load_published_form() -> types.ModuleType:
    source = SCRIPT.read_text(encoding="utf-8").replace(_CODENAME, _PLACEHOLDER)
    module = types.ModuleType("published_structural_index_refresh")
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)  # noqa: S102
    return module


def test_published_transform_form_still_yields_the_real_key():
    module = _load_published_form()
    assert module.REGISTRY_KEY == f"repos.{_CODENAME}"
    assert module.ENSURE_SCRIPT.parts[0] == f"{_CODENAME}_scripts"


def test_published_form_resolves_the_index_repo(tmp_path, monkeypatch):
    repo = tmp_path / "idx"
    (repo / f"{_CODENAME}_scripts").mkdir(parents=True)
    (repo / f"{_CODENAME}_scripts" / "structural_index_refresh.py").write_text("", encoding="utf-8")
    module = _load_published_form()
    import coordinator_core.machine_resolver as mr

    monkeypatch.setattr(mr, "registry_get", lambda key: str(repo) if key == f"repos.{_CODENAME}" else None)
    assert module.resolve_index_repo() == repo
