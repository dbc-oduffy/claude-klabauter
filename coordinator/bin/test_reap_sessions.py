from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PASS = 0
FAIL = 0


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    global FAIL
    print(f"  FAIL: {label}")
    if detail:
        print(f"    {detail}")
    FAIL += 1
    pytest.fail(f"{label}: {detail}" if detail else label, pytrace=False)


def _load_module():
    path = os.path.join(SCRIPT_DIR, "reap-sessions.py")
    spec = importlib.util.spec_from_file_location("reap_sessions_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_route=None):
    orig_route = mod.cc_invoke.route
    if fake_route is not None:
        mod.cc_invoke.route = fake_route
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv if argv is not None else [])
    finally:
        mod.cc_invoke.route = orig_route
    return rc, out.getvalue(), err.getvalue()


def test_dispatches_session_reap_empty_params_no_force(tmp_path):
    mod = _load_module()
    seen = {}
    fake_repo_root = str(tmp_path)

    def fake_route(op, params, repo_root, legacy_fn):
        seen["op"] = op
        seen["params"] = params
        seen["repo_root"] = repo_root
        return {"exit_code": 0}

    rc, out, _err = _run_main_capturing(mod, argv=[fake_repo_root], fake_route=fake_route)

    if rc == 0:
        _pass("dispatch shape: exit 0")
    else:
        _fail("dispatch shape: exit 0", f"got rc={rc}")

    if seen.get("op") == "session.reap":
        _pass("dispatch shape: op == session.reap")
    else:
        _fail("dispatch shape: op == session.reap", f"got {seen.get('op')!r}")

    if seen.get("params") == {}:
        _pass("dispatch shape: params == {} exactly")
    else:
        _fail("dispatch shape: params == {} exactly", f"got {seen.get('params')!r}")

    if "force" not in seen.get("params", {}):
        _pass("dispatch shape: 'force' never passed")
    else:
        _fail("dispatch shape: 'force' never passed", f"params: {seen.get('params')!r}")

    if seen.get("repo_root") == fake_repo_root:
        _pass("dispatch shape: repo_root forwarded from argv")
    else:
        _fail("dispatch shape: repo_root forwarded from argv", f"got {seen.get('repo_root')!r}")


def test_no_stdout_on_success(tmp_path):
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        return {"exit_code": 0}

    rc, out, err = _run_main_capturing(mod, argv=[str(tmp_path)], fake_route=fake_route)

    if rc == 0:
        _pass("no-stdout-on-success: exit 0")
    else:
        _fail("no-stdout-on-success: exit 0", f"got rc={rc}")

    if out == "":
        _pass("no-stdout-on-success: stdout is empty (negative-spec)")
    else:
        _fail("no-stdout-on-success: stdout is empty", f"got {out!r}")

    if err == "":
        _pass("no-stdout-on-success: stderr is empty (no WARN on success)")
    else:
        _fail("no-stdout-on-success: stderr is empty", f"got {err!r}")


def test_transport_failure_exits_zero_warns_no_stdout(tmp_path):
    mod = _load_module()

    def fake_route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated session.reap transport failure")

    rc, out, err = _run_main_capturing(mod, argv=[str(tmp_path)], fake_route=fake_route)

    if rc == 0:
        _pass("transport failure: exit 0 (best-effort, never blocks session start)")
    else:
        _fail("transport failure: exit 0", f"got rc={rc}")

    if out == "":
        _pass("transport failure: no stdout on failure path")
    else:
        _fail("transport failure: no stdout on failure path", f"got {out!r}")

    if "session.reap failed" in err and "best-effort" in err:
        _pass("transport failure: stderr mentions session.reap failed (best-effort)")
    else:
        _fail("transport failure: stderr mentions session.reap failed (best-effort)", f"got {err!r}")


def test_unresolvable_repo_root_exits_zero_no_dispatch():
    mod = _load_module()
    called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        called["n"] += 1
        return {"exit_code": 0}

    mod._resolve_repo_root = lambda argv: None

    rc, out, err = _run_main_capturing(mod, argv=[], fake_route=fake_route)

    if rc == 0:
        _pass("unresolvable repo_root: exit 0 (never blocks session start)")
    else:
        _fail("unresolvable repo_root: exit 0", f"got rc={rc}")

    if called["n"] == 0:
        _pass("unresolvable repo_root: route() never dispatched")
    else:
        _fail("unresolvable repo_root: route() never dispatched", f"called {called['n']} times")

    if "cannot resolve git repo root" in err:
        _pass("unresolvable repo_root: stderr explains the failure")
    else:
        _fail("unresolvable repo_root: stderr explains the failure", f"got {err!r}")


def test_repo_flag_resolves_to_value(tmp_path):
    mod = _load_module()
    seen = {}
    fake_repo_root = str(tmp_path)

    def fake_route(op, params, repo_root, legacy_fn):
        seen["repo_root"] = repo_root
        return {"exit_code": 0}

    rc, _out, _err = _run_main_capturing(
        mod, argv=["--repo", fake_repo_root], fake_route=fake_route
    )

    if rc == 0:
        _pass("--repo flag: exit 0")
    else:
        _fail("--repo flag: exit 0", f"got rc={rc}")

    if seen.get("repo_root") == fake_repo_root:
        _pass("--repo flag: resolves to the value, not the literal '--repo'")
    else:
        _fail(
            "--repo flag: resolves to the value, not the literal '--repo'",
            f"got {seen.get('repo_root')!r}",
        )


