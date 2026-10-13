"""run_token: mint, admit, consume, and which sizings an in-flight run owns."""

from __future__ import annotations

import threading

import pytest

from coordinator_core.ops.dispatch_emit import run_token


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path


def test_run_owns_only_the_sizing_its_record_names(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "a.mjs", session_id="s1", sizing_rel="state/sizings/mine.yaml")
    (repo / "state" / "sizings" / "other.yaml").write_text("x", encoding="utf-8")
    assert run_token.run_owns_sizing(repo, "s1", "state/sizings/mine.yaml")
    assert not run_token.run_owns_sizing(repo, "s1", "state/sizings/other.yaml")
    assert not run_token.run_owns_sizing(repo, "s2", "state/sizings/mine.yaml")


def test_run_without_a_named_sizing_owns_none(tmp_path):
    repo = _repo(tmp_path)
    run_token.mint(repo, "ask-1", "a.mjs", session_id="s1")
    (repo / "state" / "sizings" / "new.yaml").write_text("x", encoding="utf-8")
    assert not run_token.run_owns_sizing(repo, "s1", "state/sizings/new.yaml")


def test_admits_only_the_minted_token_once(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "b.mjs")
    assert not run_token.admits(repo, "../x", token, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


def test_admit_without_script_path_is_refused_and_does_not_burn_the_record(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", token)
    assert not run_token.admits(repo, "ask-1", token, None)
    assert run_token.admits(repo, "ask-1", token, "a.mjs")


def test_wrong_token_does_not_burn_a_live_record(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", "f" * 32, "a.mjs")
    assert run_token.admits(repo, "ask-1", token, "a.mjs")


def test_mint_requires_a_script_path(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        run_token.mint(repo, "ask-1", None)  # type: ignore[arg-type]


def test_record_with_a_null_script_is_never_admitted(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    path = repo / ".git" / "coordinator-warp-run-tokens" / "ask-1.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"a.mjs"', "null"), encoding="utf-8")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


def test_remint_after_admit_admits_once_more(tmp_path):
    repo = _repo(tmp_path)
    first = run_token.mint(repo, "ask-1", "a.mjs")
    assert run_token.admits(repo, "ask-1", first, "a.mjs")
    second = run_token.mint(repo, "ask-1", "a.mjs")
    assert not run_token.admits(repo, "ask-1", first, "a.mjs")
    assert run_token.admits(repo, "ask-1", second, "a.mjs")
    assert not run_token.admits(repo, "ask-1", second, "a.mjs")


def test_consume_makes_the_token_inert(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    run_token.consume(repo, "ask-1")
    assert not run_token.admits(repo, "ask-1", token, "a.mjs")


def test_racing_admits_yield_exactly_one_true(tmp_path):
    repo = _repo(tmp_path)
    token = run_token.mint(repo, "ask-1", "a.mjs")
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[bool] = []

    def attempt():
        barrier.wait()
        results.append(run_token.admits(repo, "ask-1", token, "a.mjs"))

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == workers


def test_command_params_parse_inline_and_heredoc():
    inline = "coordinator-invoke dispatch.terminal_commit '{\"run_id\": \"a\", \"run_token\": \"t\", \"x\": \"it'\\''s\"}'"
    assert run_token.token_params_from_command(inline)["run_id"] == "a"
    heredoc = "coordinator-invoke dispatch.terminal_commit --params-file - <<'JSON'\n{\"run_id\": \"b\", \"run_token\": \"t\"}\nJSON"
    assert run_token.token_params_from_command(heredoc)["run_id"] == "b"
