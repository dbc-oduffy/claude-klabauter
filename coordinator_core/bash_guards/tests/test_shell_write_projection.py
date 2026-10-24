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


def _paths(cmd, root, cwd=None):
    return [os.path.relpath(w.abs_path, root) for w in shell_writes(cmd, cwd or root, root)]


def test_git_add_pathspecs(tmp_path):
    root = _root(tmp_path)
    assert _paths("git add pkg/a.py sub/b.py", root) == ["pkg/a.py", "sub/b.py"]
    assert _paths("git add -- pkg/a.py", root) == ["pkg/a.py"]
    assert _paths("cd sub && git add b.py", root) == ["sub/b.py"]
    assert all(w.body is None for w in shell_writes("git add pkg/a.py", root, root))


def test_git_add_all_is_root(tmp_path):
    root = _root(tmp_path)
    assert _paths("git add -A", root) == ["."]
    assert _paths("git add .", root) == ["."]


def test_git_checkout_only_after_double_dash(tmp_path):
    root = _root(tmp_path)
    assert _paths("git checkout -- pkg/a.py", root) == ["pkg/a.py"]
    assert _paths("git checkout main", root) == []
    assert _paths("git restore pkg/a.py", root) == ["pkg/a.py"]
    assert _paths("git restore --staged pkg/a.py", root) == []


def test_git_commit_forms(tmp_path):
    root = _root(tmp_path)
    assert _paths("git commit -m 'msg; x' pkg/a.py", root) == ["pkg/a.py"]
    assert _paths("git commit -m msg", root) == ["."]
    assert _paths("git commit -am msg", root) == ["."]
    assert _paths("git commit -m msg -- pkg/a.py", root) == ["pkg/a.py"]
    assert _paths('git commit -m "$(cat <<\'EOF\'\nfix\nEOF\n)"', root) == ["."]


def test_git_apply_reads_patch_headers(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "x.patch").write_text(
        "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-a\n+b\n+++ /dev/null\n--- a/sub/c.py\n+++ b/sub/c.py\n"
    )
    assert _paths("git apply x.patch", root) == ["pkg/a.py", "sub/c.py"]
    assert _paths("git apply --check x.patch", root) == []


def test_git_apply_unreadable_patch_is_root(tmp_path):
    root = _root(tmp_path)
    assert _paths("git apply missing.patch", root) == ["."]
    assert _paths("git apply -", root) == ["."]


def test_git_tag_push_log_yield_nothing(tmp_path):
    root = _root(tmp_path)
    for c in ("git tag -a v1 -m x", "git push origin main", "git log --oneline -3", "git status 2>&1"):
        assert _paths(c, root) == []


def test_sed_i_has_no_body(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "pkg" / "m.py").write_text("a\n")
    w = shell_writes("sed -i 's/a/b/' pkg/m.py", root, root)
    assert len(w) == 1 and w[0].body is None
