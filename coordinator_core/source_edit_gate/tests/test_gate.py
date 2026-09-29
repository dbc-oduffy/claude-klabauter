"""Tests for `coordinator_core.source_edit_gate` -- the computed source-edit
tier: `selection.select_test_files`, `runner.detect_runner`, `gate._new_failures`,
and `gate.run_gate`'s base/candidate/callback sequence + falsifier end-to-end.

Unit-level tests (selection, runner detection, the regression rule) run
in-process and stay in the fast tier. The end-to-end tests build a tiny
fixture repo on disk and spawn real `python3 -m pytest` processes, marked
`spawns_process` + `cadence` per the fleet's standing spawn-policy gates.

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md (gate
redesign -- computed tier, no per-repo declaration).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_init(repo: Path) -> None:
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(args, cwd=repo, check=True, creationflags=_NO_CONSOLE)

from coordinator_core.source_edit_gate.gate import GateResult, _new_failures, run_gate
from coordinator_core.source_edit_gate.gate_report import parse_junitxml, parse_vitest_json
from coordinator_core.source_edit_gate.runner import detect_runner, find_runner_root
from coordinator_core.source_edit_gate.selection import select_test_files

# --------------------------------------------------------------------------
# junitxml / vitest-json parsing (moved to gate_report, same contract)
# --------------------------------------------------------------------------


def test_parse_junitxml_passed_failed_skipped(tmp_path):
    xml_path = tmp_path / "report.xml"
    xml_path.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuites>
              <testsuite name="pytest">
                <testcase classname="tests.test_a" name="test_pass" time="0.01"/>
                <testcase classname="tests.test_a" name="test_fail" time="0.01">
                  <failure message="boom">assertion error</failure>
                </testcase>
                <testcase classname="tests.test_a" name="test_skip" time="0.0">
                  <skipped message="skipped"/>
                </testcase>
              </testsuite>
            </testsuites>
            """
        ),
        encoding="utf-8",
    )

    report = parse_junitxml(xml_path)

    assert report["tests.test_a::test_pass"] == "passed"
    assert report["tests.test_a::test_fail"] == "failed"
    assert report["tests.test_a::test_skip"] == "skipped"


def test_parse_junitxml_missing_file_returns_none_not_empty_report(tmp_path):
    assert parse_junitxml(tmp_path / "does-not-exist.xml") is None


def test_parse_vitest_json_passed_failed_skipped():
    raw = """
    {
      "testResults": [
        {"name": "tests/a.test.js", "assertionResults": [
          {"fullName": "a passes", "status": "passed"},
          {"fullName": "a fails", "status": "failed"}
        ]}
      ]
    }
    """
    report = parse_vitest_json(raw)
    assert report["tests/a.test.js::a passes"] == "passed"
    assert report["tests/a.test.js::a fails"] == "failed"


def test_parse_vitest_json_empty_string_returns_none():
    assert parse_vitest_json("") is None


# --------------------------------------------------------------------------
# Regression rule (_new_failures)
# --------------------------------------------------------------------------


def test_new_failures_detects_a_genuinely_new_failure():
    base = {"t::a": "passed", "t::b": "passed"}
    candidate = {"t::a": "passed", "t::b": "failed"}
    assert _new_failures(base, candidate) == ("t::b",)


def test_new_failures_ignores_base_red_candidate_red_subset():
    base = {"t::a": "failed", "t::b": "passed"}
    candidate = {"t::a": "failed", "t::b": "passed"}
    assert _new_failures(base, candidate) == ()


def test_new_failures_treats_vanished_node_id_as_a_regression():
    assert _new_failures({"t::a": "passed"}, {}) == ("t::a",)


def test_new_failures_treats_turned_into_skip_as_a_regression():
    assert _new_failures({"t::a": "passed"}, {"t::a": "skipped"}) == ("t::a",)


def test_new_failures_empty_when_nothing_changed():
    assert _new_failures({"t::a": "passed"}, {"t::a": "passed"}) == ()


# --------------------------------------------------------------------------
# Selection: import hit, path-literal hit, dir-literal hit, no hit
# --------------------------------------------------------------------------


