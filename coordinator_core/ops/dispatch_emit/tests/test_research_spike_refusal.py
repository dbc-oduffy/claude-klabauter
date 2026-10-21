"""A spike (or unmapped) research class is refused at emit; no script, receipt, scratch or ledger row is written."""
from __future__ import annotations

import pytest

from coordinator_core.ops import research_shape
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing, repo  # noqa: F401

SPIKE_MESSAGE = "research.value_class=spike has no engine pipeline; run /coordinator:spike with the sizing"


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


@pytest.mark.parametrize("appetite", ["small", "medium", "large"])
def test_spike_sizing_is_refused_naming_the_spike_route_and_writes_nothing(repo, capsys, appetite):  # noqa: F811
    rel = _write_sizing(
        repo, "--research-class", "spike", "--research-appetite", appetite, "--research-source", "repo"
    )
    capsys.readouterr()
    before = _files(repo)
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_DATA_ERROR
    assert SPIKE_MESSAGE in capsys.readouterr().err
    assert _files(repo) == before


def test_shape_never_maps_spike_to_a_tier():
    with pytest.raises(ValueError, match="/coordinator:spike"):
        research_shape.shape({"value_class": "spike", "appetite": "medium", "sources": ["repo"]})


def test_an_unmapped_class_is_refused_not_defaulted(repo, capsys):  # noqa: F811
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "repo")
    path = repo / rel
    path.write_text(path.read_text(encoding="utf-8").replace("value_class: corpus", "value_class: bogus"), encoding="utf-8")
    capsys.readouterr()
    before = _files(repo)
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_DATA_ERROR
    assert "'bogus' is not one of" in capsys.readouterr().err
    assert _files(repo) == before
