import pytest

from coordinator_core.commenting.added import added_comments


def _texts(path, before, after):
    return [c.text for c in added_comments(path, before, after)]


def test_untagged_python_comment_is_added():
    out = added_comments("pkg/mod.py", "x = 1\n", "x = 1\n# keep this because the test was flaky\n")
    assert [(c.line, c.kind) for c in out] == [(2, "comment")]
    assert out[0].path == "pkg/mod.py"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "line",
    [
        "# noqa: E501",
        "# type: ignore",
        "# pylint: disable=foo",
        "# SPDX-License-Identifier: MIT",
        "# A-HARDCODED-DATA-ROOT-IS-NOT-EXEMPT",
        "# engine-gap: producer=claude_klabauter:x.y",
    ],
)
def test_allowed_python_comments_raise_nothing(line, eol):
    after = f"x = 1{eol}{line}{eol}"
    assert added_comments("pkg/mod.py", f"x = 1{eol}", after) == []


def test_shebang_raises_nothing():
    assert added_comments("bin/run.sh", "", "#!/usr/bin/env bash\necho hi\n") == []


def test_eslint_and_uproperty_raise_nothing():
    after = "// eslint-disable-next-line no-console\nconsole.log(1);\n// UPROPERTY(EditAnywhere)\n"
    assert added_comments("src/a.ts", "console.log(1);\n", after) == []


def test_clike_untagged_line_and_block_flagged_with_crlf():
    after = "int a;\r\n// because reasons\r\n/* also\r\n   because */\r\nint b;\r\n"
    out = added_comments("src/a.cpp", "int a;\r\nint b;\r\n", after)
    assert [(c.line, c.text) for c in out] == [(2, "// because reasons"), (3, "/* also\r\n   because */")]


@pytest.mark.parametrize(
    "path,line",
    [
        ("pkg/mod.py", "# trap: the loader swallows ImportError"),
        ("pkg/mod.py", "#trap: no space"),
        ("src/a.ts", "// invariant: sorted ascending"),
        ("src/a.ts", "/* external-constraint: vendor API pads to 8 */"),
        ("conf/a.yaml", "# trap: anchors merge shallowly"),
    ],
)
def test_tagged_comments_raise_nothing(path, line):
    assert added_comments(path, "", f"{line}\nx\n") == []


@pytest.mark.parametrize("line", ["# Trap: x", "# TRAP: x", "# note trap: x", "# traps: x", "# trap x", "# invariants: x"])
def test_tag_is_case_sensitive_leading_and_colon_terminated(line):
    assert len(added_comments("pkg/mod.py", "", f"x = 1\n{line}\n")) == 1


def test_tag_block_continuation_lines_inherit_the_tag():
    after = "x = 1\n# trap: first line\n# second line of the same note\n\n# stray\n"
    out = added_comments("pkg/mod.py", "x = 1\n", after)
    assert [c.text for c in out] == ["# stray"]


def test_inline_tagged_comment_does_not_cover_next_line():
    after = "x = 1  # trap: inline\n# next\n"
    assert _texts("pkg/mod.py", "x = 1\n", after) == ["# next"]


def test_pre_existing_comment_never_flagged_and_move_is_not_new():
    before = "# old note\nx = 1\n\ndef f():\n    pass\n"
    after = "x = 1\n\ndef f():\n    pass\n\n# old note\n"
    assert added_comments("pkg/mod.py", before, after) == []


def test_duplicate_comment_beyond_before_count_is_added():
    before = "# dup\nx = 1\n"
    after = "# dup\nx = 1\n# dup\n"
    assert [c.line for c in added_comments("pkg/mod.py", before, after)] == [3]


def test_whitespace_and_marker_normalised_for_identity():
    before = "#   spaced   out\n"
    after = "#spaced out\n"
    assert added_comments("pkg/mod.py", before, after) == []


def test_one_line_docstring_allowed_three_line_flagged():
    one = 'def f():\n    """Does a thing."""\n    return 1\n'
    three = 'def f():\n    """Does a thing.\n\n    Because reasons.\n    """\n    return 1\n'
    assert added_comments("pkg/mod.py", "def f():\n    return 1\n", one) == []
    out = added_comments("pkg/mod.py", "def f():\n    return 1\n", three)
    assert [(c.kind, c.line) for c in out] == [("docstring", 2)]


def test_multiline_docstring_with_dr_backlink_or_tag_allowed():
    dr = 'def f():\n    """Op.\n\n    DR-218 governs this op.\n    """\n'
    tag = 'def f():\n    """trap: first line\n    more\n    """\n'
    assert added_comments("pkg/mod.py", "def f():\n    pass\n", dr) == []
    assert added_comments("pkg/mod.py", "def f():\n    pass\n", tag) == []


def test_multiline_docstring_crlf_flagged():
    three = 'def f():\r\n    """A.\r\n\r\n    B.\r\n    """\r\n    return 1\r\n'
    assert len(added_comments("pkg/mod.py", "def f():\r\n    return 1\r\n", three)) == 1


def test_unchanged_multiline_docstring_not_flagged():
    src = 'def f():\n    """A.\n\n    B.\n    """\n'
    assert added_comments("pkg/mod.py", src, src + "y = 1\n") == []


def test_markdown_and_unknown_suffix_never_scanned():
    assert added_comments("docs/a.md", "", "# heading\n<!-- c -->\n") == []
    assert added_comments("data/a.txt", "", "# c\n") == []
    assert added_comments("Makefile", "", "# c\n") == []


def test_exempt_and_fixture_paths_return_nothing():
    assert added_comments("coordinator_core/commenting/x.py", "", "# c\n") == []


def test_generated_header_returns_nothing():
    after = "# Code generated by protoc. DO NOT EDIT.\nx = 1\n# more\n"
    assert added_comments("pkg/gen.py", "", after) == []
    late = "\n" * 6 + "# generated by hand\n# more\n"
    assert len(added_comments("pkg/gen.py", "", late)) == 2


def test_lexer_failure_returns_nothing():
    assert added_comments("pkg/mod.py", "x = 1\n", "def (:\n# c\n") == []
    assert added_comments("pkg/mod.py", "def (:\n", "x = 1\n# c\n") == []


def test_hash_languages():
    assert _texts("bin/a.sh", "", "echo hi # why\n") == ["# why"]
    assert _texts("conf/a.toml", "", 'k = "a # not"\n# real\n') == ["# real"]
    assert _texts("conf/a.yml", "", "a: 1\n# real\n") == ["# real"]


def test_python_hash_inside_string_is_not_a_comment():
    assert added_comments("pkg/mod.py", "", 's = "# not a comment"\n') == []
