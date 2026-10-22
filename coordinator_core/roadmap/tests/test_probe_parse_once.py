"""Pins the upgrader's parse_frontmatter / raw_spine_rows return shapes, the contract a shared parse must keep."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


@pytest.fixture(scope="module")
def upgrader():
    sys.path.insert(0, str(_BIN / "lib"))
    spec = importlib.util.spec_from_file_location("mise_prep_upgrade_shape", _BIN / "mise-prep-upgrade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FM = "---\ntitle: t\nstatus: draft\n---\n"


def test_raw_spine_rows_is_none_for_a_plan_with_no_spine(upgrader):
    assert upgrader.raw_spine_rows(_FM + "body\n") is None


def test_raw_spine_rows_is_empty_for_an_unparsable_spine(upgrader):
    text = _FM + "```yaml plan-tasks\n- id: [unclosed\n```\n"
    assert upgrader.raw_spine_rows(text) == []


def test_raw_spine_rows_returns_the_authored_rows(upgrader):
    text = _FM + "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  writes: [a.py]\n```\n"
    assert upgrader.raw_spine_rows(text) == [{"id": "C1", "writes": ["a.py"]}]


def test_parse_frontmatter_reports_a_plan_with_no_frontmatter(upgrader):
    assert upgrader.parse_frontmatter("no frontmatter\n") == (None, "", "no frontmatter block")


def test_parse_frontmatter_returns_the_mapping_and_body(upgrader):
    fm, body, error = upgrader.parse_frontmatter(_FM + "body\n")
    assert (fm, body, error) == ({"title": "t", "status": "draft"}, "body\n", None)


def _counting_yaml_load(monkeypatch):
    import yaml

    calls = []
    real = yaml.load

    def counting(stream, Loader=None):
        calls.append(stream)
        return real(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", counting)
    return calls


def test_one_upgrade_report_loads_frontmatter_and_spine_once_each(upgrader, monkeypatch, tmp_path):
    text = "---\ntitle: load-once-unique\nstatus: draft\n---\nload-once-marker\n## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: load-once\n```\n"
    calls = _counting_yaml_load(monkeypatch)
    plan = tmp_path / "2026-01-01-p.md"
    upgrader.upgrade_plan(plan, check=True, text=text)
    upgrader.upgrade_plan(plan, check=True, text=text)
    assert len(calls) == 2


def test_a_spine_with_a_nested_fence_returns_every_authored_row(upgrader):
    text = (
        _FM
        + "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  body: |\n    ```py\n    x = 1\n    ```\n- id: C2\n  writes: [b.py]\n```\n"
    )
    rows = upgrader.raw_spine_rows(text)
    assert [r["id"] for r in rows] == ["C1", "C2"]


def test_a_second_parse_after_an_upgrade_check_equals_a_fresh_load(upgrader):
    import yaml

    fm_text = "title: shared-memo-pin\nstatus: draft\nprime_exit_criterion:\n  statement: s\n"
    text = f"---\n{fm_text}---\nbody\n"
    first, _, _ = upgrader.parse_frontmatter(text)
    upgrader.upgrade_plan(Path("2026-01-01-q.md"), check=True, text=text)
    second, _, _ = upgrader.parse_frontmatter(text)
    assert second == yaml.safe_load(fm_text)
    assert second is first