def test_selection_python_import_hit(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text("def f(): return 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import f\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    all_files = ["pkg/__init__.py", "pkg/mod.py", "tests/test_mod.py"]

    selected = select_test_files(str(tmp_path), ["pkg/mod.py"], all_files)

    assert selected == ["tests/test_mod.py"]


def test_selection_path_literal_hit(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "schema.json").write_text("{}", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_schema_hash.py").write_text(
        'PATH = "data/schema.json"\n\ndef test_hash():\n    assert PATH\n', encoding="utf-8"
    )
    all_files = ["data/schema.json", "tests/test_schema_hash.py"]

    selected = select_test_files(str(tmp_path), ["data/schema.json"], all_files)

    assert selected == ["tests/test_schema_hash.py"]


def test_selection_dir_literal_hit(tmp_path):
    (tmp_path / "vendor" / "lib").mkdir(parents=True)
    (tmp_path / "vendor" / "lib" / "header.h").write_text("//header\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_vendor_dir.py").write_text(
        'DIR = "vendor/lib"\n\ndef test_dir():\n    assert DIR\n', encoding="utf-8"
    )
    all_files = ["vendor/lib/header.h", "tests/test_vendor_dir.py"]

    selected = select_test_files(str(tmp_path), ["vendor/lib/header.h"], all_files)

    assert selected == ["tests/test_vendor_dir.py"]


def test_selection_no_hit_when_unrelated(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert True\n", encoding="utf-8"
    )
    all_files = ["pkg/mod.py", "tests/test_unrelated.py"]

    selected = select_test_files(str(tmp_path), ["pkg/mod.py"], all_files)

    assert selected == []


def test_selection_js_relative_import_hit(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "widget.ts").write_text("export const widget = 1;\n", encoding="utf-8")
    tests_dir = tmp_path / "src" / "__tests__"
    tests_dir.mkdir()
    (tests_dir / "widget.test.ts").write_text(
        'import { widget } from "../widget";\ntest("x", () => { expect(widget).toBe(1); });\n',
        encoding="utf-8",
    )
    all_files = ["src/widget.ts", "src/__tests__/widget.test.ts"]

    selected = select_test_files(str(tmp_path), ["src/widget.ts"], all_files)

    assert selected == ["src/__tests__/widget.test.ts"]


def test_selection_conftest_fixture_hit_selects_every_test_in_its_subtree(tmp_path):
    """A test that consumes a fixture defined in `conftest.py` neither imports
    the edited module nor mentions its path literally -- only the conftest
    fixture does. The hit must be detected on the conftest itself and
    propagated to every test file in the conftest's directory subtree."""
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "header.h").write_text("//v1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(
        'import pytest\n\n'
        '@pytest.fixture\n'
        'def pinned_hash():\n'
        '    return open("vendor/header.h").read()\n',
        encoding="utf-8",
    )
    (tests_dir / "test_uses_fixture.py").write_text(
        "def test_it(pinned_hash):\n    assert pinned_hash\n", encoding="utf-8"
    )
    sub_dir = tests_dir / "sub"
    sub_dir.mkdir()
    (sub_dir / "test_nested.py").write_text(
        "def test_nested(pinned_hash):\n    assert pinned_hash\n", encoding="utf-8"
    )
    (tests_dir / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert True\n", encoding="utf-8"
    )
    all_files = [
        "vendor/header.h",
        "tests/conftest.py",
        "tests/test_uses_fixture.py",
        "tests/sub/test_nested.py",
        "tests/test_unrelated.py",
    ]

    selected = select_test_files(str(tmp_path), ["vendor/header.h"], all_files)

    # A conftest.py hit selects every test file in its directory subtree --
    # including one that never touches the fixture -- deliberately: over-
    # selection is the safe direction, and pytest conftest visibility
    # cascades to every test below it regardless of which fixtures a given
    # test actually requests.
    assert set(selected) == {
        "tests/test_uses_fixture.py",
        "tests/sub/test_nested.py",
        "tests/test_unrelated.py",
    }


def test_selection_colocated_helper_module_hit_selects_its_directory(tmp_path):
    """A non-test helper module colocated with test files (imported by them,
    but itself the one that hits an edited path literal) also needs the
    import/literal scan, not just direct test files."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "schema.json").write_text("{}", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "hash_utils.py").write_text(
        'SCHEMA_PATH = "data/schema.json"\n', encoding="utf-8"
    )
    (tests_dir / "test_via_helper.py").write_text(
        "from tests.hash_utils import SCHEMA_PATH\n\ndef test_it():\n    assert SCHEMA_PATH\n",
        encoding="utf-8",
    )
    all_files = ["data/schema.json", "tests/hash_utils.py", "tests/test_via_helper.py"]

    selected = select_test_files(str(tmp_path), ["data/schema.json"], all_files)

    assert selected == ["tests/test_via_helper.py"]


def test_selection_js_file_in_python_rooted_dir_not_selected_into_pytest(tmp_path):
    """A `.test.js` file sitting under a directory whose only detectable
    runner marker is pytest's own `conftest.py` (no vitest marker anywhere)
    must not be selected -- selecting it would land it in the pytest group,
    which cannot collect it."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "widget.test.js").write_text(
        "test('x', () => { require('../pkg/mod'); });\n", encoding="utf-8"
    )
    all_files = ["conftest.py", "pkg/mod.py", "tests/widget.test.js"]

    selected = select_test_files(str(tmp_path), ["pkg/mod.py"], all_files)

    assert selected == []


# --------------------------------------------------------------------------
# Change classification: comment-only vs docstring/string vs code
# --------------------------------------------------------------------------


def _widely_imported_helper_repo(tmp_path):
    """A shared helper module at the repo root (so its path-literal needle is
    just its bare filename, never colliding with the dotted-module text an
    importer's own `import` statement necessarily contains) with two test
    files: one that merely reads its path as a string literal (a
    literal-only reader) and one that actually `import`s it (an importer)."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    helper = tmp_path / "helper.py"
    helper.write_text(
        '"""helper docstring"""\n\n# a comment\ndef util():\n    return 1\n',
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_literal_reader.py").write_text(
        'from pathlib import Path\n\n'
        'def test_it():\n'
        '    assert Path("helper.py").read_text()\n',
        encoding="utf-8",
    )
    (tests_dir / "test_importer.py").write_text(
        "import helper\n\ndef test_it():\n    assert helper.util() == 1\n",
        encoding="utf-8",
    )
    all_files = ["conftest.py", "helper.py", "tests/test_literal_reader.py", "tests/test_importer.py"]
    return helper, all_files


def test_selection_comment_only_edit_selects_only_literal_readers(tmp_path):
    helper, all_files = _widely_imported_helper_repo(tmp_path)
    original = helper.read_text(encoding="utf-8")
    edited = original.replace("# a comment", "# a different comment")
    helper.write_text(edited, encoding="utf-8")

    selected = select_test_files(
        str(tmp_path), ["tests/helper.py"], all_files,
        file_texts={"tests/helper.py": (original, edited)},
    )

    assert selected == ["tests/test_literal_reader.py"]


def test_selection_module_docstring_edit_with_no_doc_reader_selects_only_literal_readers(tmp_path):
    """A module docstring edit is `"doc-only"`. Neither the edited module
    (no `__doc__`/argparse/pydantic marker) nor the importing test
    (no `__doc__`/`getdoc`/`schema`/etc marker) reads the docstring at
    runtime, so the importer must NOT be selected -- only the literal
    reader is (the same narrowing a `"comment-only"` edit gets)."""
    helper, all_files = _widely_imported_helper_repo(tmp_path)
    original = helper.read_text(encoding="utf-8")
    edited = original.replace('"""helper docstring"""', '"""a different docstring"""')
    helper.write_text(edited, encoding="utf-8")

    selected = select_test_files(
        str(tmp_path), ["tests/helper.py"], all_files,
        file_texts={"tests/helper.py": (original, edited)},
    )

    assert selected == ["tests/test_literal_reader.py"]


def test_selection_docstring_edit_self_reader_selects_importers_too(tmp_path):
    """The edited module itself reads `__doc__` (e.g. `description=__doc__`
    on an argparse parser) -- the import rule applies, same as a `"code"`
    or `"string"` edit."""
    helper, all_files = _widely_imported_helper_repo(tmp_path)
    original = helper.read_text(encoding="utf-8")
    original = original.replace(
        "def util():\n    return 1\n",
        "def util():\n    return 1\n\nHELP = __doc__\n",
    )
    helper.write_text(original, encoding="utf-8")
    edited = original.replace('"""helper docstring"""', '"""a different docstring"""')
    helper.write_text(edited, encoding="utf-8")

    selected = select_test_files(
        str(tmp_path), ["tests/helper.py"], all_files,
        file_texts={"tests/helper.py": (original, edited)},
    )

    assert set(selected) == {"tests/test_literal_reader.py", "tests/test_importer.py"}


def test_selection_pydantic_class_docstring_edit_selects_importers(tmp_path):
    """A pydantic `BaseModel` class docstring becomes a JSON-schema
    description at runtime -- the `_DOC_SELF_READER_MARKERS` heuristic
    treats any file with a `BaseModel` class as a self-reader, so the
    import rule applies to a class-docstring edit inside it."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    model = tmp_path / "model.py"
    original = (
        "from pydantic import BaseModel\n\n"
        "class Widget(BaseModel):\n"
        '    """widget docstring"""\n\n'
        "    name: str\n"
    )
    model.write_text(original, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_importer.py").write_text(
        "from model import Widget\n\ndef test_it():\n    assert Widget(name='x')\n",
        encoding="utf-8",
    )
    all_files = ["conftest.py", "model.py", "tests/test_importer.py"]
    edited = original.replace('"""widget docstring"""', '"""a different docstring"""')
    model.write_text(edited, encoding="utf-8")

    selected = select_test_files(
        str(tmp_path), ["model.py"], all_files,
        file_texts={"model.py": (original, edited)},
    )

    assert selected == ["tests/test_importer.py"]


