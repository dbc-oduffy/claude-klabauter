"""`coordinator-safe-commit.py::_refuse_contested_pathspec` -- the ownership
gate the explicit-pathspec form never had.

WHY THIS EXISTS. `do_pathspec`'s inherited anti-scope ("no session id,
orphan-claim, or handoff-scope machinery applies to this explicit-path form")
and `main`'s dispatch comment ("the delegate does its own session/ownership
gating") together described coverage that stopped existing when DR-344 killed
`scoped-git-commit` on 2026-08-23. Its replacement, `ceremony.commit_v2`, is a
thin envelope over `commit.commit_paths`: it releases claims AFTER a commit and
checks none before it. `check_validate_commit`'s Check-5 apparatus does not
cover the gap either -- its own regex matches a literal `git commit` only, and
`coordinator-safe-commit "<msg>" -- <paths>` contains no such token.

The cost, measured 2026-08-31 on `work/machine-a/2026-08-18to31`: sessions
`d12e25cf` and `1ad288d0` each held uncommitted hunks in
`coordinator_core/workstream_complete/__init__.py`; `e74e4ce8` committed the
whole file at `40abe011d0` for an unrelated fix and landed both, under a message
describing neither and, for one of them, without its regression tests.

FAILURE DIRECTION, pinned: this runs ahead of every explicit-pathspec commit the
fleet makes, so an unresolvable identity or an unreadable sink must ALLOW. A
refusal has to rest on a claim that was actually read.

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _stub_session(mod, monkeypatch, *, session_id="mine", contested=None, raises=None):
    """Stand in for `_import_session()`'s (core, liveness, scope, claims) tuple."""

    def _contested(paths, sid, cwd=None):
        if raises is not None:
            raise raises
        return dict(contested or {})

    cs_core = types.SimpleNamespace(resolve_session_id=lambda: session_id)
    cs_scope = types.SimpleNamespace(contested_by_live_peers=_contested)
    monkeypatch.setattr(
        mod, "_import_session", lambda: (cs_core, object(), cs_scope, object())
    )


class TestRefusal:
    def test_contested_path_refuses_and_names_every_holder(self, monkeypatch, capsys):
        mod = _load_cli_module()
        _stub_session(
            mod,
            monkeypatch,
            contested={"pkg/mod.py": ["1ad288d0-258b", "d12e25cf-3a6b"]},
        )

        with pytest.raises(SystemExit) as exc:
            mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo")

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "pkg/mod.py" in err
        # Both holders, not just the most recent -- coordinating with half the
        # people who hold the file is the failure this replaces.
        assert "1ad288d0" in err and "d12e25cf" in err, err

    def test_refusal_does_not_advertise_an_override(self, monkeypatch, capsys):
        """Message register (`docs/wiki/guard-messaging.md` § Register): one
        fact, once, plus a terse alternative -- never an override key."""
        mod = _load_cli_module()
        _stub_session(mod, monkeypatch, contested={"pkg/mod.py": ["peer-a"]})

        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo")

        err = capsys.readouterr().err
        assert "COORDINATOR_OVERRIDE_SCOPE" not in err, err
        assert "git commit" not in err, err


class TestFailsOpen:
    def test_uncontested_paths_return_without_exiting(self, monkeypatch):
        mod = _load_cli_module()
        _stub_session(mod, monkeypatch, contested={})

        assert mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo") is None

    def test_unresolved_session_id_allows(self, monkeypatch):
        """An identity this route never resolves for itself must not become a
        refusal -- `resolve_session_id`'s fail-CLOSED exit(1) is deliberately
        not reused here."""
        mod = _load_cli_module()
        _stub_session(
            mod, monkeypatch, session_id="", contested={"pkg/mod.py": ["peer-a"]}
        )

        assert mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo") is None

    def test_raising_claim_read_allows(self, monkeypatch):
        mod = _load_cli_module()
        _stub_session(mod, monkeypatch, raises=OSError("sink unreadable"))

        assert mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo") is None

    def test_unimportable_engine_allows(self, monkeypatch):
        mod = _load_cli_module()

        def _boom():
            raise ImportError("coordinator_core not on path")

        monkeypatch.setattr(mod, "_import_session", _boom)

        assert mod._refuse_contested_pathspec(["pkg/mod.py"], "/repo") is None


def test_do_pathspec_calls_the_gate_before_dispatching():
    """The gate is worthless if it runs after `ceremony.commit_v2`. Asserts
    ORDER in the source, since the dispatch is what it must precede."""
    source = (_BIN_DIR / "coordinator-safe-commit.py").read_text(encoding="utf-8")
    body = source.split("def do_pathspec(", 1)[1].split("\ndef ", 1)[0]
    assert "_refuse_contested_pathspec(" in body, "gate not called from do_pathspec"
    assert body.index("_refuse_contested_pathspec(") < body.index(
        'cc_invoke("ceremony.commit_v2"'
    ), "gate must run before the commit dispatch"


