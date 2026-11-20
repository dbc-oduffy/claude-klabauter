"""`warm.generation` compares the token a server's breadcrumb recorded against a
freshly computed one; on POSIX the recorded endpoint is a socket path."""

from __future__ import annotations

from coordinator_core.warm import breadcrumb


def test_posix_socket_endpoint_yields_the_token_not_the_suffix() -> None:
    assert breadcrumb.endpoint_token("/run/user/1000/coordinator/warm/ab12/0123456789abcdef.sock") == "0123456789abcdef"


def test_windows_pipe_endpoint_still_yields_the_trailing_token() -> None:
    assert breadcrumb.endpoint_token(r"\.\pipe\coordinator.warm.0123456789abcdef") == "0123456789abcdef"
    assert breadcrumb.endpoint_token(r"C:\x\y.sock") == "y"
