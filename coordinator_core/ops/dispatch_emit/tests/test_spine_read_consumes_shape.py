"""A non-string `reads:`/`consumes:` entry is a named InvalidFieldTypeError, not an unhashable TypeError."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import InvalidFieldTypeError, read_spine


def _plan(tmp_path, field: str):
    path = tmp_path / "plan.md"
    path.write_text(
        "# fixture plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: t\n  surface: s\n"
        f"  {field}:\n    - {{chunk: C0, artifact: a.py}}\n```\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("field", ["consumes", "reads"])
def test_dict_entry_names_row_and_field(tmp_path, field):
    with pytest.raises(InvalidFieldTypeError) as info:
        read_spine(_plan(tmp_path, field))
    message = str(info.value)
    assert "'C1'" in message and f"{field}:" in message and "artifact" in message


def test_valid_string_entries_parse_unchanged(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: t\n  surface: s\n  consumes:\n    - a.py\n```\n",
        encoding="utf-8",
    )
    assert read_spine(path)[0].reads == ["a.py"]