def test_selection_docstring_edit_importer_referencing_doc_marker_is_selected(tmp_path):
    """The edited module itself is not a self-reader, but the importing
    test references `__doc__` on the imported object -- the conditional
    (b) path selects it even though (a) does not apply."""
    helper, all_files = _widely_imported_helper_repo(tmp_path)
    original = helper.read_text(encoding="utf-8")
    edited = original.replace('"""helper docstring"""', '"""a different docstring"""')
    helper.write_text(edited, encoding="utf-8")
    test_importer = tmp_path / "tests" / "test_importer.py"
    test_importer.write_text(
        "import helper\n\ndef test_it():\n    assert helper.__doc__\n",
        encoding="utf-8",
    )

    selected = select_test_files(
        str(tmp_path), ["tests/helper.py"], all_files,
        file_texts={"tests/helper.py": (original, edited)},
    )

    assert set(selected) == {"tests/test_literal_reader.py", "tests/test_importer.py"}


def test_selection_code_edit_selects_importers_too(tmp_path):
    helper, all_files = _widely_imported_helper_repo(tmp_path)
    original = helper.read_text(encoding="utf-8")
    edited = original.replace("return 1", "return 2")
    helper.write_text(edited, encoding="utf-8")

    selected = select_test_files(
        str(tmp_path), ["tests/helper.py"], all_files,
        file_texts={"tests/helper.py": (original, edited)},
    )

    assert set(selected) == {"tests/test_literal_reader.py", "tests/test_importer.py"}


def test_classify_edit_malformed_python_classifies_as_code_not_a_crash():
    """An unterminated triple-quoted string raises `tokenize.TokenError`
    inside `_python_token_stream` -- the classifier must catch it and fall
    back to `"code"`, not propagate the exception up through
    `classify_edit`/`select_test_files`/`run_gate`."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "x = 1\n"
    edited = 'x = """unterminated\n'

    assert classify_edit(original, edited, "mod.py") == "code"


def test_classify_edit_js_pure_comment_edit_is_comment_only():
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "// old comment\nconst x = 1;\n"
    edited = "// a new comment\nconst x = 1;\n"

    assert classify_edit(original, edited, "mod.ts") == "comment-only"


def test_classify_edit_js_regex_literal_containing_slash_slash_is_code():
    """A regex literal whose contents happen to include a `//` sequence
    (e.g. matching a literal `/`) must never be misclassified comment-only
    just because the line rule sees a `//`-looking substring inside it."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "const re = /a/;\n"
    edited = "const re = /\\/\\//;\n"

    assert classify_edit(original, edited, "mod.js") == "code"


def test_classify_edit_js_trailing_comment_on_code_line_is_code():
    """A trailing `//` comment appended to an otherwise code-shaped line is
    not comment-only under the line rule -- the changed line does not
    START with a comment marker after stripping whitespace."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "const x = 1;\n"
    edited = "const x = 1; // trailing note\n"

    assert classify_edit(original, edited, "mod.ts") == "code"


def test_classify_edit_js_ts_expect_error_edit_is_code():
    """`// @ts-expect-error` and similar directives are semantic comments a
    tool reads -- an edit to one is never comment-only even though the
    changed line starts with `//`."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "// @ts-expect-error old reason\nfoo();\n"
    edited = "// @ts-expect-error new reason\nfoo();\n"

    assert classify_edit(original, edited, "mod.ts") == "code"


# --------------------------------------------------------------------------
# Runner detection
# --------------------------------------------------------------------------


def test_detect_runner_pytest_via_conftest(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    assert detect_runner(str(tmp_path)) == "pytest"


def test_detect_runner_pytest_via_pyproject_ini_options(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", encoding="utf-8"
    )
    assert detect_runner(str(tmp_path)) == "pytest"


def test_detect_runner_vitest_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"vitest": "^1.0.0"}}', encoding="utf-8"
    )
    assert detect_runner(str(tmp_path)) == "vitest"


def test_detect_runner_none_when_no_markers(tmp_path):
    assert detect_runner(str(tmp_path)) is None


def test_find_runner_root_prefers_nearest_subpackage_over_repo_root(tmp_path):
    """A monorepo with a root-level vitest `package.json` AND a subpackage
    with its own -- the subpackage's own config must win for a test file
    that lives under it, not the root's."""
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"vitest": "^1.0.0"}}', encoding="utf-8"
    )
    pkg_dir = tmp_path / "packages" / "engine"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        '{"devDependencies": {"vitest": "^2.0.0"}}', encoding="utf-8"
    )
    tests_dir = pkg_dir / "src"
    tests_dir.mkdir()
    (tests_dir / "widget.test.ts").write_text("test('x', () => {});\n", encoding="utf-8")

    runner, root = find_runner_root(str(tmp_path), "packages/engine/src/widget.test.ts")

    assert runner == "vitest"
    assert Path(root) == pkg_dir.resolve()


