"""test_sweep_actioned_memos.py — regression coverage for sweep-actioned-memos.py's
argv handling.

Was: `_resolve_repo_root(argv)` took `argv[0]` as the repo root unconditionally,
including when it was a leading-dash flag (`--help`, a typo). `--help` got
forwarded to `coordinator_core.invoke` as a bogus `--repo` value, the transport
call failed, and the trampoline's best-effort log-and-continue posture printed
"transport error -- skipping" and still exited 0 — a fail-silent success for a
user asking for help (or making a typo). Fixed by routing argv through the
shared `sweep_argv.parse_repo_root_argv` guard (coordinator/bin/lib/sweep_argv.py)
before any transport call is attempted.

Spec backlink: coordinator/bin/sweep-actioned-memos.py `_resolve_repo_root`
fail-silent-success fix, 2026-07-25.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    """Import sweep-actioned-memos.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "sweep-actioned-memos.py")
    spec = importlib.util.spec_from_file_location("sweep_actioned_memos_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv, fake_route=None, fake_run=None):
    """Run mod.main(argv) with stdout/stderr captured; optionally fake
    cc_invoke.route and subprocess.run (the git-resolution call)."""
    orig_route = mod.cc_invoke.route
    orig_run = mod.subprocess.run
    if fake_route is not None:
        mod.cc_invoke.route = fake_route
    if fake_run is not None:
        mod.subprocess.run = fake_run
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv)
    finally:
        mod.cc_invoke.route = orig_route
        mod.subprocess.run = orig_run
    return rc, out.getvalue(), err.getvalue()


def test_help_exits_zero_and_prints_usage():
    mod = _load_module()
    calls = {"n": 0}

    def fake_route(*a, **kw):
        calls["n"] += 1
        return {"exit_code": 0, "candidates": []}

    rc, out, err = _run_main_capturing(mod, ["--help"], fake_route=fake_route)

    assert rc == 0
    assert "usage" in out.lower()
    assert "sweep-actioned-memos.py" in out
    assert calls["n"] == 0, "no transport call should happen on --help"


def test_unknown_flag_exits_nonzero_and_never_calls_transport():
    mod = _load_module()
    calls = {"n": 0}

    def fake_route(*a, **kw):
        calls["n"] += 1
        return {"exit_code": 0, "candidates": []}

    rc, out, err = _run_main_capturing(mod, ["--bogus"], fake_route=fake_route)

    assert rc != 0
    assert "unrecognized argument" in err
    assert "--bogus" in err
    assert calls["n"] == 0, "no transport call should happen on an unrecognized flag"


def test_bare_positional_repo_root_resolves():
    mod = _load_module()
    seen_repo_roots = []

    def fake_route(op, params, repo_root, legacy_fn):
        seen_repo_roots.append(repo_root)
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": []}
        return {"exit_code": 0, "acted": []}

    def fake_run(*a, **kw):
        raise AssertionError("git subprocess must not run when a positional repo_root is given")

    rc, out, _err = _run_main_capturing(
        mod, ["/tmp/fake-repo"], fake_route=fake_route, fake_run=fake_run
    )

    assert rc == 0
    assert out.strip() == "0"
    assert seen_repo_roots == ["/tmp/fake-repo"]


def test_noarg_resolves_via_git():
    mod = _load_module()
    seen_repo_roots = []

    def fake_route(op, params, repo_root, legacy_fn):
        seen_repo_roots.append(repo_root)
        if params.get("dry_run") is True:
            return {"exit_code": 0, "candidates": []}
        return {"exit_code": 0, "acted": []}

    class _FakeProc:
        returncode = 0
        stdout = "/resolved/via/git\n"

    def fake_run(*a, **kw):
        return _FakeProc()

    rc, out, _err = _run_main_capturing(mod, [], fake_route=fake_route, fake_run=fake_run)

    assert rc == 0
    assert out.strip() == "0"
    assert seen_repo_roots == ["/resolved/via/git"]


def _partial_route(failed, skipped=()):
    """fake_route whose act call reports a partial (exit_code=2) sweep."""
    def fake_route(op, params, repo_root, legacy_fn):
        if params.get("dry_run") is True:
            # `candidates` are dicts keyed by "id" -- a list of bare strings
            # yields no ids and main() short-circuits before the act call.
            return {"exit_code": 0, "candidates": [{"id": "cross-repo/inbox/probe.md"}]}
        return {
            "exit_code": 2,
            "acted": [],
            "failed": list(failed),
            "skipped": list(skipped),
        }
    return fake_route


def test_partial_sweep_names_the_failed_memo_and_its_reason():
    """A partial sweep must name WHICH memo failed and WHY on stderr.

    Was: the warning said only "partial (exit_code=2, acted=0) -- check claude-klabauter
    logs" while `failed[]` already carried both facts in the envelope. A
    2026-07-30 caller reverse-engineered `_is_terminal` and `archive_and_commit`
    by hand to rediscover that its two memos were untracked -- the `git mv`
    error below was available the whole time.
    """
    mod = _load_module()
    failed = [{
        "id": "cross-repo/inbox/2026-07-30-x-em-untracked-probe.md",
        "reason": "fatal: not under version control, source=cross-repo/inbox/"
                  "2026-07-30-x-em-untracked-probe.md",
    }]

    rc, out, err = _run_main_capturing(
        mod, ["/tmp/fake-repo"], fake_route=_partial_route(failed),
        fake_run=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no git")),
    )

    assert rc == 0
    assert out.strip() == "0"
    assert "2026-07-30-x-em-untracked-probe.md" in err
    assert "not under version control" in err
    assert "check claude-klabauter logs" not in err, (
        "the reason is now printed, so the message must not punt to the logs"
    )


def test_partial_sweep_falls_back_to_skip_reasons_when_nothing_failed():
    """exit_code=2 with an empty failed[] means every candidate was DEFERRED;
    the skip reasons carry the why, so they must surface too."""
    mod = _load_module()
    skipped = [{"id": "cross-repo/inbox/probe.md", "reason": "re-live: live claim held"}]

    rc, _out, err = _run_main_capturing(
        mod, ["/tmp/fake-repo"], fake_route=_partial_route([], skipped=skipped),
        fake_run=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no git")),
    )

    assert rc == 0
    assert "probe.md" in err
    assert "re-live: live claim held" in err


def test_partial_sweep_caps_reported_failures():
    """The inbox can hold ~90 memos; an unbounded dump would bury the summary
    line it exists to explain. Overflow is reported as a count, not dropped."""
    mod = _load_module()
    failed = [{"id": f"cross-repo/inbox/m{i}.md", "reason": "boom"} for i in range(25)]

    rc, _out, err = _run_main_capturing(
        mod, ["/tmp/fake-repo"], fake_route=_partial_route(failed),
        fake_run=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no git")),
    )

    assert rc == 0
    printed = [ln for ln in err.splitlines() if "FAILED" in ln]
    assert len(printed) == mod._MAX_REPORTED_FAILURES
    assert f"and {25 - mod._MAX_REPORTED_FAILURES} more failure(s)" in err


def test_cli_help_via_subprocess_exits_zero():
    """End-to-end sanity: invoking the script itself (not the imported module)
    with `--help` exits 0 and never reaches the real transport."""
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "sweep-actioned-memos.py"), "--help"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0
    assert "usage" in proc.stdout.lower()


def test_cli_unknown_flag_via_subprocess_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "sweep-actioned-memos.py"), "--bogus"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode != 0
    assert "unrecognized argument" in proc.stderr
