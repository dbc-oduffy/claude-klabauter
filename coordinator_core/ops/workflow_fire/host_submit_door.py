"""Darwin host-side adapter for the installed coordinator-invoke submit command."""

import json
import os
import stat
import struct

import psutil

from coordinator_core.invoke.__main__ import _exit_code_for_response
from coordinator_core.invoke.dispatch import STRUCTURAL_PIN_ERROR
from coordinator_core.warm.restricted_endpoint import _matches, _read_request, _unique_keys


_ARGV = ["engine_ask.submit", "--params-file", ".coordinator-local/em-submit.json"]


def _issued_file(workspace):
    # Follow neither a substituted state directory nor a substituted final file.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(workspace, directory_flags)
    try:
        state = os.open(".coordinator-local", directory_flags, dir_fd=root)
        try:
            fd = os.open("em-submit.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("regular submit params file required")
                data = stream.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError("submit params exceed one MiB")
        finally:
            os.close(state)
    finally:
        os.close(root)
    return json.loads(data, object_pairs_hook=_unique_keys)


def serve_submit_connection(conn, *, command_process, native_process, caller_env, allocation):
    """Both process handles and the caller environment come from the supervising launcher."""
    request_id = None
    try:
        if (not _matches(conn, command_process) or not native_process.is_running()
                or native_process not in command_process.parents()):
            result = {"refused": "launch-identity-mismatch"}
        else:
            frame = _read_request(conn, limit=65536)
            expected = {"argv": _ARGV, "cwd": str(allocation.workspace)}
            if (not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0"
                    or frame.get("method") != "invoke.from_argv" or frame.get("params") != expected
                    or type(frame.get("id")) is not int):
                result = {"refused": "unissued-command"}
            else:
                request_id = frame["id"]
                result = allocation.receive(
                    _issued_file(allocation.workspace), caller_env=caller_env,
                    native_pid=native_process.pid, native_birth=int(native_process.create_time()),
                )
    except (ValueError, OSError, psutil.Error, struct.error, RecursionError):
        result = {"refused": "host-submit-unavailable"}
    except Exception:
        # Dispatch may already have minted. Keep the durable claim and return a terminal door response.
        result = {"refused": "reconcile-existing-submit"}
    reply = {"jsonrpc": "2.0", "id": 1, "result": result}
    code = _exit_code_for_response(reply, STRUCTURAL_PIN_ERROR, "engine_ask.submit")
    door = {"stdout": json.dumps(reply) + "\n", "stderr": "", "exit_code": code}
    conn.sendall((json.dumps({"jsonrpc": "2.0", "id": request_id, "result": door}) + "\n").encode())
