"""A shell write into state/sizings is refused; a read is allowed."""

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard


def _check(cmd):
    return guard.check({"tool_name": "Bash", "tool_input": {"command": cmd}}, None)


def test_append_redirect_into_sizings_is_refused():
    result = _check("cat >> state/sizings/x.yaml <<'EOF'\nk: v\nEOF")
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sizing-assemble --write" in reason
    assert "sizing.record_pm_resolution" in reason


def test_tee_into_sizings_is_refused():
    assert _check("echo k: v | tee state/sizings/x.yaml") is not None


def test_read_of_sizings_is_allowed():
    assert _check("cat state/sizings/x.yaml") is None
