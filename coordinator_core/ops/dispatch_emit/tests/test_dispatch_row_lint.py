"""Unit tests for the pure dispatch-row lints."""
from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import dispatch_row_lint as lint
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow

C7_WRITES = [
    "coordinator/tests/test_session_state_roundtrip.py",
    "coordinator/docs/wiki/session-state-contract.md",
    "state/roadmap/pcore/STUB-NUMBERING.md",
]
C7_TITLE = "round-trip test + wiki + register pcore-13 in STUB-NUMBERING"
C1_WRITES = [
    "coordinator/bin/session_state_writer.py",
    "coordinator/schemas/session-state.schema.json",
    "coordinator/tests/test_session_state_writer.py",
]
C1_TITLE = "session-state writer + schema + test"


def _row(rid, title, writes, body=""):
    return EmitterRow(id=rid, title=title, surface="", writes=writes, reads=[], depends_on=[], body=body)


def test_c7_fires_with_three_targets():
    offer = lint.disjoint_write_offer(_row("C7", C7_TITLE, C7_WRITES))
    assert offer == (
        "row C7 writes 3 disjoint targets and its title conjoins deliverables: "
        'split it into 3 rows, or add a "Coherence: <reason>" line to its body.'
    )


def test_c1_is_quiet_though_title_conjoins():
    assert lint.title_conjoins(C1_TITLE)
    assert len(lint.write_groups(C1_WRITES)) == 1
    assert lint.disjoint_write_offer(_row("C1", C1_TITLE, C1_WRITES)) is None


def test_coherence_line_quiets_offer():
    body = "intro\n   Coherence: one rollout\nmore"
    assert lint.disjoint_write_offer(_row("C7", C7_TITLE, C7_WRITES, body)) is None


def test_coherence_mid_line_does_not_quiet():
    body = "see Coherence: not at line start"
    assert lint.disjoint_write_offer(_row("C7", C7_TITLE, C7_WRITES, body)) is not None


def test_single_write_conjoined_title_is_quiet():
    assert lint.disjoint_write_offer(_row("C9", C7_TITLE, ["a/one.md"])) is None


def test_disjoint_writes_plain_title_is_quiet():
    assert lint.disjoint_write_offer(_row("C9", "Tidy things", C7_WRITES)) is None


def test_directory_shaped_write_yields_no_offer():
    assert lint.disjoint_write_offer(_row("C9", C7_TITLE, ["a/dir/", "b/x.md"])) is None


def test_prefix_requires_whole_token():
    assert len(lint.write_groups(["src/plan.py", "src/planner.py"])) == 2
    assert len(lint.write_groups(["src/session_state.py", "src/session_state_writer.py"])) == 1


def test_affinity_strips_every_suffix_test_markers_and_dashes():
    assert len(lint.write_groups(["a/x.schema.json", "b/test_x.py", "c/X_test.py"])) == 1
    assert len(lint.write_groups(["a/my-thing.md", "b/my_thing.py"])) == 1


def test_ancestor_paths_are_coherent():
    assert len(lint.write_groups(["docs/wiki", "docs/wiki/zzz.md"])) == 1
    assert len(lint.write_groups(["docs/wikix", "docs/wiki/zzz.md"])) == 2


def test_write_groups_preserves_first_seen_order_and_dedupes():
    groups = lint.write_groups(["b/zeta.md", "a/alpha.md", "b/zeta.md"])
    assert groups == [["b/zeta.md"], ["a/alpha.md"]]


@pytest.mark.parametrize(
    "title",
    ["a + b", "a & b", "Fix X And Y", "one, two, three", "one, two, and three"],
)
def test_conjoin_triggers(title):
    assert lint.title_conjoins(title)


@pytest.mark.parametrize(
    "title",
    ["Handle standalone errors", "Add `f(a, b, c)` helper", "Add f(a, b, c) helper", "one, two", "Plain title"],
)
def test_conjoin_negatives(title):
    assert not lint.title_conjoins(title)


def test_comma_items_empty_do_not_count():
    assert not lint.title_conjoins("one,, two")


# --- exercising ---------------------------------------------------------------------------


def test_filename_hit_at_boundary():
    pairs = [("tests/test_a.py", 'load("bin/foo.py")'), ("tests/test_b.py", "foo.py")]
    assert lint.tests_exercising("bin/foo.py", pairs) == ["tests/test_a.py", "tests/test_b.py"]


def test_filename_inside_longer_name_is_not_a_hit():
    pairs = [("tests/test_a.py", "barfoo.py"), ("tests/test_b.py", "my-foo.py"), ("tests/test_c.py", "x_foo.py")]
    assert lint.tests_exercising("bin/foo.py", pairs) == []


