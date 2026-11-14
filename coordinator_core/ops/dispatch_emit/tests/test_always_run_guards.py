"""always_run_guards: declared list reading, touched-file hit counting, new-hit comparison."""

from coordinator_core.ops.dispatch_emit import always_run_guards as arg


def _repo(tmp_path, front):
    (tmp_path / "coordinator.local.md").write_text(f"---\n{front}\n---\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_guard.py").write_text("", encoding="utf-8")
    return tmp_path


def test_declared_guards_reads_inline_list_and_drops_missing_files(tmp_path):
    root = _repo(tmp_path, 'always_run_guards: ["tests/test_guard.py::test_no_literal", "gone/test_x.py"]')
    assert arg.declared_guards(root) == ["tests/test_guard.py::test_no_literal"]


def test_declared_guards_absent_file_or_key(tmp_path):
    assert arg.declared_guards(tmp_path) == []
    root = _repo(tmp_path, "other_key: [a]")
    assert arg.declared_guards(root) == []


def test_config_problems_names_a_misspelled_key(tmp_path):
    root = _repo(tmp_path, 'always_run_guard: ["tests/test_guard.py::test_x"]')
    problems = arg.config_problems(root)
    assert len(problems) == 1 and "'always_run_guard'" in problems[0]


def test_config_problems_names_an_unresolvable_id(tmp_path):
    root = _repo(tmp_path, 'always_run_guards: ["tests/test_guard.py::test_x", "gone/test_x.py::t"]')
    problems = arg.config_problems(root)
    assert len(problems) == 1 and "'gone/test_x.py::t'" in problems[0]


def test_config_problems_clean_for_valid_unrelated_and_absent(tmp_path):
    assert arg.config_problems(tmp_path) == []
    root = _repo(tmp_path, 'always_run_guards: ["tests/test_guard.py"]\nfast_test_cmd: "pytest"\nother_key: [a]')
    assert arg.config_problems(root) == []


def _fixture_guard_output(lines):
    return "\n".join(f"{p}:{n}: /home/someone/x" for p, n in lines)


def test_touched_hits_counts_per_hit_lines():
    text = _fixture_guard_output([("src/a.py", 3), ("src/a.py", 9), ("src/other.py", 1)])
    assert arg.touched_hits(text, ["src/a.py", "src/b.py"]) == {"src/a.py": 2, "src/b.py": 0}


def test_touched_hits_windows_and_posix_lines_both_count():
    text = "src\\a.py:1: lit\nsrc/a.py:2: lit"
    assert arg.touched_hits(text, ["src/a.py"]) == {"src/a.py": 2}


def test_touched_hits_prefix_of_longer_path_not_counted():
    text = "src/a.pyc:1: x\nsrc/a.py/inner:2: x\nxsrc/a.py:3: x\nsrc/a.py:4: x"
    assert arg.touched_hits(text, ["src/a.py"]) == {"src/a.py": 1}


def test_new_hits_flags_only_growth_over_head():
    head = {"a.py": 2, "b.py": 1}
    tree = {"a.py": 3, "b.py": 1, "c.py": 1, "d.py": 0}
    assert arg.new_hits(head, tree) == ["a.py", "c.py"]


def test_baseline_only_red_yields_no_new_hits():
    text = _fixture_guard_output([("a.py", 1), ("a.py", 2)])
    counts = arg.touched_hits(text, ["a.py"])
    assert arg.new_hits(counts, counts) == []


def test_new_hit_under_red_baseline_is_found():
    head = arg.touched_hits(_fixture_guard_output([("a.py", 1)]), ["a.py"])
    tree = arg.touched_hits(_fixture_guard_output([("a.py", 1), ("a.py", 7)]), ["a.py"])
    assert arg.new_hits(head, tree) == ["a.py"]


def test_record_keys():
    assert (arg.GUARDS_KEY, arg.GUARD_HITS_KEY) == ("always_run_guards", "guard_hits")
