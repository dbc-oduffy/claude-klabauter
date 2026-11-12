"""Pins the binding reader and the per-run opt-in predicate; tmp_path DoE roots only."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import stage_family_binding as sfb
from coordinator_core.ops.emit import doe_drift

_GOOD = (
    "schema_version: 1\nopt_in: per-run\nrunner: coordinator:family-stage-runner\n"
    "fallback: claude\nroles:\n  scout: grok\n  research: agy\n  review: codex\n"
)


def _root(tmp_path, text):
    f = tmp_path / "coordinator" / "pipelines" / "stage-family-binding.yaml"
    f.parent.mkdir(parents=True)
    f.write_text(text, encoding="utf-8")
    return tmp_path


def test_opted_in_only_on_exact_ref():
    assert sfb.opted_in({"stage_family_binding": sfb.BINDING_REF}) is True
    assert sfb.opted_in({}) is False
    assert sfb.opted_in({"stage_family_binding": ""}) is False
    assert sfb.opted_in(None) is False
    with pytest.raises(ValueError, match="coordinator-content-repo:coordinator/pipelines"):
        sfb.opted_in({"stage_family_binding": "somewhere/else.yaml"})


def test_load_reads_roles(tmp_path):
    b = sfb.load(_root(tmp_path, _GOOD))
    assert b.runner == "coordinator:family-stage-runner" and b.fallback == "claude"
    assert b.roles == {"scout": "grok", "research": "agy", "review": "codex"}


@pytest.mark.parametrize(
    "old,new,match",
    [
        ("schema_version: 1", "schema_version: 2", "schema_version"),
        ("runner: coordinator:family-stage-runner", "runner: other", "runner"),
        ("fallback: claude", "fallback: codex", "fallback"),
        ("  scout: grok", "  build: grok", "role"),
        ("  scout: grok", "  scout: gpt", "family"),
    ],
)
def test_load_refuses_contract_violations(tmp_path, old, new, match):
    with pytest.raises(sfb.BindingError, match=match):
        sfb.load(_root(tmp_path, _GOOD.replace(old, new)))


def test_load_missing_file_is_binding_error(tmp_path):
    with pytest.raises(sfb.BindingError, match="cannot read"):
        sfb.load(tmp_path)


def test_unresolved_content_root_names_registry_key(monkeypatch):
    def boom():
        raise doe_drift.DoeResolveError("nope")

    monkeypatch.setattr(doe_drift, "resolve_doe_clone", boom)
    with pytest.raises(sfb.BindingError, match=r"engine\.working_repos\.content_root"):
        sfb.load()
