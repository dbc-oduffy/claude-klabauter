"""percolate-mirror.py — one entry point owns a whole mirror's publish.

Covers the two properties that make it worth existing over N `percolate-round`
invocations: it resolves a mirror's FULL row set from any of three selector
forms, and it publishes them in ONE `publish.py` invocation held under ONE dest
lock, with the lock taken before anything mutates the dest.

Spec backlink: state/sizings/2026-08-18-one-entry-point-owns-the-mirror-publish.yaml
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_mirror", _BIN_DIR / "percolate-mirror.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Selector resolution. `load_targets` returns RESOLVED rows (field 3 is an
# absolute dest), so the grouping key is the dest's git worktree root, not the
# portable file's `publish-mirror:*` sigil -- which is already gone by then.
# ---------------------------------------------------------------------------

_GROUPS = {
    "X:/claude-klabauter": [
        "claude-klabauter-publish-repo-toplevel",
        "claude-klabauter-bin",
        "claude-klabauter",
    ],
    "X:/other-mirror": ["other-thing"],
}


def test_selector_accepts_worktree_root_verbatim():
    assert _mod._select_mirror("X:/claude-klabauter", _GROUPS) == "X:/claude-klabauter"


def test_selector_accepts_bare_mirror_name():
    assert _mod._select_mirror("claude-klabauter", _GROUPS) == "X:/claude-klabauter"


def test_selector_accepts_underscore_separator_form():
    assert _mod._select_mirror("claude_klabauter", _GROUPS) == "X:/claude-klabauter"


def test_selector_accepts_any_member_target_name():
    """A caller who knows only a row name must not have to learn the path."""
    assert _mod._select_mirror("claude-klabauter-bin", _GROUPS) == "X:/claude-klabauter"


def test_selector_unknown_returns_none():
    assert _mod._select_mirror("no-such-mirror", _GROUPS) is None


def test_selector_resolves_whole_row_set_not_just_the_named_row():
    """The bug this entry point exists to remove: naming one row must select
    every row landing in that mirror, so they publish in one invocation."""
    root = _mod._select_mirror("claude-klabauter-bin", _GROUPS)
    assert _GROUPS[root] == [
        "claude-klabauter-publish-repo-toplevel",
        "claude-klabauter-bin",
        "claude-klabauter",
    ]


# ---------------------------------------------------------------------------
# One invocation, one lock, lock before mutation.
# ---------------------------------------------------------------------------


class _RecordingLockCtx:
    def __init__(self, order: List[str], *a, **kw):
        self._order = order

    def __enter__(self):
        self._order.append("lock-acquired")
        return self

    def __exit__(self, *a):
        self._order.append("lock-released")
        return False


def _wire(monkeypatch, order, *, dirty=False, scan_rc=0, drift_anchor="marker", drift_real=False):
    targets = [
        "claude-klabauter-publish-repo-toplevel",
        "claude-klabauter-bin",
        "claude-klabauter",
    ]
    monkeypatch.setattr(
        _mod, "_mirror_groups", lambda root: {"X:/claude-klabauter": targets}
    )
    monkeypatch.setattr(_mod._round, "_resolve_dest", lambda t, r: "X:/claude-klabauter")
    monkeypatch.setattr(_mod._round, "_resolve_repo_root", lambda d: "X:/claude-klabauter")
    monkeypatch.setattr(
        _mod._round,
        "_round_held_lock",
        lambda target, **kw: _RecordingLockCtx(order, target, **kw),
    )
    # `_split_stdout_by_row_dest` survives chunk C4 for exactly this caller
    # (`_run_gate_legs`'s scan-secrets/inverse-drift row attribution) --
    # unrelated to the commit pathspec, so still monkeypatched here.
    monkeypatch.setattr(_mod._round, "_split_stdout_by_row_dest", lambda s, d: [("X:/claude-klabauter", s)])
    # Chunk C4: the commit pathspec now comes from a `RoundManifest`
    # `_read_fresh_round_manifest` reads off disk, never from `_extract_
    # change_lines`/`_build_commit_pathspec` parsing publish.py's stdout
    # (both gone). Stubbed here the same way test_percolate_round.py's
    # `_install_manifest_stub` does -- a fixed manifest standing in for what
    # publish.py's real run would have persisted.
    monkeypatch.setattr(
        _mod._round,
        "_read_fresh_round_manifest",
        lambda repo_root, not_before: _mod._round._RoundManifest(
            round_id="test", added_or_updated=frozenset({"a.py"})
        ),
    )
    monkeypatch.setattr(
        _mod._round, "_push_dest", lambda d: subprocess.CompletedProcess([], 0, "", "")
    )

    monkeypatch.setattr(_mod, "_row_paths", lambda r: {n: ("X:/src", "X:/claude-klabauter") for n in targets})
    monkeypatch.setattr(_mod._round, "_resolve_central_state", lambda: None)

    publish_calls = []

    def _fake_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "publish.py" in joined:
            order.append("publish")
            publish_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Rows succeeded: 3/3", "")
        if "parse-dryrun" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, '{"preflight": {"step2c_scan_file_list": ["a.py"]}}', ""
            )
        if "scan-secrets" in joined:
            order.append("scan")
            return subprocess.CompletedProcess(cmd, scan_rc, "", "")
        if "inverse-drift" in joined:
            order.append("drift")
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        "anchor_mode": drift_anchor,
                        "anchor_reliable": drift_anchor == "marker",
                        "anchor_ref": "deadbeef",
                        "commits": 1 if drift_real else 0,
                        "commit_lines": ["abc123 2026-08-18 hand edit"] if drift_real else [],
                        "dismissed_crlf_only": [],
                        "content_differs": ["a.py"] if drift_real else [],
                        "real_drift": drift_real,
                    }
                ),
                "",
            )
        if "scoped-git-commit" in joined:
            order.append("commit")
            return subprocess.CompletedProcess(cmd, 0, '{"committed": true}', "")
        if cmd and str(cmd[0]) == "git":
            # Pre-flight's own dirty probe. Clean tree unless the test asked
            # for residue, so the reconcile path stays out of the ordering
            # assertions that are not about it.
            return subprocess.CompletedProcess(
                cmd, 0, " M stranded.py\n" if dirty else "", ""
            )
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod._round, "_run", _fake_run)

    # The commit pathspec comes from the manifest publish.py PERSISTS, never from
    # parsing its stdout (`percolate-mirror.py`'s own comment says so, matching
    # percolate-round's C4 fix). Nothing in this test process writes that manifest,
    # so without this stub `_pathspec_from_manifest` is empty, the mirror prints
    # "publish reported no changed files; nothing to commit" and returns before the
    # commit step -- which is why the ordering assertions stopped seeing "commit".
    # Mirrors `test_percolate_round.py::_install_manifest_stub`'s shape.
    declared = frozenset({"a.py"})

    def _fake_read_fresh_manifest(repo_root, not_before):
        return _mod._round._RoundManifest(
            round_id="test",
            added_or_updated=declared,
            removed=frozenset(),
            declared_payload=declared,
        )

    monkeypatch.setattr(_mod._round, "_read_fresh_round_manifest", _fake_read_fresh_manifest)
    monkeypatch.setattr(
        _mod._round, "_pathspec_from_manifest", lambda manifest, repo_root: (sorted(declared), _mod._round._no_filter_drops())
    )

    # The commit leg is an in-process `commit_paths` call (C4 repoint,
    # docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-pipeline-
    # can-go.md, off the killed `run_commit_pipeline`), not a
    # `scoped-git-commit` spawn, so the "commit" ordering marker comes from
    # here rather than from `_fake_run`'s argv branch. Same reasoning as
    # `test_percolate_round.py::_install_commit_pipeline_stub`.
    from coordinator_core.git import commit as commit_mod

    def _fake_commit_paths(repo_root, paths, message, **kwargs):
        order.append("commit")
        return commit_mod.CommitOutcome(
            sha="deadbeef1234", staged_preferred=(), worktree_over_staged=()
        )

    monkeypatch.setattr(commit_mod, "commit_paths", _fake_commit_paths)
    return targets, publish_calls


def test_all_rows_go_through_a_single_publish_invocation(tmp_path, monkeypatch):
    """The whole point: N rows, ONE publish.py process. `--delta` skips a row's
    work but never its verification, so N invocations re-scan the full dest N
    times."""
    order: List[str] = []
    targets, publish_calls = _wire(monkeypatch, order)

    rc = _mod.main(
        ["claude-klabauter", "--percolate-root", str(tmp_path), "--invocation-authorized"]
    )

    assert rc == _mod._round._EXIT_OK
    assert order.count("publish") == 1, order
    assert ",".join(targets) in publish_calls[0], publish_calls[0]


def test_lock_spans_publish_and_commit(tmp_path, monkeypatch):
    order: List[str] = []
    _wire(monkeypatch, order)

    _mod.main(
        ["claude-klabauter", "--percolate-root", str(tmp_path), "--invocation-authorized"]
    )

    # scan/drift run once per row (both gates are target-scoped), so assert the
    # SHAPE: lock first, publish, then every gate leg, then commit, then release.
    assert order[0] == "lock-acquired", order
    assert order[-1] == "lock-released", order
    assert order[1] == "publish", order
    assert order[-2] == "commit", order
    assert order.count("scan") == 3, order
    assert order.count("drift") == 3, order
    assert order.index("commit") > max(
        i for i, m in enumerate(order) if m in ("scan", "drift")
    ), order


def test_high_tier_leak_aborts_before_commit(tmp_path, monkeypatch):
    """scan-secrets exit 2 is a HIGH-tier credential shape. This entry point
    publishes to a PUBLIC mirror, so it must refuse to commit or push — the
    bytes are already synced to dest, but nothing is published until commit."""
    order: List[str] = []
    _wire(monkeypatch, order, scan_rc=2)

    rc = _mod.main(
        ["claude-klabauter", "--percolate-root", str(tmp_path), "--invocation-authorized"]
    )

    assert rc == _mod._round._EXIT_FAIL
    assert "commit" not in order, order


def test_gate_legs_run_for_every_row_not_just_the_first(tmp_path, monkeypatch):
    """scan-secrets' peer-repo pattern and registry_codenames guard are both
    target-scoped, so one pass over the first row would leave the other rows
    unscanned."""
    order: List[str] = []
    targets, _ = _wire(monkeypatch, order)

    _mod.main(
        ["claude-klabauter", "--percolate-root", str(tmp_path), "--invocation-authorized"]
    )

    assert order.count("scan") == len(targets), order


def test_no_publish_commits_but_does_not_push(tmp_path, monkeypatch):
    order: List[str] = []
    _wire(monkeypatch, order)
    pushed = []
    monkeypatch.setattr(
        _mod._round,
        "_push_dest",
        lambda d: pushed.append(d) or subprocess.CompletedProcess([], 0, "", ""),
    )

    rc = _mod.main(
        [
            "claude-klabauter",
            "--percolate-root",
            str(tmp_path),
            "--invocation-authorized",
            "--no-publish",
        ]
    )

    assert rc == _mod._round._EXIT_OK
    assert "commit" in order
    assert pushed == []


def test_non_tty_without_authorization_refuses_before_commit(tmp_path, monkeypatch):
    order: List[str] = []
    _wire(monkeypatch, order)
    monkeypatch.setattr(_mod.sys.stdin, "isatty", lambda: False)

    rc = _mod.main(["claude-klabauter", "--percolate-root", str(tmp_path)])

    assert rc == _mod._round._EXIT_CONFIRM_REQUIRED
    assert "commit" not in order


def test_list_prints_row_set_and_publishes_nothing(tmp_path, monkeypatch, capsys):
    order: List[str] = []
    _wire(monkeypatch, order)

    rc = _mod.main(["claude-klabauter", "--percolate-root", str(tmp_path), "--list"])

    assert rc == _mod._round._EXIT_OK
    assert order == []
    out = capsys.readouterr().out
    assert "claude-klabauter-bin" in out


# ---------------------------------------------------------------------------
# C2 — a contended row's `repo_root` refuses at once, naming that row's
# destination via the same `_lock_busy_message` builder `percolate-round.py`
# uses (the mirror takes exactly ONE lock, on the resolved worktree root, so
# a contended sweep refuses as a whole rather than per-row -- see C2's brief,
# "the per-row abort-vs-continue question is moot, not open").
# ---------------------------------------------------------------------------


class _TimeoutLockCtx:
    def __init__(self, target, **kwargs):
        pass

    def __enter__(self):
        raise _mod._round._RoundLockTimeout(
            "Could not acquire lock for X:/claude-klabauter within 0.0s "
            "(held by: pid=4242 holder='peer:percolate-round' "
            "acquired_at=2026-08-30T00:00:00+00:00)"
        )

    def __exit__(self, exc_type, exc, tb):
        return False


def test_percolate_mirror_denies_fast_on_contended_repo_root(tmp_path, monkeypatch, capsys):
    """Third leg (brief's own numbering): a row's `repo_root` held by a peer
    round refuses in well under a second and names that row's destination
    plus the holder — never a 180s sleep."""
    targets = [
        "claude-klabauter-publish-repo-toplevel",
        "claude-klabauter-bin",
        "claude-klabauter",
    ]
    monkeypatch.setattr(
        _mod, "_mirror_groups", lambda root: {"X:/claude-klabauter": targets}
    )
    monkeypatch.setattr(_mod._round, "_resolve_dest", lambda t, r: "X:/claude-klabauter")
    monkeypatch.setattr(_mod._round, "_resolve_repo_root", lambda d: "X:/claude-klabauter")
    monkeypatch.setattr(
        _mod._round,
        "_round_held_lock",
        lambda target, **kw: _TimeoutLockCtx(target, **kw),
    )

    import time

    start = time.monotonic()
    rc = _mod.main(
        ["claude-klabauter", "--percolate-root", str(tmp_path), "--invocation-authorized"]
    )
    elapsed = time.monotonic() - start

    # Content contract (holder named, mechanism page present, override
    # key/re-run imperative absent) is asserted once, directly on
    # `wire_contract.lock_busy_message`
    # (test_wire_contract_publish_contention.py); this leg only checks the
    # per-entrypoint exit code, deny-at-once timing, and that the holder
    # metadata for THIS row's destination is folded in.
    assert rc == _mod._round._EXIT_LOCK_BUSY
    assert elapsed < 1.0
    err = capsys.readouterr().err
    assert "X:/claude-klabauter" in err
    assert "pid=4242" in err
