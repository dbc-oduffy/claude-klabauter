"""A committed-blob checkout of a sizing is admitted only while a merge, rebase
or cherry-pick is in progress; hand-authored and no-operation writes deny."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

F = "state/sizings/2026-10-27-weekly-gate-mechanical-to-code.yaml"


def _repo(tmp_path, head=None):
    git = tmp_path / ".git"
    git.mkdir()
    if head:
        (git / head).write_text("0" * 40 + "\n")
    return str(tmp_path)


def _run(cmd, cwd):
    return check({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}}, None)


def _assert_denied(result):
    # DR-475: a non-strict guard level returns the levelled advisory envelope in place of the deny record.
    assert result is not None
    if isinstance(result, dict):
        assert result.get("guardLevel") and "sizing" in result["hookSpecificOutput"]["additionalContext"]
    else:
        assert result.verdict_kind == "deny"


@pytest.mark.parametrize("head", ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"])
@pytest.mark.parametrize(
    "cmd",
    [
        f"git checkout --theirs -- {F}",
        f"git checkout --ours -- {F}",
        f"git checkout origin/machine-b -- {F}",
    ],
)
def test_blob_checkout_admitted_mid_operation(tmp_path, head, cmd):
    assert _run(cmd, _repo(tmp_path, head)) is None


@pytest.mark.parametrize(
    "cmd",
    [f"git checkout --theirs -- {F}", f"git checkout origin/machine-b -- {F}"],
)
def test_blob_checkout_denied_with_no_operation(tmp_path, cmd):
    _assert_denied(_run(cmd, _repo(tmp_path)))


def test_blob_checkout_denied_without_a_repo(tmp_path):
    _assert_denied(_run(f"git checkout --theirs -- {F}", str(tmp_path)))


@pytest.mark.parametrize(
    "cmd",
    [
        f"echo x > {F}",
        f"sed -i s/a/b/ {F}",
        f"git checkout --theirs -- {F} && echo x > {F}",
        f"git checkout --theirs -- {F}; cp /tmp/x {F}",
        f"git checkout --theirs -- state/sizings/*.yaml",
        f"git checkout --theirs -- {F} CLAUDE.md",
        f"git checkout -p -- {F}",
        f"git checkout --theirs -- state/sizings/../../x.yaml",
        f"git checkout -f --theirs -- {F}",
        f"git checkout --theirs -- {F} > {F}",
    ],
)
def test_non_blob_write_still_denied_mid_merge(tmp_path, cmd):
    _assert_denied(_run(cmd, _repo(tmp_path, "MERGE_HEAD")))
