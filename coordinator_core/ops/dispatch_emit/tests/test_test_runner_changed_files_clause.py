"""Every test-runner leg is told to list changed files with git, never a tree walk."""
from coordinator_core.ops.dispatch_emit import emit


def test_test_runner_clause_names_git_for_changed_files():
    clause = emit._test_runner_clause(None)
    assert "git diff --name-only" in clause
    assert "git ls-files --others --exclude-standard" in clause
    assert "never `find`" in clause
