"""Forward a child-spawning op from a sandboxed (restricted-token) caller to the warm engine.

A codex EM sandbox has no network and every process it spawns inherits that token, so a
spawning op must run in the warm engine instead of nested. `caller_is_sandboxed` is the
probe; `forward_spawn_to_warm` is the one-shot warm dispatch, with no server spawn.
"""

from __future__ import annotations

import sys
from typing import Any

from coordinator_core.warm import client

JSONRPC_VERSION = "2.0"


class SandboxedSpawnRefused(RuntimeError):
    """The warm engine could not be reached from a sandboxed caller; nothing was spawned."""


class WarmServerError(RuntimeError):
    """The warm server answered with a JSON-RPC error envelope; `.error` is that envelope's error."""

    def __init__(self, error: dict) -> None:
        self.error = error
        super().__init__(f"warm engine error {error.get('code')}: {error.get('message')}")


class SpawnIndeterminate(RuntimeError):
    """The request was delivered but unanswered; the run may exist. Reconcile `run_id`, never re-fire."""

    def __init__(self, run_id: Any, error: dict) -> None:
        self.run_id = run_id
        self.error = error
        super().__init__(
            f"spawn indeterminate for run {run_id}: {error.get('message')}; "
            "reconcile before retrying"
        )


def _process_token_is_restricted() -> bool:
    """True when this process runs under a restricted token (a sandbox child).

    Windows-only; False off Windows and on any API failure, so a probe problem never
    forwards a spawn it should have run in place."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.IsTokenRestricted.argtypes = [wintypes.HANDLE]
        advapi32.IsTokenRestricted.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        token = wintypes.HANDLE()
        token_query = 0x0008
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return False
        try:
            return bool(advapi32.IsTokenRestricted(token))
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 -- a probe failure names no cause
        return False


def caller_is_sandboxed() -> bool:
    """True when this process holds a restricted token."""
    return _process_token_is_restricted()


def _cold_reason(missed: bool) -> str:
    if not client.is_warm_enabled():
        return "warm_disabled"
    return client.last_cold_reason() or (
        "no_server" if missed else "no_warm_reply: skew, contention or closed pipe"
    )


def forward_spawn_to_warm(op: str, params: dict, repo: str) -> dict:
    """Run `op` in the warm engine and return the reply's `result`.

    Never starts a server. A miss raises `SandboxedSpawnRefused` naming the cold reason; a JSON-RPC
    error envelope raises `WarmServerError`; a delivered-but-unanswered mutation raises
    `SpawnIndeterminate` (a mutation, so never a refusal and never a cold re-run).
    """
    reply = client.try_warm_dispatch_no_spawn(
        {"jsonrpc": JSONRPC_VERSION, "id": 1, "method": op, "params": params, "_origin_worktree": repo}
    )
    missed = client.spawn_wanted()
    if reply is None:
        raise SandboxedSpawnRefused(
            "child would spawn inside the codex sandbox without network; "
            f"warm engine unreachable: {_cold_reason(bool(missed))}; publish/restart the warm engine"
        )
    error = reply.get("error")
    if isinstance(error, dict):
        if error.get("code") == client.WARM_DISPATCH_INDETERMINATE:
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            raise SpawnIndeterminate(params.get("run_id") or data.get("dispatch_key"), error)
        raise WarmServerError(error)
    return reply.get("result")
