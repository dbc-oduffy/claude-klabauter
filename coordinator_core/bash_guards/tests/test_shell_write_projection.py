"""shell_writes: in-repo write targets of a Bash command, with literal heredoc bodies."""

import os

from coordinator_core.bash_guards._shell_write_projection import ShellWrite, shell_writes


def _root(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "sub").mkdir()
    return str(tmp_path)


def test_cat_quoted_heredoc_body(tmp_path):
    root = _root(tmp_path)
    cmd = "cat > pkg/mod.py <<'EOF'\nx = 1\ny = 2\nEOF\n"
    assert shell_writes(cmd, root, root) == [
        ShellWrite(os.path.join(root, "pkg/mod.py"), "x = 1\ny = 2\n", False)
    ]


def test_cat_append(tmp_path):
    root = _root(tmp_path)
    w = shell_writes("cat >> pkg/mod.py <<'EOF'\na\nEOF\n", root, root)
    assert len(w) == 1 and w[0].append is True and w[0].body == "a\n"


def test_tee_unquoted_literal(tmp_path):
    root = _root(tmp_path)
    w = shell_writes("tee pkg/mod.py <<EOF\na\nb\nEOF\n", root, root)
    assert [x.body for x in w] == ["a\nb\n"]


def test_python_payload_target_has_no_body(tmp_path):
    root = _root(tmp_path)
    cmd = "python3 - <<'EOF'\nopen('pkg/step_ledger.py','w').write('x')\nEOF\n"
    w = shell_writes(cmd, root, root)
    assert [os.path.basename(x.abs_path) for x in w] == ["step_ledger.py"]
    assert w[0].body is None


def test_unquoted_delimiter_with_expansion_is_not_literal(tmp_path):
    root = _root(tmp_path)
    w = shell_writes("cat > pkg/mod.py <<EOF\nv=$X\nEOF\n", root, root)
    assert len(w) == 1 and w[0].body is None


def test_out_of_repo_target_absent(tmp_path):
    root = _root(tmp_path)
    other = tmp_path.parent / (tmp_path.name + "-other")
    other.mkdir()
    assert shell_writes(f"cat > {other}/m.py <<'EOF'\na\nEOF\n", root, root) == []


def test_cd_resolves_against_sub(tmp_path):
    root = _root(tmp_path)
    w = shell_writes("cd sub && cat > m.py <<EOF\na\nEOF\n", root, root)
    assert [x.abs_path for x in w] == [os.path.join(root, "sub", "m.py")]
    assert w[0].body == "a\n"


def test_sed_i_has_no_body(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "pkg" / "m.py").write_text("a\n")
    w = shell_writes("sed -i 's/a/b/' pkg/m.py", root, root)
    assert len(w) == 1 and w[0].body is None
