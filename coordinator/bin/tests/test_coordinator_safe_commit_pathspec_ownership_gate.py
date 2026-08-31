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
