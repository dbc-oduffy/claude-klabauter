"""git_fetch_failure: last stderr line, ssh agent/auth classification, remediation wording."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from git_fetch_failure import (  # noqa: E402
    fetch_failure_detail,
    is_ssh_agent_failure,
    last_line,
    ssh_remediation,
)


@pytest.mark.parametrize(
    "stderr",
    [
        'sign_and_send_pubkey: signing failed for ED25519 "k" from agent: agent refused operation',
        "git@github.com: Permission denied (publickey).",
        "Could not open a connection to your authentication agent.",
        "Error connecting to agent: No such file or directory",
    ],
)
def test_ssh_agent_and_auth_failures_are_classified(stderr):
    assert is_ssh_agent_failure(stderr)
    assert ssh_remediation(stderr)


@pytest.mark.parametrize(
    "stderr",
    ["", None, "fatal: unable to access 'https://x/': Could not resolve host: x", "fatal: not a git repository"],
)
def test_other_failures_are_not_ssh_and_carry_no_remediation(stderr):
    assert not is_ssh_agent_failure(stderr)
    assert ssh_remediation(stderr) == ""


def test_remediation_leads_with_agent_unlock_and_never_defaults_to_key_on_disk():
    text = ssh_remediation("agent refused operation")
    assert text.startswith("Unlock or approve your SSH agent")
    assert "1Password" in text
    assert text.index("Unlock or approve") < text.index("IdentityFile")
    assert "Only for a box whose key deliberately lives on disk" in text
    assert "IdentityAgent none" not in text
    assert re.search(r"(^|\s)/[a-z]", text) is None


def test_last_line_skips_trailing_blank_lines_and_falls_back():
    assert last_line("a\nb \n\n  \n") == "b"
    assert last_line("   ", "fb") == "fb"
    assert last_line(None) == "git fetch failed"


def test_detail_names_last_line_and_url_and_marks_a_missing_url():
    assert fetch_failure_detail("x\nfatal: nope\n", "git@h:r.git") == "fatal: nope (remote: git@h:r.git)"
    assert fetch_failure_detail("", "") == "git fetch failed (remote: unknown)"
