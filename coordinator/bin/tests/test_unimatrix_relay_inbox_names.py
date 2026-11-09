"""Inbox routing names must fit real sessions while remaining safe socket path components."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "inbox_name_relay", Path(__file__).resolve().parents[1] / "unimatrix-relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


@pytest.mark.parametrize("name", [
    "a", "_", "-", "codex-01a1b630-6d3a-7861-ae89-44e2b9b4cfaa",
    "codex-Project_ClaudeKlabauter-12-on-MacMini", "group-em-09.192.168.0.175",
    "A" * 64, "a.b", "Case_Sensitive-1",
])
def test_accepts_native_names_and_64_character_boundary(name):
    assert relay.valid_inbox_name(name)
    assert relay.parse_mode("inbox " + name) == ("inbox", name)
    socket_path = f"/tmp/cc-socks/unimatrix-{name}.sock"
    assert len(socket_path.encode()) < 104


@pytest.mark.parametrize("name", [
    "", "a" * 65, ".", "..", ".hidden", "trailing.", "nested..name", "../escape", "a/../../b",
    r"a\b", "name@box", "two words", "tabs\tname", "new\nline", "control\x00name",
    "trailing ", " leading", "x;y", "x&y", "x|y", "$(id)", "`id`", '"quoted"', "x:y", "é",
])
def test_refuses_paths_traversal_whitespace_controls_and_shell_punctuation(name):
    assert not relay.valid_inbox_name(name)
    assert relay.parse_mode("inbox " + name) is None


def test_direct_inbox_call_validates_before_filesystem_or_platform_work(monkeypatch):
    monkeypatch.setattr(relay, "endpoint_kind", lambda: pytest.fail("used platform before validating name"))
    with pytest.raises(SystemExit, match="name must"):
        relay.inbox("../escape")
