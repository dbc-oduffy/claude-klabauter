"""A shell write into state/sizings is refused; a read is allowed."""

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _check(cmd):
    return _enveloped(guard.check)({"tool_name": "Bash", "tool_input": {"command": cmd}}, None)


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


def test_cp_install_from_sizings_into_scratch_is_allowed():
    for cmd in (
        "cp state/sizings/x.yaml scratch/x.yaml",
        "cp -p state/sizings/x.yaml scratch/",
        "cp -t scratch/ state/sizings/x.yaml",
        "cp --target-directory=scratch state/sizings/x.yaml",
        "install state/sizings/x.yaml scratch/x.yaml",
    ):
        assert _check(cmd) is None, cmd


def test_cp_mv_into_sizings_is_still_refused():
    for cmd in (
        "cp scratch/x.yaml state/sizings/x.yaml",
        "mv scratch/x.yaml state/sizings/",
        "mv -t state/sizings/ scratch/x.yaml",
        "mv state/sizings/x.yaml scratch/x.yaml",
        "mv -t scratch/ state/sizings/x.yaml",
        "cp -t state/sizings/ scratch/x.yaml",
        "cp --target-directory=state/sizings scratch/x.yaml",
        "cp state/sizings/a.yaml state/sizings/b.yaml",
        "install scratch/x.yaml state/sizings/x.yaml",
        "cp state/sizings/x.yaml scratch/x.yaml && cp scratch/y state/sizings/y.yaml",
        "cp state/sizings/x.yaml $D",
    ):
        assert _check(cmd) is not None, cmd


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


def test_read_of_a_sizing_whose_slug_names_an_interpreter_is_allowed():
    # The engine-ask gate stage's own call: the slug is minted from the ask's words.
    rel = "state/sizings/2026-10-15-python-repo-with-setup-py-setup-cfg-but.yaml"
    assert _check(f"cat {rel}") is None
    assert _check(
        '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" '
        f"dispatch.ask_gate '{{\"sizing_path\":\"{rel}\"}}'; echo ---; cat {rel}"
    ) is None
    assert _check(f"python3 -c 'open(\"{rel}\",\"w\")'") is not None
