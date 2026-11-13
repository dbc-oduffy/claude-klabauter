"""check_blanket_git_add matches executing command words only: heredoc payloads and quoted arguments never trigger it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_blanket_add, _dc_git_shared, dispatch_checks
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = pytest.mark.complete


@pytest.fixture
def hazard_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / "state").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    monkeypatch.setattr(
        _dc_blanket_add,
        "_bt_add_subtree_foreign_paths_and_churn",
        lambda *a, **k: (["state/peer.md"], 0),
    )
    return repo


def _denied(cmd: str) -> bool:
    out = envelope_of(dispatch_checks.check_blanket_git_add(cmd, "sess-heredoc"))
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


HEREDOC_BODY = "cat > f.json <<'EOF'\n{\"note\": \"it's a fixture: git add -- state\"}\nEOF\ncoordinator-invoke workflow.revise \"$(cat f.json)\""


@pytest.mark.spawns_process
def test_real_add_still_denies(hazard_repo):
    assert _denied("git add -- state")
    assert _denied("git add -A")


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "cmd",
    [
        HEREDOC_BODY,
        "cat > f <<EOF\ngit add -- state\nEOF",
        "git commit -F - <<'MSG'\nnote: git add -A\nMSG",
        'echo "git add -- state"',
        "echo 'git add -A'",
        "printf '%s' \"run git add . later\"",
    ],
)
def test_heredoc_body_and_quoted_literal_pass(hazard_repo, cmd):
    assert not _denied(cmd)


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "cmd",
    [
        "cat > f <<'EOF'\nx\nEOF\ngit add -- state",
        "bash <<'EOF'\ngit add -- state\nEOF",
        'bash -c "git add -- state"',
    ],
)
def test_executing_forms_still_deny(hazard_repo, cmd):
    assert _denied(cmd)


@pytest.mark.spawns_process
def test_unquoted_heredoc_substitution_executes_and_denies(hazard_repo):
    assert _denied("cat <<EOF\n$(git " + "add -A)\nEOF")
    assert _denied("cat <<EOF\n`git " + "add -- state`\nEOF")


@pytest.mark.spawns_process
def test_quoted_heredoc_substitution_is_data(hazard_repo):
    assert not _denied("cat <<'EOF'\n$(git " + "add -A)\nEOF")
    assert not _denied("cat <<EOF\nnote: $(echo hi) git " + "add -A\nEOF")


def test_shared_splitter_drops_heredoc_body():
    segs = _dc_blanket_add._awk_quote_aware_split("cat > f <<'EOF'\nit's; git add -A\nEOF\nls")
    assert not any("git add" in s for s in segs)
