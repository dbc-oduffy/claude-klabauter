"""A designed_red failing node yields no blocker; an unmarked one still does."""

from coordinator_core.ops.review_test_blocker import from_reverify, is_designed_red, own_diff_blockers

RED_MODULE = "import pytest\npytestmark = pytest.mark.designed_red\n\ndef test_x():\n    assert False\n"
PLAIN_MODULE = "def test_x():\n    assert False\n\n@pytest.mark.designed_red\ndef test_dec():\n    assert False\n"


def _tree(tmp_path):
    (tmp_path / "red.py").write_text(RED_MODULE)
    (tmp_path / "plain.py").write_text(PLAIN_MODULE)
    return str(tmp_path)


def _result(*ids):
    return {"failing": list(ids)}


def test_module_level_pytestmark_excluded_unmarked_kept(tmp_path):
    root = _tree(tmp_path)
    r = _result("red.py::test_x", "plain.py::test_x")
    assert [b.node_id for b in from_reverify(r, root)] == ["plain.py::test_x"]
    assert [b.node_id for b in own_diff_blockers(r, ["red.py", "plain.py"], root)] == ["plain.py::test_x"]


def test_function_decorator_excluded_sibling_function_kept(tmp_path):
    root = _tree(tmp_path)
    r = _result("plain.py::test_dec", "plain.py::test_x")
    assert [b.node_id for b in from_reverify(r, root)] == ["plain.py::test_x"]


def test_list_pytestmark_and_cwd_default(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("import pytest\npytestmark = [pytest.mark.slow, pytest.mark.designed_red]\n")
    monkeypatch.chdir(tmp_path)
    assert is_designed_red("m.py::test_a[param]")
    assert from_reverify(_result("m.py::test_a")) == []


def test_missing_file_still_blocks(tmp_path):
    assert [b.node_id for b in from_reverify(_result("nope.py::t"), str(tmp_path))] == ["nope.py::t"]


def test_edited_module_is_reread(tmp_path):
    p = tmp_path / "e.py"
    p.write_text("def test_a():\n    pass\n")
    assert is_designed_red("e.py::test_a", str(tmp_path)) is False
    p.write_text("import pytest\npytestmark = pytest.mark.designed_red\n" + "# pad\n" * 3)
    assert is_designed_red("e.py::test_a", str(tmp_path)) is True