def test_find_runner_root_falls_back_to_repo_root_marker(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")

    runner, root = find_runner_root(str(tmp_path), "tests/test_a.py")

    assert runner == "pytest"
    assert Path(root) == tmp_path.resolve()


def test_find_runner_root_none_when_no_marker_anywhere(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")

    runner, root = find_runner_root(str(tmp_path), "tests/test_a.py")

    assert (runner, root) == (None, None)


# --------------------------------------------------------------------------
# run_gate: no detectable runner -> indeterminate, still restores
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Serial confirmation re-run for a non-empty parallel diff (xdist-flake guard)
# --------------------------------------------------------------------------


def _confirm_fixture(tmp_path):
    """A minimal repo that reaches `_confirm_new_failures` without a real
    pytest spawn -- `detect_runner`/`find_runner_root`/`select_test_files`
    all need real files on disk, but the actual TEST RUNS are faked via a
    monkeypatched `run_selected`."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fixture.py").write_text(
        "from pathlib import Path\n\n"
        "def test_it():\n"
        "    assert (Path(__file__).resolve().parent.parent / 'target.py').exists()\n",
        encoding="utf-8",
    )
    _git_init(tmp_path)
    return ["conftest.py", "target.py", "tests/test_fixture.py"]


@pytest.mark.spawns_process
def test_run_gate_flaky_under_parallel_id_passes_confirmed_and_recorded_unconfirmed(
    tmp_path, monkeypatch
):
    """An id fails in the parallel candidate run but PASSES when re-run
    serially in confirmation -- the gate must not fail on it, and must
    record it as `unconfirmed` rather than silently dropping the signal."""
    _confirm_fixture(tmp_path)
    from coordinator_core.source_edit_gate import gate as gate_module

    node_id = "tests/test_fixture.py::test_it"
    calls = {"n": 0}

    def fake_run_selected(repo_root, test_files, runner, *, parallel=True):
        calls["n"] += 1
        if calls["n"] == 1:
            # Parallel candidate run: flaky failure.
            assert parallel is True
            return {node_id: "failed"}
        if calls["n"] == 2:
            # Parallel base run: clean.
            assert parallel is True
            return {node_id: "passed"}
        if calls["n"] == 3:
            # Serial candidate confirmation: passes this time (the flake).
            assert parallel is False
            return {node_id: "passed"}
        # Serial base confirmation.
        assert parallel is False
        return {node_id: "passed"}

    monkeypatch.setattr(gate_module, "run_selected", fake_run_selected)

    calls_log = []
    result = gate_module.run_gate(
        str(tmp_path),
        ["target.py"],
        restore_originals=lambda: calls_log.append("restore"),
        reapply_stripped=lambda: calls_log.append("reapply"),
    )

    assert result.verdict == "pass"
    assert result.new_failures == ()
    # Namespaced by `_run_groups`'s own group-root prefix (repo root -> "."):
    # see `gate.py::_run_groups`'s docstring.
    assert result.unconfirmed == (f".::{node_id}",)
    # Confirmation ends at base bytes -> the pass path must reapply once more.
    assert calls_log == ["restore", "reapply", "restore", "reapply"]


@pytest.mark.spawns_process
def test_run_gate_confirmed_regression_fails_and_leaves_tree_at_base(tmp_path, monkeypatch):
    """An id fails BOTH in the parallel candidate run and in serial
    confirmation, and passes at base confirmation -- a genuine regression,
    the gate must fail and the tree must be left restored."""
    _confirm_fixture(tmp_path)
    from coordinator_core.source_edit_gate import gate as gate_module

    node_id = "tests/test_fixture.py::test_it"
    calls = {"n": 0}

    def fake_run_selected(repo_root, test_files, runner, *, parallel=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return {node_id: "failed"}  # parallel candidate
        if calls["n"] == 2:
            return {node_id: "passed"}  # parallel base
        if calls["n"] == 3:
            assert parallel is False
            return {node_id: "failed"}  # serial candidate confirmation
        assert parallel is False
        return {node_id: "passed"}  # serial base confirmation

    monkeypatch.setattr(gate_module, "run_selected", fake_run_selected)

    calls_log = []
    result = gate_module.run_gate(
        str(tmp_path),
        ["target.py"],
        restore_originals=lambda: calls_log.append("restore"),
        reapply_stripped=lambda: calls_log.append("reapply"),
    )

    assert result.verdict == "fail"
    # Namespaced by `_run_groups`'s own group-root prefix (repo root -> "."):
    # see `gate.py::_run_groups`'s docstring.
    assert result.new_failures == (f".::{node_id}",)
    assert result.unconfirmed == ()
    # Last call is restore_originals -- tree stays at base bytes, no reapply.
    assert calls_log == ["restore", "reapply", "restore"]


@pytest.mark.spawns_process
def test_run_gate_pre_existing_failure_confirmed_on_both_sides_does_not_fail(
    tmp_path, monkeypatch
):
    """An id fails on both candidate AND base in confirmation -- pre-existing
    breakage the edit didn't cause, not a regression."""
    _confirm_fixture(tmp_path)
    from coordinator_core.source_edit_gate import gate as gate_module

    node_id = "tests/test_fixture.py::test_it"
    calls = {"n": 0}

    def fake_run_selected(repo_root, test_files, runner, *, parallel=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return {node_id: "failed"}  # parallel candidate
        if calls["n"] == 2:
            return {"other::id": "passed"}  # parallel base -- diff sees "vanished"
        if calls["n"] == 3:
            return {node_id: "failed"}  # serial candidate confirmation
        return {node_id: "failed"}  # serial base confirmation -- fails too

    monkeypatch.setattr(gate_module, "run_selected", fake_run_selected)

    result = gate_module.run_gate(
        str(tmp_path),
        ["target.py"],
        restore_originals=lambda: None,
        reapply_stripped=lambda: None,
    )

    assert result.verdict == "pass"
    assert result.new_failures == ()
    assert result.unconfirmed == ()


def test_run_gate_indeterminate_when_no_runner_detected_still_restores(tmp_path):
    calls = []
    result = run_gate(
        str(tmp_path),
        ["some/file.py"],
        restore_originals=lambda: calls.append("restore"),
        reapply_stripped=lambda: calls.append("reapply"),
    )

    assert result.verdict == "indeterminate"
    assert result == GateResult(
        verdict="indeterminate", reason="no-runner-marker", detail=(str(tmp_path),)
    )
    assert calls == ["restore"]


def test_run_gate_indeterminate_selected_file_has_no_runner_carries_reason_and_files(tmp_path):
    """A selected test file with no detectable runner anywhere between it and
    `repo_root` must still yield `verdict="indeterminate"`, but now with a
    machine-readable `reason` and the offending file(s) in `detail` -- not a
    silent `GateResult(verdict="indeterminate")` with nothing to act on."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    orphan_dir = tmp_path / "orphan"
    orphan_dir.mkdir()
    (orphan_dir / "test_orphan.py").write_text(
        "import target\n\ndef test_it():\n    assert target.VALUE == 1\n", encoding="utf-8"
    )
    _git_init(tmp_path)

    from coordinator_core.source_edit_gate import gate as gate_module

    monkeypatch_files = [
        "conftest.py", "target.py", "orphan/test_orphan.py",
    ]
    import coordinator_core.source_edit_gate.gate as gm

    orig_list = gm._list_tracked_files
    try:
        gm._list_tracked_files = lambda repo_root: monkeypatch_files
        # No runner marker exists under orphan/ nor anywhere up to repo_root
        # other than repo_root's own conftest.py -- forge a mismatch by
        # making `find_runner_root` see no marker for this one file, via a
        # monkeypatched lookup rather than deleting the real conftest.py
        # (which would also fail `detect_runner`'s own coarse gate).
        orig_find = gm.find_runner_root

        def fake_find_runner_root(repo_root, test_rel):
            if test_rel == "orphan/test_orphan.py":
                return None, None
            return orig_find(repo_root, test_rel)

        gm.find_runner_root = fake_find_runner_root
        try:
            result = gm.run_gate(
                str(tmp_path),
                ["target.py"],
                restore_originals=lambda: None,
                reapply_stripped=lambda: None,
            )
        finally:
            gm.find_runner_root = orig_find
    finally:
        gm._list_tracked_files = orig_list

    assert result.verdict == "indeterminate"
    assert result.reason == "no-runner-for-selected-file"
    assert result.detail == ("orphan/test_orphan.py",)


# --------------------------------------------------------------------------
# End-to-end: a tiny fixture repo, real pytest spawns.
# --------------------------------------------------------------------------


def _write_fixture_repo(root: Path, *, test_body: str, target_body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "conftest.py").write_text("", encoding="utf-8")
    (root / "target.py").write_text(textwrap.dedent(target_body), encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_fixture.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    _git_init(root)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_gate_end_to_end_falsifier_hashes_edited_bytes_and_gate_restores(tmp_path):
    """A test that hashes `target.py`'s own bytes (a stand-in for a hash-pinned
    vendor header / content-hash allowlist) fails once the edit changes those
    bytes; the gate must catch it and restore the tree to pre-edit bytes."""
    repo = tmp_path / "fixture-repo"
    original_body = "VALUE = 1\n"
    edited_body = "VALUE = 2\n"
    original_bytes = original_body.encode("utf-8")
    edited_bytes = edited_body.encode("utf-8")

    test_body = """\
        import hashlib
        from pathlib import Path

        EXPECTED = "{expected_hash}"

        def test_target_hash_pinned():
            data = (Path(__file__).resolve().parent.parent / "target.py").read_bytes()
            assert hashlib.sha256(data).hexdigest() == EXPECTED
    """.format(expected_hash=__import__("hashlib").sha256(original_bytes).hexdigest())

    _write_fixture_repo(repo, test_body=test_body, target_body=edited_body)
    target_file = repo / "target.py"
    # Simulate the edit already having landed on disk (the gate never touches
    # bytes itself -- the caller does that before calling in).
    target_file.write_bytes(edited_bytes)

    def restore_originals() -> None:
        target_file.write_bytes(original_bytes)

    def reapply_stripped() -> None:
        target_file.write_bytes(edited_bytes)

    result = run_gate(
        str(repo),
        ["target.py"],
        restore_originals=restore_originals,
        reapply_stripped=reapply_stripped,
    )

    assert result.verdict == "fail"
    assert any("test_target_hash_pinned" in node_id for node_id in result.new_failures)
    # Left at restored (pre-edit) bytes on fail.
    assert target_file.read_bytes() == original_bytes


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_gate_end_to_end_harmless_change_passes(tmp_path):
    repo = tmp_path / "fixture-repo"
    original_body = "VALUE = 1\n"
    edited_body = "# a harmless comment\nVALUE = 1\n"

    test_body = """\
        from pathlib import Path

        def test_target_exists():
            text = (Path(__file__).resolve().parent.parent / "target.py").read_text(encoding="utf-8")
            assert "VALUE" in text
    """
    _write_fixture_repo(repo, test_body=test_body, target_body=edited_body)
    target_file = repo / "target.py"

    def restore_originals() -> None:
        target_file.write_text(original_body, encoding="utf-8")

    def reapply_stripped() -> None:
        target_file.write_text(edited_body, encoding="utf-8")

    result = run_gate(
        str(repo),
        ["target.py"],
        restore_originals=restore_originals,
        reapply_stripped=reapply_stripped,
    )

    assert result.verdict == "pass"
    assert result.new_failures == ()
    assert target_file.read_text(encoding="utf-8") == edited_body


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_gate_end_to_end_no_selected_tests_passes_with_zero_spawn(tmp_path, monkeypatch):
    """An edited file that no test file imports or mentions selects zero test
    files -- the gate must pass immediately without spawning either side."""
    repo = tmp_path / "fixture-repo"
    _write_fixture_repo(
        repo,
        test_body="def test_unrelated():\n    assert True\n",
        target_body="VALUE = 1\n",
    )
    target_file = repo / "target.py"

    from coordinator_core.source_edit_gate import gate as gate_module

    def _boom(*a, **k):
        raise AssertionError("run_selected must not be called when selection is empty")

    monkeypatch.setattr(gate_module, "run_selected", _boom)

    calls = []
    result = run_gate(
        str(repo),
        ["target.py"],
        restore_originals=lambda: calls.append("restore"),
        reapply_stripped=lambda: calls.append("reapply"),
    )

    assert result.verdict == "pass"
    assert calls == ["reapply"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_gate_two_package_monorepo_runs_each_group_from_its_own_root(tmp_path):
    """Two Python subpackages, each with its own `conftest.py` (its own
    runner root), and NOTHING at the repo root -- selection must pick a test
    file from each package, `find_runner_root` must resolve each to its OWN
    package directory (never the repo root, which carries no marker at all),
    and each group must run from its own root so a genuinely-failing test in
    one package is caught without either group under- or over-collecting."""
    repo = tmp_path / "monorepo"
    repo.mkdir()

    pkg_a = repo / "pkg-a"
    pkg_a.mkdir()
    (pkg_a / "conftest.py").write_text("", encoding="utf-8")
    (pkg_a / "target_a.py").write_text("VALUE_A = 1\n", encoding="utf-8")
    (pkg_a / "tests").mkdir()
    (pkg_a / "tests" / "test_a.py").write_text(
        textwrap.dedent(
            """\
            from pathlib import Path

            def test_target_a_hash_pinned():
                text = (Path(__file__).resolve().parent.parent / "target_a.py").read_text(encoding="utf-8")
                assert text == "VALUE_A = 1\\n"
            """
        ),
        encoding="utf-8",
    )

    pkg_b = repo / "pkg-b"
    pkg_b.mkdir()
    (pkg_b / "conftest.py").write_text("", encoding="utf-8")
    (pkg_b / "target_b.py").write_text("VALUE_B = 1\n", encoding="utf-8")
    (pkg_b / "tests").mkdir()
    (pkg_b / "tests" / "test_b.py").write_text(
        textwrap.dedent(
            """\
            from pathlib import Path

            def test_target_b_exists():
                text = (Path(__file__).resolve().parent.parent / "target_b.py").read_text(encoding="utf-8")
                assert "VALUE_B" in text
            """
        ),
        encoding="utf-8",
    )

    # repo root itself carries NO pytest/vitest marker -- only the two
    # subpackages do, so `detect_runner(repo_root)` alone would previously
    # have refused (before, both were run from repo_root and either
    # under-collected or -- with a root marker present -- silently ran
    # against the wrong config). A root conftest.py is required for the
    # coarse readiness gate to pass; give it one so the test exercises the
    # per-file grouping path, not the "no runner anywhere" refusal.
    (repo / "conftest.py").write_text("", encoding="utf-8")
    _git_init(repo)

    target_a = pkg_a / "target_a.py"
    target_b = pkg_b / "target_b.py"
    original_a = target_a.read_bytes()
    original_b = target_b.read_bytes()
    edited_a = b"VALUE_A = 2\n"  # breaks pkg-a's hash-pinned test
    edited_b = b"# a harmless comment\nVALUE_B = 1\n"  # harmless in pkg-b

    target_a.write_bytes(edited_a)
    target_b.write_bytes(edited_b)

    def restore_originals() -> None:
        target_a.write_bytes(original_a)
        target_b.write_bytes(original_b)

    def reapply_stripped() -> None:
        target_a.write_bytes(edited_a)
        target_b.write_bytes(edited_b)

    result = run_gate(
        str(repo),
        ["pkg-a/target_a.py", "pkg-b/target_b.py"],
        restore_originals=restore_originals,
        reapply_stripped=reapply_stripped,
    )

    assert result.verdict == "fail"
    assert any("test_target_a_hash_pinned" in node_id for node_id in result.new_failures)
    assert not any("test_target_b_exists" in node_id for node_id in result.new_failures)
    # Left at restored (pre-edit) bytes on fail.
    assert target_a.read_bytes() == original_a
    assert target_b.read_bytes() == original_b


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_gate_one_group_crash_reports_only_that_group_not_every_group(tmp_path, monkeypatch):
    """Two independent runner-root groups (e.g. a nested vitest subpackage
    with no installed deps, alongside a healthy pytest group) -- one group's
    `run_selected` returning `None` (crashed/unparseable/unavailable) must
    NOT poison the merge for the OTHER group, and the resulting
    `indeterminate` detail must name only the group that actually failed,
    never every group in the batch (the field bug: a single vitest group
    with no `node_modules` made every sibling pytest group's detail entry
    look crashed too)."""
    repo = tmp_path / "monorepo"
    repo.mkdir()

    pkg_a = repo / "pkg-a"
    pkg_a.mkdir()
    (pkg_a / "conftest.py").write_text("", encoding="utf-8")
    (pkg_a / "target_a.py").write_text("VALUE_A = 1\n", encoding="utf-8")
    (pkg_a / "tests").mkdir()
    (pkg_a / "tests" / "test_a.py").write_text(
        "from pathlib import Path\n\n"
        "def test_target_a():\n"
        "    assert (Path(__file__).resolve().parent.parent / 'target_a.py').exists()\n",
        encoding="utf-8",
    )

    pkg_b = repo / "pkg-b"
    pkg_b.mkdir()
    (pkg_b / "conftest.py").write_text("", encoding="utf-8")
    (pkg_b / "target_b.py").write_text("VALUE_B = 1\n", encoding="utf-8")
    (pkg_b / "tests").mkdir()
    (pkg_b / "tests" / "test_b.py").write_text(
        "from pathlib import Path\n\n"
        "def test_target_b():\n"
        "    assert (Path(__file__).resolve().parent.parent / 'target_b.py').exists()\n",
        encoding="utf-8",
    )

    (repo / "conftest.py").write_text("", encoding="utf-8")
    _git_init(repo)

    target_a = pkg_a / "target_a.py"
    target_b = pkg_b / "target_b.py"
    target_a.write_bytes(b"VALUE_A = 2\n")
    target_b.write_bytes(b"VALUE_B = 2\n")

    from coordinator_core.source_edit_gate import gate as gate_module

    pkg_a_root = str(pkg_a.resolve())

    def fake_run_selected(root, test_files, runner, *, parallel=True):
        if root == pkg_a_root:
            return None  # simulates a crashed/unavailable runner for pkg-a only
        return {f"{f}::ok": "passed" for f in test_files}

    monkeypatch.setattr(gate_module, "run_selected", fake_run_selected)

    calls = []
    result = run_gate(
        str(repo),
        ["pkg-a/target_a.py", "pkg-b/target_b.py"],
        restore_originals=lambda: calls.append("restore"),
        reapply_stripped=lambda: calls.append("reapply"),
    )

    assert result.verdict == "indeterminate"
    assert result.reason == "candidate-run-crashed-or-empty"
    assert len(result.detail) == 1
    assert "pkg-a" in result.detail[0]
    assert "pkg-b" not in result.detail[0]


# --------------------------------------------------------------------------
# Root-cause regression tests, 2026-09-27 field measurement:
#   (1) AST-based classify_edit -- a positional token-stream diff
#       misclassified an ADDED docstring as "code" because it assumed the
#       two token streams have the same length.
#   (2) Selection breadth -- a directory- or bare-basename-literal hit on a
#       conftest/helper-module CASCADE selected the whole test tree for an
#       edit nowhere near it.
#   (3) Selection speed -- suffix sets and per-test-file import parsing were
#       recomputed once per (edited file x test file) pair instead of once
#       per call.
# --------------------------------------------------------------------------


def test_classify_edit_python_added_module_docstring_is_doc_only():
    """The exact shape of the 2026-09-27 field measurement: a module with NO
    existing docstring gains one as its first statement. A positional
    token-stream zip misclassifies this `"code"` (the token counts differ),
    because it only ever considers a REPLACED string token, never an
    inserted one."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "\nfrom __future__ import annotations\n\nX = 1\n"
    edited = (
        '"""Maps a queue-family directory name to a record_type key."""\n\n'
        "from __future__ import annotations\n\nX = 1\n"
    )

    assert classify_edit(original, edited, "mod.py") == "doc-only"


def test_classify_edit_python_removed_class_docstring_is_doc_only():
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = 'class Widget:\n    """old docstring"""\n\n    def f(self):\n        return 1\n'
    edited = "class Widget:\n\n    def f(self):\n        return 1\n"

    assert classify_edit(original, edited, "mod.py") == "doc-only"


def test_classify_edit_python_added_comment_is_comment_only():
    """A pure comment addition changes no AST node at all -- the raw
    `ast.dump` is identical on both sides."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "x = 1\n"
    edited = "# a new comment\nx = 1\n"

    assert classify_edit(original, edited, "mod.py") == "comment-only"


def test_classify_edit_python_non_docstring_string_literal_is_string():
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = 'PATH = "old/path.json"\n'
    edited = 'PATH = "new/path.json"\n'

    assert classify_edit(original, edited, "mod.py") == "string"


def test_classify_edit_python_logic_change_is_code():
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "def f(x):\n    return x + 1\n"
    edited = "def f(x):\n    return x + 2\n"

    assert classify_edit(original, edited, "mod.py") == "code"


def test_classify_edit_python_type_comment_change_is_code():
    """A `# type:` comment is runtime-invisible but mypy-visible -- with
    `type_comments=True` it becomes a real AST field, so an edit to one
    must not fall through to `"comment-only"`."""
    from coordinator_core.source_edit_gate.selection import classify_edit

    original = "def f(x):\n    y = x  # type: int\n    return y\n"
    edited = "def f(x):\n    y = x  # type: str\n    return y\n"

    assert classify_edit(original, edited, "mod.py") == "code"


def test_selection_root_conftest_directory_mention_does_not_cascade_whole_tree(tmp_path):
    """A repo-ROOT conftest.py whose docstring/comments happen to mention an
    edited file's PARENT DIRECTORY in passing (e.g. explaining what a
    package is) must not select every test file in the repo -- a directory-
    substring hit is not evidence any given test actually depends on the
    edited file. This is the exact shape of the field measurement: 10/16
    edited files under `coordinator_core/ops/` selected ~the whole 3441-file
    suite because the repo-root conftest's own docstring mentions
    `coordinator_core/ops` in prose."""
    (tmp_path / "conftest.py").write_text(
        "# This repo's coordinator_core/ops package holds every op handler.\n",
        encoding="utf-8",
    )
    pkg_dir = tmp_path / "coordinator_core" / "ops"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "queue_family.py").write_text("X = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert True\n", encoding="utf-8"
    )
    all_files = [
        "conftest.py",
        "coordinator_core/ops/queue_family.py",
        "tests/test_unrelated.py",
    ]

    selected = select_test_files(
        str(tmp_path), ["coordinator_core/ops/queue_family.py"], all_files,
    )

    assert selected == []


def test_selection_helper_module_basename_mention_does_not_cascade_whole_tree(tmp_path):
    """A colocated helper module (sits beside a test file, so it is scanned
    as a possible fixture provider) that merely mentions an edited file's
    BARE BASENAME in a comment -- a cross-reference, not a dependency --
    must not cascade to its entire (possibly huge) directory subtree. Only
    a hit against the edited file's FULL path is trusted as a cascade
    trigger; a full-path literal genuinely pins a specific reference."""
    top_dir = tmp_path / "pkg"
    top_dir.mkdir()
    (top_dir / "helper.py").write_text(
        "# companion script to unrelated_script.py, see its docs\n",
        encoding="utf-8",
    )
    (top_dir / "test_at_top.py").write_text(
        "def test_at_top():\n    assert True\n", encoding="utf-8"
    )
    nested_dir = top_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "test_nested.py").write_text(
        "def test_nested():\n    assert True\n", encoding="utf-8"
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "unrelated_script.py").write_text("Y = 1\n", encoding="utf-8")
    all_files = [
        "pkg/helper.py",
        "pkg/test_at_top.py",
        "pkg/nested/test_nested.py",
        "scripts/unrelated_script.py",
    ]

    selected = select_test_files(str(tmp_path), ["scripts/unrelated_script.py"], all_files)

    assert selected == []


def test_selection_helper_module_full_path_mention_still_cascades(tmp_path):
    """The counterpart of the basename test above: a full-path literal in a
    colocated helper module IS still trusted as a cascade trigger -- the
    breadth fix narrows the evidence bar, it does not remove the cascade
    mechanism the conftest-fixture tests above depend on."""
    top_dir = tmp_path / "pkg"
    top_dir.mkdir()
    (top_dir / "helper.py").write_text(
        'PINNED = "scripts/unrelated_script.py"\n', encoding="utf-8"
    )
    (top_dir / "test_at_top.py").write_text(
        "def test_at_top():\n    assert True\n", encoding="utf-8"
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "unrelated_script.py").write_text("Y = 1\n", encoding="utf-8")
    all_files = ["pkg/helper.py", "pkg/test_at_top.py", "scripts/unrelated_script.py"]

    selected = select_test_files(str(tmp_path), ["scripts/unrelated_script.py"], all_files)

    assert selected == ["pkg/test_at_top.py"]


def test_selection_doc_only_edit_helper_module_doc_marker_import_does_not_cascade_directory(
    tmp_path,
):
    """A `"doc-only"` edit's doc-conditional import branch (a test/importer
    selected merely because it reads docstrings, per the loose
    `_DOC_TEST_READER_MARKERS` heuristic) must not be trusted as a whole-
    directory cascade trigger for an ordinary colocated non-test module --
    only `conftest.py` gets that trust (real pytest fixture-visibility
    mechanism). A large multi-purpose CLI-shaped helper (argparse
    `description=`/`--help`) that merely imports the edited module, sitting
    beside a huge sibling test directory, must select only the test files
    that actually import/mention the edited module themselves -- not every
    test file in its directory. This is the field-measurement shape: a
    32-file docstring-only edit selected 3014/3443 test files via exactly
    this path before the fix."""
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "target.py").write_text(
        '"""original docstring"""\n\nVALUE = 1\n', encoding="utf-8"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    # A colocated non-test "helper" that is really a CLI-shaped production
    # script: it imports the edited module AND satisfies the loose doc-
    # marker heuristic (`description=`, `--help`) without itself being a
    # dedicated fixture provider.
    (tests_dir / "cli_tool.py").write_text(
        "import argparse\n"
        "import pkg.target\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser(description='a cli tool')\n"
        "    parser.add_argument('--help', action='store_true')\n"
        "    return pkg.target.VALUE\n",
        encoding="utf-8",
    )
    # An unrelated test in the SAME directory that never imports/mentions
    # the edited module at all -- must not be swept in by the cascade.
    (tests_dir / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert True\n", encoding="utf-8"
    )
    all_files = ["pkg/target.py", "tests/cli_tool.py", "tests/test_unrelated.py"]

    original = '"""original docstring"""\n\nVALUE = 1\n'
    edited = '"""a different docstring"""\n\nVALUE = 1\n'

    selected = select_test_files(
        str(tmp_path), ["pkg/target.py"], all_files,
        file_texts={"pkg/target.py": (original, edited)},
    )

    assert selected == []


def test_selection_suffixes_precomputed_once_per_call_not_per_test_file(tmp_path, monkeypatch):
    """`_module_suffixes` depends only on the edited path, not on which test
    file is being scanned -- `select_test_files` must call it O(#edited
    files) times (via `_suffixes_for`), never O(#edited files x #test
    files)."""
    from coordinator_core.source_edit_gate import selection

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(20):
        (tests_dir / f"test_{i}.py").write_text(
            "def test_it():\n    assert True\n", encoding="utf-8"
        )
    all_files = ["pkg/mod.py"] + [f"tests/test_{i}.py" for i in range(20)]

    calls = []
    original = selection._module_suffixes

    def _counting(edited_rel):
        calls.append(edited_rel)
        return original(edited_rel)

    monkeypatch.setattr(selection, "_module_suffixes", _counting)

    selection.select_test_files(str(tmp_path), ["pkg/mod.py"], all_files)

    assert len(calls) == 1


def test_selection_python_import_parsed_once_per_test_file(tmp_path, monkeypatch):
    """A test file's own `.py` source must be `ast.parse`d at most once per
    `select_test_files` call, even though its import set is checked against
    both the primary and the doc-conditional suffix set."""
    from coordinator_core.source_edit_gate import selection

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_it.py").write_text(
        "import pkg.mod\n\ndef test_it():\n    assert pkg.mod.X == 1\n", encoding="utf-8"
    )
    all_files = ["pkg/mod.py", "tests/test_it.py"]

    calls = []
    original = selection._python_imports

    def _counting(source):
        calls.append(source)
        return original(source)

    monkeypatch.setattr(selection, "_python_imports", _counting)

    selected = selection.select_test_files(str(tmp_path), ["pkg/mod.py"], all_files)

    assert selected == ["tests/test_it.py"]
    assert len(calls) == 1