def _tiny_repo(tmp_path):
    """A real git repo with one committed file. The narrowing asks git a
    question no stub can answer honestly, so these tests spend one."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(
        ["git", *a], cwd=root, capture_output=True, text=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "committed.py").write_text("clean\n", encoding="utf-8")
    (root / "dirty.py").write_text("original\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.py").write_text("original\n", encoding="utf-8")
    run("add", "committed.py", "dirty.py", "sub/nested.py")
    run("commit", "-q", "-m", "seed")
    return root


class TestCleanPathNarrowing:
    """A TOUCH has a birth and no death, so most of what reaches the refusal is
    residue: a path committed hours ago still contests for the rest of its
    holder's lifetime. Measured 2026-08-31 -- one holder blocked
    `coordinator/bin/publish.py` for 11.3h with no uncommitted content in it,
    bypass the only exit. A path identical to its HEAD blob cannot carry the
    harm the refusal names, whoever holds it."""

    def test_a_path_matching_head_no_longer_blocks_the_commit(self, tmp_path, monkeypatch):
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        _stub_session(mod, monkeypatch, contested={"committed.py": ["peer-a"]})

        # Returns rather than exiting: nothing uncommitted is in that file, so
        # committing it lands nobody's work.
        assert mod._refuse_contested_pathspec(["committed.py"], str(root)) is None

    def test_a_dirty_contested_path_still_refuses(self, tmp_path, monkeypatch, capsys):
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "dirty.py").write_text("edited\n", encoding="utf-8")
        _stub_session(mod, monkeypatch, contested={"dirty.py": ["peer-a"]})

        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec(["dirty.py"], str(root))
        assert "dirty.py" in capsys.readouterr().err

    def test_the_dirty_half_of_a_mixed_pathspec_still_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        """Narrowing the set is not the same as dropping the refusal."""
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "dirty.py").write_text("edited\n", encoding="utf-8")
        _stub_session(
            mod,
            monkeypatch,
            contested={"dirty.py": ["peer-a"], "committed.py": ["peer-b"]},
        )

        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec(["dirty.py", "committed.py"], str(root))
        err = capsys.readouterr().err
        assert "dirty.py" in err
        assert "committed.py" not in err, err

    def test_an_untracked_contested_path_still_refuses(self, tmp_path, monkeypatch):
        """`diff HEAD` would call a brand-new file clean -- it is absent from
        HEAD -- while committing it lands exactly the peer's work. That is why
        the probe reads `status --porcelain`."""
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "brand-new.py").write_text("theirs\n", encoding="utf-8")
        _stub_session(mod, monkeypatch, contested={"brand-new.py": ["peer-a"]})

        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec(["brand-new.py"], str(root))

    def test_an_undeterminable_answer_leaves_the_refusal_whole(self, tmp_path, monkeypatch):
        """Fails CLOSED. `I could not tell` must never become `safe to commit`."""
        mod = _load_cli_module()
        _stub_session(mod, monkeypatch, contested={"committed.py": ["peer-a"]})

        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec(["committed.py"], str(tmp_path / "not-a-repo"))

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_a_backslash_path_dirty_content_still_refuses(self, tmp_path, monkeypatch, capsys):
        """Review: coordinator:code-reviewer af0c0865daafdd73a, Finding P1 --
        the subprocess argv used to receive the RAW `paths` argument while
        everything it was compared against was normalized (backslash ->
        forward-slash). A backslash-bearing contested path could then fail
        git's pathspec matching, come back with empty porcelain output
        (returncode==0, nothing unparseable), and be silently classified
        `clean` -- exactly the "I could not tell -> safe to commit" flip the
        function's own docstring forbids. Pins the fix: a path named with a
        Windows-style backslash for a nested, actually-dirty file must still
        be recognized as dirty and refuse the commit.

        Real git is the assertion here, not stubbed state: the bug was in
        how git's OWN pathspec matching treats a literal backslash, which no
        plain-file fixture can reproduce.
        """
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "sub" / "nested.py").write_text("edited\n", encoding="utf-8")

        backslash_path = "sub\\nested.py"
        clean = mod._paths_with_no_uncommitted_content([backslash_path], str(root))
        assert clean == set(), (
            "a backslash-spelled, actually-dirty path must not be reported clean"
        )

        _stub_session(mod, monkeypatch, contested={backslash_path: ["peer-a"]})
        with pytest.raises(SystemExit):
            mod._refuse_contested_pathspec([backslash_path], str(root))
        assert backslash_path in capsys.readouterr().err

    def test_the_probe_spends_one_process_for_the_whole_set(self, tmp_path, monkeypatch):
        """Never one spawn per path -- the amplification gate's rule, and this
        runs ahead of every explicit-pathspec commit the fleet makes."""
        import subprocess as _subprocess

        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        calls = []
        real_run = _subprocess.run

        def _counting_run(*args, **kwargs):
            calls.append(args[0])
            return real_run(*args, **kwargs)

        monkeypatch.setattr(mod.subprocess, "run", _counting_run)
        mod._paths_with_no_uncommitted_content(
            ["committed.py", "dirty.py", "brand-new.py"], str(root)
        )
        assert len(calls) == 1, calls
