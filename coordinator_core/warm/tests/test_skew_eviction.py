"""Tests for coordinator_core.warm.skew.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C16
"""

from __future__ import annotations

import inspect
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core import lifecycle
from coordinator_core.warm import client, skew
from coordinator_core.win_portability import no_console_creationflags

_GIT_SUBPROCESS_KWARGS = {"check": True, **no_console_creationflags()}


def _write_head_and_ref(git_dir: Path, ref_rel: str, sha: str) -> None:
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(f"ref: {ref_rel}\n", encoding="utf-8")
    ref_path = git_dir / ref_rel
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(sha + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# compute_client_token -- axis 1
# ---------------------------------------------------------------------------


def test_client_token_stable_when_nothing_changes(tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)

    first = skew.compute_client_token(root)
    second = skew.compute_client_token(root)
    assert first == second


def test_client_token_changes_when_ref_sha_changes(tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    before = skew.compute_client_token(root)

    time.sleep(0.01)
    (root / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    after = skew.compute_client_token(root)

    assert before != after


def test_client_token_changes_when_head_moves_to_another_branch(tmp_path):
    root = tmp_path / "clone"
    git_dir = root / ".git"
    _write_head_and_ref(git_dir, "refs/heads/main", "a" * 40)
    before = skew.compute_client_token(root)

    time.sleep(0.01)
    _write_head_and_ref(git_dir, "refs/heads/candidate", "a" * 40)
    after = skew.compute_client_token(root)

    assert before != after


def test_client_token_handles_detached_head(tmp_path):
    root = tmp_path / "clone"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")

    token = skew.compute_client_token(root)
    assert isinstance(token, str) and token


def test_client_token_handles_missing_git_dir(tmp_path):
    root = tmp_path / "no-git-here"
    token = skew.compute_client_token(root)
    assert isinstance(token, str) and token


@pytest.mark.skipif(
    subprocess.run(
        ["git", "--version"], capture_output=True, **no_console_creationflags()
    ).returncode
    != 0,
    reason="git not on PATH",
)
def test_client_token_changes_across_real_git_checkout(tmp_path):
    """Pins the C16 negative-spec finding: klabauter-channel.py's `_set`
    path runs `git checkout -B <target> --track origin/<target>`, and this
    test proves that command rewrites `.git/HEAD` in a way axis 1 observes
    -- i.e. an in-clone channel flip IS covered by the primary signal,
    with no separate `engine.target` handling needed in this module."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], **_GIT_SUBPROCESS_KWARGS)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], **_GIT_SUBPROCESS_KWARGS)

    env_args = [
        "-c", "user.email=test@example.com",
        "-c", "user.name=Test",
    ]
    (work / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work)] + env_args + ["add", "f.txt"], **_GIT_SUBPROCESS_KWARGS
    )
    subprocess.run(
        ["git", "-C", str(work)] + env_args + ["commit", "--quiet", "-m", "init"],
        **_GIT_SUBPROCESS_KWARGS,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "--quiet", "origin", "HEAD:main"],
        **_GIT_SUBPROCESS_KWARGS,
    )
    subprocess.run(
        ["git", "-C", str(work), "checkout", "--quiet", "-b", "candidate"],
        **_GIT_SUBPROCESS_KWARGS,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "--quiet", "origin", "HEAD:candidate"],
        **_GIT_SUBPROCESS_KWARGS,
    )
    subprocess.run(
        ["git", "-C", str(work), "checkout", "--quiet", "-B", "main", "--track", "origin/main"],
        **_GIT_SUBPROCESS_KWARGS,
    )

    before = skew.compute_client_token(work)
    time.sleep(0.01)

    checkout = subprocess.run(
        ["git", "-C", str(work), "checkout", "-B", "candidate", "--track", "origin/candidate"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert checkout.returncode == 0, checkout.stderr

    after = skew.compute_client_token(work)
    assert before != after


# ---------------------------------------------------------------------------
# ServerVersionState -- axis 1 (live comparison) and axis 2 (throttled)
# ---------------------------------------------------------------------------


def test_server_boots_with_sha_and_hash(monkeypatch, tmp_path):
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(tmp_path)

    assert state.server_sha == "deadbeef"
    assert state._boot_hash == "hash-0"


def test_is_skewed_false_when_client_token_matches_live_server_token(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root)
    client_token = skew.compute_client_token(root)

    assert state.is_skewed(client_token) is False


def test_is_skewed_true_on_axis1_primary_mismatch(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root)

    assert state.is_skewed("a-stale-client-token-from-before-a-checkout") is True


def test_is_skewed_true_on_axis2_secondary_staleness_via_dirty(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")

    hashes = iter(["hash-0", "hash-1"])
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: next(hashes))
    # Prefilter reports no mtime change (a bare edit to an already-touched
    # file, or clock-resolution coincidence) -- dirty is the discriminator.
    monkeypatch.setattr(skew, "_max_source_mtime", lambda pkg_dir: 123.0)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_dirty", lambda: True)

    fake_now = [0.0]
    state = skew.ServerVersionState(root, clock=lambda: fake_now[0])
    client_token = skew.compute_client_token(root)
    assert state.is_skewed(client_token) is False  # not yet due (interval hasn't elapsed)

    fake_now[0] = skew._REFRESH_INTERVAL_SECS + 1.0
    assert state.is_skewed(client_token) is True


def test_axis2_does_not_rehash_when_prefilter_unchanged_and_not_dirty(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")
    monkeypatch.setattr(skew, "_max_source_mtime", lambda pkg_dir: 123.0)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_dirty", lambda: False)

    fake_now = [0.0]
    state = skew.ServerVersionState(root, clock=lambda: fake_now[0])
    client_token = skew.compute_client_token(root)

    fake_now[0] = skew._REFRESH_INTERVAL_SECS + 1.0
    assert state.is_skewed(client_token) is False
    assert state._source_stale is False


def test_axis2_refresh_throttled_to_interval(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    calls = {"n": 0}

    def counting_prefilter(pkg_dir):
        calls["n"] += 1
        return 123.0

    monkeypatch.setattr(skew, "_max_source_mtime", counting_prefilter)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_dirty", lambda: False)

    fake_now = [0.0]
    state = skew.ServerVersionState(root, clock=lambda: fake_now[0])
    calls["n"] = 0  # reset past the constructor's own prefilter call

    client_token = skew.compute_client_token(root)
    state.is_skewed(client_token)
    state.is_skewed(client_token)
    state.is_skewed(client_token)
    assert calls["n"] == 0  # interval hasn't elapsed; refresh is a no-op

    fake_now[0] = skew._REFRESH_INTERVAL_SECS + 1.0
    state.is_skewed(client_token)
    assert calls["n"] == 1


def test_source_stale_is_sticky(monkeypatch, tmp_path):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")

    hashes = iter(["hash-0", "hash-1", "hash-0"])
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: next(hashes))
    monkeypatch.setattr(skew, "_max_source_mtime", lambda pkg_dir: 123.0)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_dirty", lambda: True)

    fake_now = [0.0]
    state = skew.ServerVersionState(root, clock=lambda: fake_now[0])
    client_token = skew.compute_client_token(root)

    fake_now[0] += skew._REFRESH_INTERVAL_SECS + 1.0
    assert state.is_skewed(client_token) is True

    fake_now[0] += skew._REFRESH_INTERVAL_SECS + 1.0
    assert state.is_skewed(client_token) is True  # reverted hash does not un-stale


# ---------------------------------------------------------------------------
# evict_on_skew -- THE INVERSION: close precedes drain, no idleness input
# ---------------------------------------------------------------------------


def test_evict_on_skew_orders_respond_close_drain():
    order = []

    skew.evict_on_skew(
        respond=lambda payload: order.append(("respond", payload)),
        close_listener=lambda: order.append(("close", None)),
        drain=lambda: order.append(("drain", None)),
        request_id=42,
        server_sha="deadbeef",
        client_token="tok-123",
    )

    assert [step for step, _ in order] == ["respond", "close", "drain"]

    respond_step, payload = order[0]
    assert payload["error"]["code"] == skew.ENGINE_SKEW
    assert payload["error"]["data"]["server_sha"] == "deadbeef"
    assert payload["error"]["data"]["client_token"] == "tok-123"
    assert payload["id"] == 42


def test_evict_on_skew_close_precedes_drain_even_if_drain_raises():
    order = []

    def failing_drain():
        order.append(("drain", None))
        raise RuntimeError("drain blew up")

    with pytest.raises(RuntimeError):
        skew.evict_on_skew(
            respond=lambda payload: order.append(("respond", payload)),
            close_listener=lambda: order.append(("close", None)),
            drain=failing_drain,
            request_id=1,
            server_sha=None,
            client_token="tok",
        )

    # The listener must already be closed by the time drain ran (and blew
    # up) -- a drain failure must never leave the stale listener open.
    assert [step for step, _ in order] == ["respond", "close", "drain"]


def test_evict_on_skew_signature_carries_no_idleness_or_drain_flag_input():
    params = set(inspect.signature(skew.evict_on_skew).parameters)
    forbidden = {"idle", "idleness", "in_flight", "in_flight_count", "drain_flag", "is_draining"}
    assert not (params & forbidden)


def test_build_skew_response_shape():
    payload = skew.build_skew_response(7, "deadbeef", "tok-abc")
    assert payload == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32001,
            "message": "engine skew: this warm server is running stale source",
            "data": {"server_sha": "deadbeef", "client_token": "tok-abc"},
        },
    }


def test_engine_skew_constant_matches_client_module():
    assert skew.ENGINE_SKEW == client.ENGINE_SKEW == -32001
