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
    path = os.path.join(SCRIPT_DIR, "prune-closed-improvements.py")
    spec = importlib.util.spec_from_file_location("prune_closed_improvements_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_load_records=None, fake_route=None):
    orig_load_records = mod.load_family_records
    orig_route = mod.route
    if fake_load_records is not None:
        mod.load_family_records = fake_load_records
    if fake_route is not None:
        mod.route = fake_route
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv if argv is not None else [])
    finally:
        mod.load_family_records = orig_load_records
        mod.route = orig_route
    return rc, out.getvalue(), err.getvalue()


def test_empty_candidates_skips_all_calls():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        assert family == "improvement-queue"
        assert where == "status = closed"
        return []

    route_called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        route_called["n"] += 1
        return {"exit_code": 0, "archived": None, "dest": None, "items": []}

    rc, out, _err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc == 0:
        _pass("empty candidates: exit 0")
    else:
        _fail("empty candidates: exit 0", f"got rc={rc}")

    if route_called["n"] == 0:
        _pass("empty candidates: fleet.archive_queue_entry never invoked")
    else:
        _fail("empty candidates: never invoked", f"called {route_called['n']} times")

    if "nothing to prune" in out:
        _pass("empty candidates: 'nothing to prune' message printed")
    else:
        _fail("empty candidates: 'nothing to prune' message printed", f"stdout: {out!r}")


