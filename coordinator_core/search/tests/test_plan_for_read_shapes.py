"""Recognition tests for `answer.plan_for`'s read-shape branch (C3), plus an explicit
regression that the pre-existing grep branch's plans are unchanged.

Purpose: `plan_for` now recognizes two source classes -- grep-family invocations
(unchanged, AC4) and `cat`/`head`/`tail`/`sed -n` read invocations (C1's parser, wired
here). This file asserts recognition/decline AT THE `plan_for` LEVEL (what `AnswerPlan`
gets built, and with what `Source`), not end-to-end rendered text -- that differential
coverage already lives in `test_answer_differential.py` (grep) and
`test_sources_read.py` (read parsing).

PowerShell recognition (C10b) is covered at the bottom of this file, same level: what
`plan_for(cmd, tool_name="PowerShell")` builds, keyed off `tool_name` rather than off
`cmd`'s own basename table. Byte-fidelity coverage against a real PowerShell host is
`test_powershell_shapes_differential.py`'s job, not this file's.

Negative-spec:
  - Does NOT re-assert the grep branch's differential correctness -- only that
    `plan_for` still recognizes it and builds a `GrepSource` (AC4's own oracle covers
    output fidelity).
  - Does NOT test rendering (`_render`/`answer`) -- that is `test_answer_differential.py`
    and any read-specific rendering coverage, out of this file's scope.
  - Does NOT re-assert PowerShell byte-fidelity here -- see
    `test_powershell_shapes_differential.py` for that oracle.
"""

from __future__ import annotations

import pytest

from coordinator_core.search.answer import LsSource, PowerShellSource, ReadSource, plan_for
from coordinator_core.search.engine import GrepSource
from coordinator_core.search.sources_powershell import ChildItemSpec, ContentSpec


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "notes.md").write_text("alpha appears here\nbeta appears here\n")
    return tmp_path


# --------------------------------------------------------------------- recognition


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


# -------------------------------------------------------------------------- decline


def test_declines_when_read_is_piped_into(tree):
    """`<cmd> | cat FILE` -- `cat` still names its own operand here, but the
    structural guard is segment-position-based (piped_into on the first segment),
    mirroring the grep branch's identical refusal."""
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


# ------------------------------------------------------------------------------ ls


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


# ------------------------------------------------------------- grep branch unchanged


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
    """`<cmd> | grep` where the upstream is NOT itself a recognizable read source
    still declines at the grep branch -- this regression is scoped to a plain
    upstream, not `cat | grep`, which is now a legitimate read+filter-stage
    combination (see module docstring; `test_answer_differential.py`'s identically
    named case pre-dates read-source recognition and is now stale for that one
    input, not for this one)."""
    assert plan_for("echo hi | grep alpha") is None


def test_grep_branch_still_declines_on_semicolon_compound(tree):
    assert plan_for("grep -n alpha notes.md ; echo done") is None


# ------------------------------------------------------------------- powershell


def test_bash_default_tool_name_never_recognizes_powershell_verbs(tree):
    """`tool_name` defaults to `"Bash"` (backward compatible, every existing
    caller unchanged) -- a `Get-Content`/`Get-ChildItem` token stream is not a
    recognized bash verb at all under that default, so it declines rather than
    silently mis-parsing."""
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
    """`ls` is a live `Get-ChildItem` alias under PowerShell -- it must build a
    `PowerShellSource`/`ChildItemSpec`, never an `LsSource`/`sources_listdir.LsSpec`
    (module docstring negative-spec: cross-wiring the bash `ls` grammar onto a
    PowerShell `ls` token stream is a confidently-wrong parse, not merely a
    less-precise one)."""
    plan = plan_for("ls", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ChildItemSpec)


def test_powershell_cat_alias_does_not_cross_wire_into_bash_cat(tree):
    """`cat` is a live `Get-Content` alias under PowerShell -- same discipline as
    the `ls` case above, mirrored for the read verb."""
    plan = plan_for("cat notes.md", tool_name="PowerShell")
    assert plan is not None
    assert isinstance(plan.source, PowerShellSource)
    assert isinstance(plan.source.spec, ContentSpec)


def test_powershell_declines_when_piped_into(tree):
    assert plan_for("echo hi | Get-Content notes.md", tool_name="PowerShell") is None


def test_powershell_declines_on_unsupported_flag(tree):
    assert plan_for("Get-Content -Raw notes.md", tool_name="PowerShell") is None


def test_powershell_declines_on_unrecognized_downstream_stage(tree):
    """No PowerShell stage vocabulary exists (module docstring) -- a downstream
    segment declines the whole plan via `build_stage`'s bash-verb-keyed lookup,
    exactly as an unabsorbable bash stage would."""
    assert plan_for("Get-Content notes.md | Measure-Object", tool_name="PowerShell") is None


def test_powershell_declines_empty_command():
    assert plan_for("", tool_name="PowerShell") is None
