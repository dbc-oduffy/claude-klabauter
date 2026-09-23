"""test_cross_repo_memo_never_inbox_mirror — the `cross-repo-memo send` CLI
refuses coordinator-claude / claude-klabauter exactly like the engine op it
forwards onto (PM ruling 2026-09-23).

`_cmd_send` is a bare forwarder onto claude-klabauter's `memo.send` op via
`cc_invoke.route_mutation` (see its own docstring) — this test exercises that
forwarding for real: `route_mutation` is monkeypatched to call the REAL
`coordinator_core.ops.fleet.memo_send._memo_send` in-process (no warm server,
no subprocess), so the assertion is against the actual chokepoint, not a
re-statement of it.

Run: python -m pytest coordinator/bin/tests/test_cross_repo_memo_never_inbox_mirror.py -p no:cacheprovider -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib
import subprocess
import sys
import types
from contextlib import redirect_stdout, redirect_stderr

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(_BIN_DIR.parent.parent))  # repo root, for coordinator_core

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402
from coordinator_core.ops.fleet.memo_send import _memo_send  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), capture_output=True, check=check,
        **no_console_creationflags(),
    )


def _make_sender_git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_mirror_git_repo(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init receiver")
    return root


def _make_claude_home(tmp_path: pathlib.Path, receiver_repos: dict[str, pathlib.Path]) -> pathlib.Path:
    # No `publish.mirrors.*` declared — the under-registered shape that
    # produced the live hole.
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _write_draft(sender_repo: pathlib.Path, topic: str, *, to: str) -> pathlib.Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    draft_path.write_text(
        "---\n"
        'title: "A test memo"\n'
        'from: "example-retrieval-repo-em"\n'
        f'to: "{to}"\n'
        "created: 2026-09-23\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "fyi"\n'
        'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
        "---\n\n"
        "Body prose.\n",
        encoding="utf-8", newline="\n",
    )
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo_never_inbox_mirror", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_never_inbox_mirror", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "receiver_key, receiver_dir, to_id",
    [
        ("coordinator_claude", "coordinator-claude", "coordinator-claude-em"),
        ("claude_klabauter", "claude-klabauter", "claude-klabauter-em"),
    ],
)
def test_cli_send_refuses_mirror_via_real_engine_op(
    tmp_path, monkeypatch, receiver_key, receiver_dir, to_id,
):
    sender_repo = _make_sender_git_repo(tmp_path)
    mirror_repo = _make_mirror_git_repo(tmp_path, receiver_dir)
    claude_home = _make_claude_home(tmp_path, {receiver_key: mirror_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "cli-topic-mirror", to=to_id)

    mod = _load_cli_module()

    def _real_route_mutation(op, payload, sender_root, legacy):
        assert op == "memo.send"
        return _memo_send(payload, repo_root=pathlib.Path(sender_root))

    fake_cc_invoke = types.SimpleNamespace(route_mutation=_real_route_mutation)
    monkeypatch.setitem(sys.modules, "cc_invoke", fake_cc_invoke)
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(sender_repo))
    monkeypatch.setattr(mod, "_warn_if_unregistered_sender", lambda: None)

    args = types.SimpleNamespace(topic="cli-topic-mirror")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod._cmd_send(args)

    assert rc != 0
    assert "Receiver-side:" not in out.getvalue()
    assert list((mirror_repo / "cross-repo" / "inbox").glob("*.md")) == []
