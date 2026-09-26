
from __future__ import annotations

import pytest

from coordinator_core.search.answer import LsSource, PowerShellSource, ReadSource, plan_for
from coordinator_core.search.engine import GrepSource
from coordinator_core.search.sources_powershell import ChildItemSpec, ContentSpec


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "notes.md").write_text("alpha appears here\nbeta appears here\n")
    return tmp_path


def test_recognizes_bare_cat(tree):
    plan = plan_for("cat notes.md")
    assert plan is not None
    assert isinstance(plan.source, ReadSource)
    assert plan.source.spec.kind == "cat"
    assert plan.source.spec.operands == ["notes.md"]
    assert plan.stages == []


def test_recognizes_head_with_count(tree):
    plan = plan_for("head -n 3 notes.md")
    assert plan is not None
    assert isinstance(plan.source, ReadSource)
    assert plan.source.spec.kind == "head"
    assert plan.source.spec.count == 3


def test_recognizes_tail(tree):
    plan = plan_for("tail -5 notes.md")
    assert plan is not None
    assert isinstance(plan.source, ReadSource)
    assert plan.source.spec.kind == "tail"
    assert plan.source.spec.count == 5


def test_recognizes_sed_range(tree):
    plan = plan_for("sed -n '1,2p' notes.md")
    assert plan is not None
    assert isinstance(plan.source, ReadSource)
    assert plan.source.spec.kind == "sed"
    assert plan.source.spec.start == 1
    assert plan.source.spec.end == 2


def test_recognizes_read_with_downstream_stage(tree):
    plan = plan_for("cat notes.md | wc -l")
    assert plan is not None
    assert isinstance(plan.source, ReadSource)
    assert len(plan.stages) == 1
    assert plan.stages[0].name == "wc"


def test_recognition_is_not_gated_on_grep_via_bash_shape(tree):
    """A bare `cat` is not a grep-family shape at all -- recognition must not depend
    on `_Shape.GREP_VIA_BASH` firing (C3: no new `Shape` member, no gate on that one)."""
    from coordinator_core.bash_guards._shape_classifier import Shape, classify_command

    classification = classify_command("cat notes.md")
    assert not classification.has_shape(Shape.GREP_VIA_BASH)
    assert plan_for("cat notes.md") is not None


def test_declines_when_read_is_piped_into(tree):
    assert plan_for("echo hi | cat notes.md") is None


def test_declines_on_semicolon_compound(tree):
    assert plan_for("cat notes.md ; echo done") is None


def test_declines_on_ampersand_compound(tree):
    assert plan_for("cat notes.md && echo done") is None


def test_declines_on_redirection(tree):
    assert plan_for("cat notes.md > out.txt") is None


def test_declines_on_unsupported_verb(tree):
    assert plan_for("wc -l notes.md") is None


def test_declines_on_multi_file_head(tree):
    assert plan_for("head notes.md other.txt") is None


def test_declines_empty_command():
    assert plan_for("") is None


def test_recognizes_bare_ls(tree):
    plan = plan_for("ls")
    assert plan is not None
    assert isinstance(plan.source, LsSource)
    assert plan.source.spec.directory == "."
    assert plan.source.spec.show_all is False
    assert plan.stages == []


def test_recognizes_ls_with_operand_and_show_all(tree):
    plan = plan_for("ls -a .")
    assert plan is not None
    assert isinstance(plan.source, LsSource)
    assert plan.source.spec.directory == "."
    assert plan.source.spec.show_all is True


def test_recognizes_ls_with_downstream_stage(tree):
    plan = plan_for("ls | wc -l")
    assert plan is not None
    assert isinstance(plan.source, LsSource)
    assert len(plan.stages) == 1
    assert plan.stages[0].name == "wc"


def test_declines_when_ls_is_piped_into(tree):
    assert plan_for("echo hi | ls") is None


def test_declines_ls_on_semicolon_compound(tree):
    assert plan_for("ls ; echo done") is None


def test_declines_ls_on_unsupported_flag(tree):
    assert plan_for("ls -l") is None


def test_grep_branch_plan_unchanged_bare(tree):
    plan = plan_for("grep -n alpha notes.md")
    assert plan is not None
    assert isinstance(plan.source, GrepSource)
    assert plan.source.spec.pattern == "alpha"
    assert plan.source.spec.targets == ["notes.md"]
    assert plan.source.spec.line_numbers is True
    assert plan.stages == []


def test_grep_branch_plan_unchanged_with_stage(tree):
    plan = plan_for("grep -rn alpha . | head -2")
    assert plan is not None
    assert isinstance(plan.source, GrepSource)
    assert plan.source.spec.recursive is True
    assert len(plan.stages) == 1
    assert plan.stages[0].name == "head"


def test_grep_branch_still_declines_on_upstream_feed(tree):
    assert plan_for("echo hi | grep alpha") is None


def test_grep_branch_still_declines_on_semicolon_compound(tree):
    assert plan_for("grep -n alpha notes.md ; echo done") is None


def test_bash_default_tool_name_never_recognizes_powershell_verbs(tree):
    assert plan_for("Get-Content notes.md") is None
    assert plan_for("Get-ChildItem") is None


def test_recognizes_get_content(tree):
    plan = plan_for("Get-Content notes.md", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ContentSpec)
    assert plan.source.spec.operand == "notes.md"
    assert plan.stages == []


def test_recognizes_get_content_with_tail(tree):
    plan = plan_for("Get-Content -Tail 2 notes.md", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source.spec, ContentSpec)
    assert plan.source.spec.tail_count == 2


def test_recognizes_get_childitem_bare(tree):
    plan = plan_for("Get-ChildItem", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ChildItemSpec)
    assert plan.source.spec.directory == "."
    assert plan.stages == []


def test_powershell_ls_alias_does_not_cross_wire_into_bash_ls(tree):
    plan = plan_for("ls", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ChildItemSpec)


def test_powershell_cat_alias_does_not_cross_wire_into_bash_cat(tree):
    plan = plan_for("cat notes.md", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ContentSpec)


def test_powershell_declines_when_piped_into(tree):
    assert plan_for("echo hi | Get-Content notes.md", tool_name="PowerShell") is None


def test_powershell_declines_on_unsupported_flag(tree):
    assert plan_for("Get-Content -Raw notes.md", tool_name="PowerShell") is None


def test_powershell_declines_on_unrecognized_downstream_stage(tree):
    assert plan_for("Get-Content notes.md | Measure-Object", tool_name="PowerShell") is None


def test_powershell_declines_empty_command():
    assert plan_for("", tool_name="PowerShell") is None