def test_bare_positional_still_resolves(tmp_path):
    mod = _load_module()
    seen = {}
    fake_repo_root = str(tmp_path)

    def fake_route(op, params, repo_root, legacy_fn):
        seen["repo_root"] = repo_root
        return {"exit_code": 0}

    rc, _out, _err = _run_main_capturing(mod, argv=[fake_repo_root], fake_route=fake_route)

    if rc == 0:
        _pass("bare positional: exit 0")
    else:
        _fail("bare positional: exit 0", f"got rc={rc}")

    if seen.get("repo_root") == fake_repo_root:
        _pass("bare positional: still resolves (back-compat)")
    else:
        _fail("bare positional: still resolves (back-compat)", f"got {seen.get('repo_root')!r}")


def test_empty_argv_falls_back_to_show_toplevel():
    mod = _load_module()

    mod._resolve_repo_root = lambda argv: "resolved-via-show-toplevel"
    seen = {}

    def fake_route(op, params, repo_root, legacy_fn):
        seen["repo_root"] = repo_root
        return {"exit_code": 0}

    rc, _out, _err = _run_main_capturing(mod, argv=[], fake_route=fake_route)

    if rc == 0:
        _pass("empty argv: exit 0")
    else:
        _fail("empty argv: exit 0", f"got rc={rc}")

    if seen.get("repo_root") == "resolved-via-show-toplevel":
        _pass("empty argv: falls back to show_toplevel() resolution")
    else:
        _fail(
            "empty argv: falls back to show_toplevel() resolution",
            f"got {seen.get('repo_root')!r}",
        )


def test_unresolvable_root_returns_zero_and_writes_stderr():
    mod = _load_module()
    called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        called["n"] += 1
        return {"exit_code": 0}

    mod._resolve_repo_root = lambda argv: None

    rc, out, err = _run_main_capturing(mod, argv=["--repo"], fake_route=fake_route)

    if rc == 0:
        _pass("unresolvable root via --repo: exit 0, not an exception")
    else:
        _fail("unresolvable root via --repo: exit 0, not an exception", f"got rc={rc}")

    if called["n"] == 0:
        _pass("unresolvable root via --repo: route() never dispatched")
    else:
        _fail("unresolvable root via --repo: route() never dispatched", f"called {called['n']} times")

    if "cannot resolve git repo root" in err:
        _pass("unresolvable root via --repo: stderr explains the failure")
    else:
        _fail("unresolvable root via --repo: stderr explains the failure", f"got {err!r}")


def test_posix_and_windows_style_paths_resolve_unchanged():
    mod = _load_module()

    posix_path = "/opt/some-repo"
    windows_path = "C:\\Users\\some-user\\some-repo"

    if mod._resolve_repo_root(["--repo", posix_path]) == posix_path:
        _pass("multi-os: POSIX-style --repo path resolves unchanged")
    else:
        _fail(
            "multi-os: POSIX-style --repo path resolves unchanged",
            f"got {mod._resolve_repo_root(['--repo', posix_path])!r}",
        )

    if mod._resolve_repo_root(["--repo", windows_path]) == windows_path:
        _pass("multi-os: Windows-style --repo path resolves unchanged")
    else:
        _fail(
            "multi-os: Windows-style --repo path resolves unchanged",
            f"got {mod._resolve_repo_root(['--repo', windows_path])!r}",
        )

    if mod._resolve_repo_root([posix_path]) == posix_path:
        _pass("multi-os: POSIX-style bare positional resolves unchanged")
    else:
        _fail(
            "multi-os: POSIX-style bare positional resolves unchanged",
            f"got {mod._resolve_repo_root([posix_path])!r}",
        )

    if mod._resolve_repo_root([windows_path]) == windows_path:
        _pass("multi-os: Windows-style bare positional resolves unchanged")
    else:
        _fail(
            "multi-os: Windows-style bare positional resolves unchanged",
            f"got {mod._resolve_repo_root([windows_path])!r}",
        )


def test_repo_flag_alone_no_value_falls_through_exits_zero():
    mod = _load_module()

    mod._resolve_repo_root_orig = mod._resolve_repo_root
    result = mod._resolve_repo_root(["--repo"])

    if result != "--repo":
        _pass("'--repo' alone: does not resolve to the literal flag string")
    else:
        _fail("'--repo' alone: does not resolve to the literal flag string", f"got {result!r}")

    called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        called["n"] += 1
        return {"exit_code": 0}

    mod._resolve_repo_root = lambda argv: None
    rc, _out, _err = _run_main_capturing(mod, argv=["--repo"], fake_route=fake_route)

    if rc == 0:
        _pass("'--repo' alone: main() returns 0, never a non-zero exit")
    else:
        _fail("'--repo' alone: main() returns 0, never a non-zero exit", f"got rc={rc}")


def test_unrecognised_flag_returns_zero_without_raising():
    mod = _load_module()

    result = mod._resolve_repo_root(["--nope"])

    if result != "--nope":
        _pass("unrecognised flag: does not resolve to the literal flag string")
    else:
        _fail("unrecognised flag: does not resolve to the literal flag string", f"got {result!r}")

    called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        called["n"] += 1
        return {"exit_code": 0}

    mod._resolve_repo_root = lambda argv: None
    rc, _out, _err = _run_main_capturing(mod, argv=["--nope"], fake_route=fake_route)

    if rc == 0:
        _pass("unrecognised flag: main() returns 0 without raising")
    else:
        _fail("unrecognised flag: main() returns 0 without raising", f"got rc={rc}")

