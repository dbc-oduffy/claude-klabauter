"""A shell write into state/sizings is refused; a read is allowed."""

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard


def _check(cmd):
    return guard.check({"tool_name": "Bash", "tool_input": {"command": cmd}}, None)


def test_append_redirect_into_sizings_is_refused():
    result = _check("cat >> state/sizings/x.yaml <<'EOF'\nk: v\nEOF")
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sizing-assemble --write" in reason
    assert "sizing.record_pm_resolution" in reason
    assert "sizing-assemble --register <yaml> --write <sizing>" in reason


def test_tee_into_sizings_is_refused():
    assert _check("echo k: v | tee state/sizings/x.yaml") is not None


def test_read_of_sizings_is_allowed():
    assert _check("cat state/sizings/x.yaml") is None


_SA = "/home/u/.coordinator-claude-settings/bin/sizing-assemble"


def test_sanctioned_writer_with_prose_naming_an_interpreter_is_allowed():
    cmd = (f'{_SA} --tshirt XS --intent "--kind python was refused" '
           '--write state/sizings/python-detect.yaml 2>&1 | tail -30')
    assert _check(cmd) is None
    assert _check("coordinator-invoke fleet.archive_terminal_sizings '{\"mode\":\"already-terminal\","
                  "\"candidate_ids\":[\"state/sizings/2026-10-12-python-x.yaml\"]}' 2>&1 | tail") is None
    assert _check('export PATH="/b:$PATH"; cd /r && sizing-assemble --intent "use python" '
                  "--register a.yaml --write state/sizings/x.yaml") is None


def test_sanctioned_writer_does_not_launder_a_write_or_execution():
    for cmd in (
        f'{_SA} --intent "$(python3 -c 1)" --write state/sizings/x.yaml',
        f"{_SA} --write state/sizings/x.yaml > state/sizings/y.yaml",
        f"{_SA} --write state/sizings/x.yaml | tee state/sizings/y.yaml",
        f"{_SA} --intent `id` --write state/sizings/x.yaml",
        f"{_SA} --intent <(python3 -c 1) --write state/sizings/x.yaml",
        f"{_SA} --write state/sizings/x.yaml; python3 -c 'open(\"state/sizings/y\",\"w\")'",
    ):
        assert _check(cmd) is not None, cmd