def test_two_call_batch_shape():
    mod = _load_module()
    calls = []

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [
            {"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}},
            {"path": "state/improvement-queue/2026-01-02-b.yaml", "frontmatter": {"status": "closed"}},
        ]

    def fake_route(op, params, repo_root, legacy_fn):
        assert op == "fleet.archive_queue_entry"
        calls.append(dict(params))
        entry_paths = params["entry_paths"]
        if params["dry_run"] is True:
            return {
                "exit_code": 0, "archived": None, "dest": None,
                "items": [
                    {"id": p, "dest": f"archive/improvement-queue/2026-01/{p.rsplit('/', 1)[-1]}", "error": None}
                    for p in entry_paths
                ],
            }
        return {
            "exit_code": 0, "archived": None, "dest": None,
            "items": [{"id": p, "archived": True, "dest": "archive/improvement-queue/2026-01/x.yaml", "error": None} for p in entry_paths],
        }

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc == 0:
        _pass("two-call-batch: exit 0")
    else:
        _fail("two-call-batch: exit 0", f"got rc={rc}")

    if len(calls) == 2:
        _pass("two-call-batch: exactly 2 route() calls total (1 preview + 1 act)")
    else:
        _fail("two-call-batch: exactly 2 route() calls total", f"got {len(calls)}: {calls!r}")

    preview_calls = [c for c in calls if c["dry_run"] is True]
    act_calls = [c for c in calls if c["dry_run"] is False]
    if len(preview_calls) == 1 and len(act_calls) == 1:
        _pass("two-call-batch: 1 preview + 1 act call")
    else:
        _fail(
            "two-call-batch: 1 preview + 1 act call",
            f"preview={len(preview_calls)} act={len(act_calls)}",
        )

    if act_calls and len(act_calls[0]["entry_paths"]) == 2:
        _pass("two-call-batch: act call carries the WHOLE candidate set in one entry_paths list")
    else:
        _fail("two-call-batch: act call carries the whole set", f"act_calls: {act_calls!r}")

    if "2 entr(ies) archived" in out:
        _pass("two-call-batch: full count (2) printed")
    else:
        _fail("two-call-batch: full count (2) printed", f"stdout: {out!r}")

    if "WARN" in err:
        _fail("two-call-batch: no WARN on full success", f"stderr: {err!r}")
    else:
        _pass("two-call-batch: no WARN on full success")


def test_dry_run_mode_skips_act_calls():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [{"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}}]

    act_called = {"n": 0}

    def fake_route(op, params, repo_root, legacy_fn):
        if params["dry_run"] is False:
            act_called["n"] += 1
        return {
            "exit_code": 0, "archived": None, "dest": None,
            "items": [{"id": p, "dest": "archive/improvement-queue/2026-01/a.yaml", "error": None} for p in params["entry_paths"]],
        }

    rc, out, _err = _run_main_capturing(
        mod, argv=["--dry-run"], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc == 0:
        _pass("dry-run mode: exit 0")
    else:
        _fail("dry-run mode: exit 0", f"got rc={rc}")

    if act_called["n"] == 0:
        _pass("dry-run mode: act call (dry_run:false) never invoked")
    else:
        _fail("dry-run mode: act call never invoked", f"called {act_called['n']} times")

    if "1 closed improvement(s) selected for prune (--dry-run: no changes made)" in out:
        _pass("dry-run mode: preview message printed")
    else:
        _fail("dry-run mode: preview message printed", f"stdout: {out!r}")


def test_preview_failure_drops_candidate():
    mod = _load_module()
    act_entry_paths = []

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [
            {"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}},
            {"path": "state/improvement-queue/2026-01-02-b.yaml", "frontmatter": {"status": "closed"}},
        ]

    def fake_route(op, params, repo_root, legacy_fn):
        if params["dry_run"] is True:
            return {
                "exit_code": 2, "archived": None, "dest": None,
                "items": [
                    {"id": "state/improvement-queue/2026-01-01-a.yaml", "dest": None, "error": "entry_path escapes state/improvement-queue/"},
                    {"id": "state/improvement-queue/2026-01-02-b.yaml", "dest": "archive/improvement-queue/2026-01/b.yaml", "error": None},
                ],
            }
        act_entry_paths.extend(params["entry_paths"])
        return {
            "exit_code": 0, "archived": None, "dest": None,
            "items": [{"id": p, "archived": True, "dest": "archive/improvement-queue/2026-01/b.yaml", "error": None} for p in params["entry_paths"]],
        }

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc == 0:
        _pass("preview failure: exit 0 (surviving candidate archived cleanly)")
    else:
        _fail("preview failure: exit 0", f"got rc={rc}")

    if "1 closed improvement(s) selected for prune" in out:
        _pass("preview failure: only the surviving candidate (1) selected")
    else:
        _fail("preview failure: only the surviving candidate (1) selected", f"stdout: {out!r}")

    if act_entry_paths == ["state/improvement-queue/2026-01-02-b.yaml"]:
        _pass("preview failure: dropped candidate never reaches the act call's entry_paths")
    else:
        _fail("preview failure: dropped candidate excluded from act call", f"act_entry_paths: {act_entry_paths!r}")

    if "WARN" in err and "preview failed" in err:
        _pass("preview failure: WARN emitted for the dropped candidate")
    else:
        _fail("preview failure: WARN emitted for the dropped candidate", f"stderr: {err!r}")

    if "1 entr(ies) archived" in out:
        _pass("preview failure: surviving candidate still archived")
    else:
        _fail("preview failure: surviving candidate still archived", f"stdout: {out!r}")


def test_discovery_failure_is_non_blocking():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        raise RuntimeError("simulated discovery failure")

    rc, out, err = _run_main_capturing(mod, argv=[], fake_load_records=fake_load_records)

    if rc == 0:
        _pass("discovery failure: exit 0 (non-blocking)")
    else:
        _fail("discovery failure: exit 0", f"got rc={rc}")

    if "skipping (non-blocking)" in out:
        _pass("discovery failure: skip message printed")
    else:
        _fail("discovery failure: skip message printed", f"stdout: {out!r}")

    if "WARN" in err:
        _pass("discovery failure: WARN on stderr")
    else:
        _fail("discovery failure: WARN on stderr", f"stderr: {err!r}")


# while the rest landed) is named AND makes the process exit NON-ZERO --
def test_partial_act_failure_is_named_and_nonzero():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [
            {"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}},
            {"path": "state/improvement-queue/2026-01-02-b.yaml", "frontmatter": {"status": "closed"}},
        ]

    def fake_route(op, params, repo_root, legacy_fn):
        if params["dry_run"] is True:
            return {
                "exit_code": 0, "archived": None, "dest": None,
                "items": [{"id": p, "dest": "archive/improvement-queue/2026-01/x.yaml", "error": None} for p in params["entry_paths"]],
            }
        return {
            "exit_code": 1, "archived": None, "dest": None,
            "items": [
                {"id": "state/improvement-queue/2026-01-01-a.yaml", "archived": True, "dest": "archive/improvement-queue/2026-01/a.yaml", "error": None},
                {
                    "id": "state/improvement-queue/2026-01-02-b.yaml", "archived": False, "dest": None,
                    "error": "cannot lock ref 'HEAD': is at 35de842... but expected 731df98...",
                },
            ],
        }

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc != 0:
        _pass("partial act failure: exit NON-ZERO (F9 fix)")
    else:
        _fail("partial act failure: exit non-zero", f"got rc={rc}")

    if "1 entr(ies) failed to archive" in err and "2026-01-02-b.yaml" in err and "cannot lock ref" in err:
        _pass("partial act failure: failed entry named with its reason on stderr")
    else:
        _fail("partial act failure: failed entry named with its reason", f"stderr: {err!r}")

    if "1 entr(ies) archived" in out:
        _pass("partial act failure: surviving entry still reported archived")
    else:
        _fail("partial act failure: surviving entry still reported archived", f"stdout: {out!r}")


def test_batch_dispatch_transport_failure_is_nonzero():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [{"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}}]

    def fake_route(op, params, repo_root, legacy_fn):
        if params["dry_run"] is True:
            return {
                "exit_code": 0, "archived": None, "dest": None,
                "items": [{"id": p, "dest": "archive/improvement-queue/2026-01/a.yaml", "error": None} for p in params["entry_paths"]],
            }
        raise RuntimeError("simulated transport failure on batch archive dispatch")

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc != 0:
        _pass("batch transport failure: exit NON-ZERO")
    else:
        _fail("batch transport failure: exit non-zero", f"got rc={rc}")

    if "WARN" in err and "batch archive dispatch failed" in err:
        _pass("batch transport failure: reason surfaced on stderr")
    else:
        _fail("batch transport failure: reason surfaced on stderr", f"stderr: {err!r}")


def test_idempotent_noop_is_not_a_failure():
    mod = _load_module()

    def fake_load_records(family, repo_root, where=None, since=None, limit=0):
        return [{"path": "state/improvement-queue/2026-01-01-a.yaml", "frontmatter": {"status": "closed"}}]

    def fake_route(op, params, repo_root, legacy_fn):
        if params["dry_run"] is True:
            return {
                "exit_code": 0, "archived": None, "dest": None,
                "items": [{"id": p, "dest": "archive/improvement-queue/2026-01/a.yaml", "error": None} for p in params["entry_paths"]],
            }
        return {
            "exit_code": 0, "archived": None, "dest": None,
            "items": [{"id": p, "archived": False, "dest": "archive/improvement-queue/2026-01/a.yaml", "error": None} for p in params["entry_paths"]],
        }

    rc, out, err = _run_main_capturing(
        mod, argv=[], fake_load_records=fake_load_records, fake_route=fake_route
    )

    if rc == 0:
        _pass("idempotent no-op: exit 0")
    else:
        _fail("idempotent no-op: exit 0", f"got rc={rc}")

    if "WARN" in err:
        _fail("idempotent no-op: no WARN (not a failure)", f"stderr: {err!r}")
    else:
        _pass("idempotent no-op: no WARN (not a failure)")


def main() -> int:
    tests = [
        test_empty_candidates_skips_all_calls,
        test_two_call_batch_shape,
        test_dry_run_mode_skips_act_calls,
        test_preview_failure_drops_candidate,
        test_discovery_failure_is_non_blocking,
        test_partial_act_failure_is_named_and_nonzero,
        test_batch_dispatch_transport_failure_is_nonzero,
        test_idempotent_noop_is_not_a_failure,
    ]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
