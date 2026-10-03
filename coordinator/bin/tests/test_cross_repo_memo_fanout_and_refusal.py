"""Comma-list fan-out helpers and the CLI send-refusal contract (stdout REFUSED line, non-zero exit, stderr reason)."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "cross-repo-memo.py"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("cross_repo_memo_cli", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_split_receivers_dedupes_and_single_is_unchanged(cli):
    assert cli._split_receivers("a, b,a,") == ["a", "b"]
    assert cli._split_receivers("solo") == ["solo"]


def test_fanout_topic_is_per_receiver_slug(cli):
    assert cli._fanout_topic("t", "Example_Retrieval_Repo-EM") == "t--example-retrieval-repo-em"


def test_draft_fanout_invokes_one_draft_per_receiver(cli, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_cmd_draft", lambda a: seen.append((a.topic, a.to)) or 0)
    ns = argparse.Namespace(topic="t", to="a,b", title="x")
    assert cli._draft_fanout(ns, ["a", "b"]) == 0
    assert seen == [("t--a", "a"), ("t--b", "b")]


def test_send_refusal_prints_refused_last_stdout_line_exits_nonzero_and_stderr(
    cli, monkeypatch, capsys,
):
    import cc_invoke

    monkeypatch.setattr(cli, "_current_repo_root", lambda: "/nowhere")
    monkeypatch.setattr(cli, "_warn_if_unregistered_sender", lambda: None)
    monkeypatch.setattr(cli, "_fanout_topics", lambda *a: [])
    monkeypatch.setattr(cli, "_print_route_mutation_failure_reasons", lambda e: None)

    def boom(*a, **k):
        raise RuntimeError("staged body is byte-identical to draft 'x'")

    monkeypatch.setattr(cc_invoke, "route_mutation", boom)
    rc = cli._cmd_send(argparse.Namespace(topic="later-topic"))
    out, err = capsys.readouterr()
    assert rc == 1
    assert "byte-identical" in err
    assert out.strip().splitlines()[-1].startswith("REFUSED: later-topic: staged body")