def test_from_dotted_import_hit():
    pairs = [("tests/test_a.py", "from x.stem import thing"), ("tests/test_b.py", "import x.stem")]
    assert lint.tests_exercising("x/stem.py", pairs) == ["tests/test_a.py", "tests/test_b.py"]


def test_from_parent_import_stem_hit():
    pairs = [("tests/test_a.py", "from x import stem, other")]
    assert lint.tests_exercising("x/stem.py", pairs) == ["tests/test_a.py"]


def test_bare_word_is_not_a_hit():
    pairs = [("tests/test_a.py", "run the cli and op"), ("tests/test_b.py", "# cli")]
    assert lint.tests_exercising("pkg/cli.py", pairs) == []


def test_output_sorted_and_unique():
    pairs = [("t/z.py", "foo.py"), ("t/a.py", "foo.py"), ("t/z.py", "see foo.py")]
    assert lint.tests_exercising("foo.py", pairs) == ["t/a.py", "t/z.py"]


def test_non_py_source_has_no_tests():
    assert lint.tests_exercising("docs/foo.md", [("t/a.py", "foo.md")]) == []


def test_exercising_pattern_none_without_py_sources():
    assert lint.exercising_pattern([]) is None
    assert lint.exercising_pattern(["a/b.md"]) is None


def test_exercising_pattern_uses_posix_classes_only():
    pat = lint.exercising_pattern(["bin/foo.py", "pkg/bar-baz.py"])
    assert "[^[:alnum:]_-]" in pat
    assert "\\b" not in pat and "\\w" not in pat
    assert "foo\\.py" in pat and "bar-baz\\.py" in pat


def _py_re(ere: str) -> "re.Pattern[str]":
    return re.compile(ere.replace("[:alnum:]", "a-zA-Z0-9"))


SAMPLES = [
    'load("bin/foo.py")',
    "foo.py",
    "barfoo.py",
    "my-foo.py",
    "x_foo.py",
    "(foo.py)",
    "a/foo.py:3",
    "foo.pyx",
    "nothing here",
]


def test_filename_ere_matches_lookbehind_reference_form():
    ere = _py_re(lint.exercising_pattern(["bin/foo.py"]))
    reference = re.compile(r"(?<![\w-])foo\.py")
    for line in SAMPLES:
        assert bool(ere.search(line)) == bool(reference.search(line)), line


def test_pattern_also_finds_import_lines():
    ere = _py_re(lint.exercising_pattern(["x/stem.py"]))
    assert ere.search("from x.stem import a")
    assert ere.search("  from x import stem")
    assert not ere.search("stem")


# --- section ------------------------------------------------------------------------------


def test_section_none_when_empty():
    assert lint.tests_exercising_section(["a.py"], [], "p") is None


def test_section_groups_labels_and_design_rule():
    out = lint.tests_exercising_section(
        ["src/a.py", "tests/test_in.py"], ["tests/test_out.py", "tests/test_in.py"], "pat"
    )
    assert out.startswith(lint.SECTION_HEAD)
    assert "In your writes: run them:\n- `tests/test_in.py`" in out
    assert "Outside your writes: run, do not edit:\n- `tests/test_out.py`" in out
    assert "PARTIAL" in out and "not only your ACs" in out
    assert "takes its root as a parameter" in out
    assert "tests in total" not in out


def test_section_omits_empty_group_label():
    out = lint.tests_exercising_section(["src/a.py"], ["tests/test_out.py"], "pat")
    assert "In your writes" not in out
    assert "Outside your writes" in out


def test_section_caps_at_25_with_total_and_literal_query():
    tests = [f"tests/test_{i:03d}.py" for i in range(30)]
    out = lint.tests_exercising_section([], tests, "it's|x")
    assert sum(1 for line in out.splitlines() if line.startswith("- `")) == 25
    assert "30 tests in total" in out
    assert "`git grep -lE 'it'\\''s|x' -- '*test_*.py' '*_test.py' '*/tests/*.py'`" in out
    assert "tests/test_029.py" not in out


def test_section_exactly_25_has_no_query():
    tests = [f"tests/test_{i:03d}.py" for i in range(25)]
    out = lint.tests_exercising_section([], tests, "p")
    assert "git grep" not in out


def test_section_caps_after_in_writes_first():
    inside = [f"t/in_{i}.py" for i in range(3)]
    outside = [f"t/out_{i:02d}.py" for i in range(30)]
    out = lint.tests_exercising_section(inside, inside + outside, "p")
    assert all(f"`{t}`" in out for t in inside)
